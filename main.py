"""FastAPI application entry point for the data-middle-platform."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from aiofiles import open as aio_open

from src import db, dedup, models, vlm
from src import mysql_client
from src.config import config as app_config
from src.converter import SUPPORTED_EXTENSIONS as TEXT_EXTENSIONS
from src.chunker import count_tokens
from src.image_handler import SUPPORTED_EXTENSIONS as IMAGE_EXTENSIONS
from src import milvus_client as mc
from src.milvus_client import collection_primary_key, has_scalar_field
from src import milvus_expr as mx
from src.logging_config import get_logger
from src.wiki import database as wiki_db
from src.wiki.api import router as wiki_router

_log = get_logger(__name__)

_BASE = Path(__file__).resolve().parent

app = FastAPI(title="Data Middle Platform", version="0.1.0")

# CORS origins come from env (default: localhost only). Same-origin frontend is
# unaffected; only cross-origin tooling changes.
_cors_origins = [o.strip() for o in app_config.security.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve normalized images (outputs/images) so a RAG service can fetch them by
# the relative /media/images/{name} URL stored in Milvus.
_images_dir = Path(app_config.output_dir_abs) / "images"
app.mount("/media/images", StaticFiles(directory=str(_images_dir), check_dir=False))


# ── Request id + API version (ROADMAP P4-T1) ──────────────────────
# Every response carries a correlation id (echoed back or generated) and the
# API version, so callers / logs can trace a request end to end. Also records
# Prometheus HTTP metrics (P4-T3), skipping /metrics itself to avoid recursion.

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
    request.state.request_id = rid
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    response.headers["X-API-Version"] = "v1"
    if request.url.path != "/metrics":
        try:
            from src import metrics

            metrics.record(request.method, request.url.path, response.status_code,
                           time.perf_counter() - start)
        except Exception:
            pass  # metrics must never break requests
    return response


# ── Generic per-IP rate limit (ROADMAP P4-T2) ─────────────────────
# Redis-backed sliding window per minute; exempts infra/static paths. Redis down
# degrades open (fail-lenient, see src/rate_limit.py).

@app.middleware("http")
async def ip_rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    if path in ("/metrics", "/health") or path.startswith(("/media/", "/wiki/")):
        return await call_next(request)
    from src import rate_limit

    ip = request.client.host if request.client else "unknown"
    if rate_limit.ip_rate_limit_hit(ip, app_config.security.ip_rate_limit_per_minute):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})
    return await call_next(request)

# Wiki frontend build (ROADMAP D5): vite output in web/dist is served at /wiki.
# Skip mount when the build doesn't exist yet (frontend not built).
_wiki_dist = _BASE / "web" / "dist"
if _wiki_dist.exists():
    app.mount("/wiki", StaticFiles(directory=str(_wiki_dist), html=True), name="wiki")

# Wiki domain API (ROADMAP P1-T2): pages / revisions / spaces.
app.include_router(wiki_router)


def _require_api_key(x_api_key: str | None = Header(default=None)):
    """Optional mutating-endpoint guard. No-op unless API_KEY is configured."""
    key = app_config.security.api_key
    if key and x_api_key != key:
        raise HTTPException(401, "Invalid or missing X-API-Key")


# ── Global exception handlers ──────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    _log.warning("HTTP %d: %s %s (request_id=%s)", exc.status_code, request.method,
                 request.url.path, getattr(request.state, "request_id", ""))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    _log.exception("Unhandled error: %s %s (request_id=%s)", request.method,
                   request.url.path, getattr(request.state, "request_id", ""))
    # Never leak internal exception text/stack to clients.
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ── Celery worker auto-start ─────────────────────────────────────────
#
# Dev convenience: starting the API also starts the Celery worker, so a single
# click (e.g. the PyCharm FastAPI run config) brings up both. The worker is
# spawned with ``sys.executable -m celery`` — the same interpreter running the
# API — so there is no hardcoded venv path and it works on any machine.

_celery_worker_proc: subprocess.Popen | None = None


def _celery_worker_running() -> bool:
    """True if any worker is already consuming this project's queue.

    Broadcasts a lightweight control ping (broker-only Celery app, no task
    imports) — any live worker on the broker answers, whether it was
    auto-started or launched manually in a terminal.
    """
    from celery import Celery

    probe = Celery("pipeline", broker=app_config.redis_url)
    try:
        return bool(probe.control.ping(timeout=1))
    except Exception:
        # Broker unreachable (e.g. Redis down): can't confirm a worker, so let
        # the spawner try — the worker retries the broker connection itself.
        return False


def _start_celery_worker() -> None:
    """Spawn the Celery worker as a child of the API process, if not running.

    Skipped when AUTO_START_WORKER=false (tests, docker `api` service) or when
    a worker already consumes the queue. Logs go to celery_worker.log, matching
    the existing file at the project root.
    """
    global _celery_worker_proc
    if not app_config.app.auto_start_worker:
        return
    if _celery_worker_running():
        _log.info("Celery worker already running, skipping auto-start")
        return

    log_path = _BASE / "celery_worker.log"
    cmd = [
        sys.executable, "-m", "celery",
        "-A", "tasks.celery_worker",
        "worker",
        "--loglevel=INFO",
        "-P", "solo",  # Windows has no prefork pool
        "--logfile", str(log_path),
    ]
    kwargs: dict = {}
    if os.name == "nt":
        # Detach from the API's console so closing PyCharm doesn't kill the
        # worker; it stays manageable via the Popen handle (terminated on
        # graceful shutdown below).
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    log_fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    try:
        _celery_worker_proc = subprocess.Popen(
            cmd, cwd=str(_BASE), stdout=log_fh, stderr=log_fh, **kwargs
        )
    except OSError as e:
        log_fh.close()
        _log.warning("Failed to auto-start Celery worker: %s", e)
        return
    # The child holds an inherited copy of the handle; drop the parent's.
    log_fh.close()
    _log.info("Auto-started Celery worker (pid=%s, log=%s)", _celery_worker_proc.pid, log_path)


def _stop_celery_worker() -> None:
    """Terminate the worker spawned by this API process (best effort)."""
    global _celery_worker_proc
    proc = _celery_worker_proc
    _celery_worker_proc = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            _log.warning("Failed to stop auto-started Celery worker (pid=%s)", proc.pid, exc_info=True)


# ── MinerU API auto-start ─────────────────────────────────────────────
#
# Dev convenience: starting the API also starts the mineru-api service (GPU
# document parsing for pdf/docx/pptx), so one click brings up API + worker +
# MinerU. The service runs in its own venv (D:\my_env\mineru_env); the
# executable comes from MINERU_EXECUTABLE. It is spawned detached and left
# running — deliberately NOT terminated on shutdown — so uvicorn --reload
# restarts never flap the GPU service; a fresh API boot reuses it via the
# port check below.

_mineru_api_proc: subprocess.Popen | None = None


def _mineru_api_running() -> bool:
    """True if something already listens on MINERU_BASE_URL's host:port."""
    from urllib.parse import urlparse

    url = urlparse(app_config.app.mineru_base_url)
    host = url.hostname or "127.0.0.1"
    port = url.port or 8010

    import socket

    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _start_mineru_api() -> None:
    """Spawn the mineru-api service as a detached child, if not running.

    Skipped when MINERU_ENABLED=false or MINERU_AUTO_START=false (tests,
    docker), or when a mineru-api already listens on MINERU_BASE_URL (whether
    started manually or by a previous API boot). Logs go to mineru_api.log.
    If the executable is missing or the spawn fails, pdf/docx/pptx conversions
    fail with a clear error — the API itself never fails to start.
    """
    global _mineru_api_proc
    if not app_config.app.mineru_enabled or not app_config.app.mineru_auto_start:
        return
    if _mineru_api_running():
        _log.info("MinerU API already listening on %s, skipping auto-start",
                  app_config.app.mineru_base_url)
        return

    exe = app_config.app.mineru_executable
    if not Path(exe).is_file():
        import shutil

        exe = shutil.which("mineru-api") or ""
    if not exe:
        _log.warning(
            "MinerU executable not found (MINERU_EXECUTABLE=%s) — skipping "
            "auto-start; pdf/docx/pptx conversions will fail with a clear error",
            app_config.app.mineru_executable,
        )
        return

    from urllib.parse import urlparse

    url = urlparse(app_config.app.mineru_base_url)
    host = url.hostname or "127.0.0.1"
    port = url.port or 8010
    log_path = _BASE / "mineru_api.log"
    cmd = [exe, "--host", host, "--port", str(port)]
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    log_fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    try:
        _mineru_api_proc = subprocess.Popen(
            cmd, cwd=str(_BASE), stdout=log_fh, stderr=log_fh, **kwargs
        )
    except OSError as e:
        log_fh.close()
        _log.warning("Failed to auto-start MinerU API: %s", e)
        return
    log_fh.close()
    _log.info("Auto-started MinerU API (pid=%s, log=%s) on %s",
              _mineru_api_proc.pid, log_path, app_config.app.mineru_base_url)


def _dispatch_task(name: str, *args, **kwargs):
    """Publish a Celery task by name without importing the task module.

    Importing ``tasks.celery_worker`` pulls in sentence-transformers / cn-clip
    (multi-second import) — fine for slow POST triggers, but unacceptable on a
    DELETE we want to feel instant. Sending by name leaves resolution to the
    worker, which has the module loaded already.
    """
    from celery import Celery

    probe = Celery("pipeline", broker=app_config.redis_url)
    return probe.send_task(name, args=args, kwargs=kwargs)


# ── Startup ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    db.init_db()
    Path(app_config.upload_dir_abs).mkdir(parents=True, exist_ok=True)
    Path(app_config.output_dir_abs).mkdir(parents=True, exist_ok=True)
    _images_dir.mkdir(parents=True, exist_ok=True)
    # Reset files stuck in converting/chunking/ingesting by a dead worker.
    db.recover_stale_files(stale_seconds=app_config.app.recover_stale_seconds)
    # Ensure the wiki Postgres engine is initialized (lazy: creates the engine,
    # does not open a connection, so this is safe when Postgres is down).
    wiki_db.get_engine()
    # Seed system user + default space. Tolerated failure: in test environments
    # the dedicated test DB may not exist yet (the wiki fixture creates it); in
    # production Postgres should be up, so this normally succeeds.
    wiki_ready = False
    try:
        with wiki_db.session_scope() as s:
            from src.wiki.seed import ensure_seed_data

            ensure_seed_data(s)
        wiki_ready = True
    except Exception:
        _log.warning("Wiki seed data not applied (Postgres may be unavailable)", exc_info=True)
    if wiki_ready:
        # Resolve pages stuck in `publishing` by a dead worker (ROADMAP D10).
        try:
            from src.wiki.publish import recover_stale_publishes

            recovered = recover_stale_publishes(app_config.app.recover_stale_seconds)
            if recovered:
                _log.info("Recovered %d stale wiki publishes", len(recovered))
        except Exception:
            _log.warning("Wiki publish recovery skipped (Milvus/Postgres may be down)", exc_info=True)
        # Purge trash older than the retention window (ROADMAP P3-T2).
        try:
            from src.wiki.trash import purge_expired_trash

            purged = purge_expired_trash(app_config.app.trash_retention_days)
            if purged:
                _log.info("Purged %d expired trash pages", len(purged))
        except Exception:
            _log.warning("Trash retention cleanup skipped (Milvus/Postgres may be down)", exc_info=True)
    _log.info("Server started, DB initialized")
    _start_celery_worker()
    _start_mineru_api()


@app.on_event("shutdown")
async def shutdown():
    _stop_celery_worker()


@app.get("/health")
async def health():
    """Liveness/readiness probe: DB + Milvus + Redis + Postgres reachability."""
    import redis as redis_lib

    status = {"status": "ok", "db": True, "milvus": True, "redis": True, "postgres": True}
    try:
        db.init_db()
        db.get_file("__health_probe__") or db.list_files(limit=1)
    except Exception:
        status["db"] = False
        status["status"] = "degraded"
    try:
        if not mc.health_check():
            status["milvus"] = False
            status["status"] = "degraded"
    except Exception:
        status["milvus"] = False
        status["status"] = "degraded"
    try:
        r = redis_lib.from_url(app_config.redis_url, socket_connect_timeout=2)
        r.ping()
        r.close()
    except Exception:
        status["redis"] = False
        status["status"] = "degraded"
    try:
        # Wiki domain lives in Postgres — readiness must cover it (P4-T2 修复).
        from sqlalchemy import text

        from src.wiki import database as wdb

        with wdb.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        status["postgres"] = False
        status["status"] = "degraded"
    return JSONResponse(status_code=200 if status["status"] == "ok" else 503, content=status)


@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus scrape endpoint (ROADMAP P4-T3)."""
    from fastapi.responses import Response as FastAPIResponse

    from src import metrics

    metrics.refresh_celery_queue(app_config.redis_url)
    return FastAPIResponse(content=metrics.render(), media_type="text/plain; version=0.0.4")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = _BASE / "static" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>static/index.html not found</h1>", status_code=404)


def _file_to_info(f: dict, chunk_cnt: int | None = None,
                  clean_state: str = "raw", has_clean: bool = False) -> dict:
    if chunk_cnt is None:
        chunk_cnt = db.get_chunk_count(f["id"]) if f.get("type") == "text" else 0
    return {
        "id": f["id"],
        "name": f["name"],
        "size": f["size"],
        "type": f["type"],
        "extension": f["extension"],
        "mushroom_type": f.get("mushroom_type", ""),
        "product_id": f.get("product_id", ""),
        "tenant_id": f.get("tenant_id", "default"),
        "kb_id": f.get("kb_id", "default"),
        "status": f.get("status", "uploaded"),
        "error": f.get("error"),
        "clean_state": clean_state,
        "has_clean": has_clean,
        "chunk_count": chunk_cnt,
        "created_at": f.get("created_at", ""),
    }


def _detect_file_type(extension: str) -> str:
    ext = extension.lower()
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "unknown"


def _purge_milvus_for_file(f: dict) -> int:
    name = f.get("name", "")
    name_unique = db.count_files_by_name(name) <= 1 if name else False
    total = mc.purge_file_vectors(
        f["id"], f["type"], name, name_unique,
        tenant_id=f.get("tenant_id"), kb_id=f.get("kb_id"),
    )
    return total


def _delete_local_artifacts(f: dict) -> None:
    """Unlink a deleted file's on-disk artifacts (best effort, fast).

    Runs synchronously in the DELETE request so the file's SQLite row and local
    files vanish together in milliseconds; the slower Milvus vector purge is
    delegated to the ``purge_file_vectors`` Celery task.
    """
    stored = f.get("stored_path", "")
    if stored and db.count_files_by_stored_path(stored) <= 1:
        sp = Path(stored)
        if sp.exists():
            try:
                sp.unlink()
            except OSError:
                _log.warning("Failed to delete stored upload %s", stored)
    out_md = Path(app_config.output_dir_abs) / f"{f['id']}.md"
    if out_md.exists():
        try:
            out_md.unlink()
        except OSError:
            _log.warning("Failed to delete converted markdown %s", out_md)
    out_path = f.get("output_path")
    if out_path:
        p = Path(out_path)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                _log.warning("Failed to delete normalized image %s", out_path)


@app.post("/api/files/upload", response_model=models.UploadResponse,
          dependencies=[Depends(_require_api_key)])
async def upload_files(
    files: list[UploadFile] = File(...),
    mushroom_type: str = Form(""),
    product_id: str = Form(""),
    tenant_id: str = Form("default"),
    kb_id: str = Form("default"),
    force: bool = Form(False),
):
    results: list[dict] = []
    upload_dir = Path(app_config.upload_dir_abs)
    for file in files:
        if not file.filename:
            continue
        ext = Path(file.filename).suffix.lower()
        file_type = _detect_file_type(ext)
        if file_type == "unknown":
            raise HTTPException(400, f"Unsupported file type: {ext}")
        cap = (app_config.app.max_image_size_mb if file_type == "image"
               else app_config.app.max_file_size_mb) * 1024 * 1024
        temp_name = f"{uuid.uuid4().hex}{ext}"
        temp_path = upload_dir / temp_name
        digest = hashlib.sha256()
        total = 0
        try:
            # Stream to disk in 1MB chunks instead of buffering the whole file.
            async with aio_open(temp_path, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap:
                        raise HTTPException(
                            413,
                            f"{file.filename} exceeds {cap // (1024 * 1024)}MB limit",
                        )
                    digest.update(chunk)
                    await out.write(chunk)
        except HTTPException:
            temp_path.unlink(missing_ok=True)
            raise
        file_sha256 = digest.hexdigest()
        stored_name = f"{file_sha256}{ext}"
        stored_path = upload_dir / stored_name
        existing = db.find_duplicate_file(
            file_sha256,
            tenant_id=tenant_id, kb_id=kb_id, product_id=product_id,
        )
        if existing and not force:
            temp_path.unlink(missing_ok=True)
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "该租户编号、知识库编号和产品编号下已存在相同内容的文档，如有需要请修改产品编号后重新上传",
                    "existing": _file_to_info(existing),
                },
            )
        try:
            if stored_path.exists():
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, stored_path)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise
        info = db.insert_file(
            name=file.filename,
            stored_path=str(stored_path),
            size=total,
            file_type=file_type,
            extension=ext,
            mushroom_type=mushroom_type,
            product_id=product_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            sha256=file_sha256,
        )
        results.append(_file_to_info(info))
    _log.info("Uploaded %d files", len(results))
    return {"files": results}


@app.get("/api/files", response_model=models.FileListResponse)
async def list_files(
    status: str = Query(""),
    type: str = Query(""),
    tenant_id: str = Query("default"),
    kb_id: str = Query("default"),
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    files, total = db.list_files(
        status=status, file_type=type, page=page, limit=limit,
        tenant_id=tenant_id or None, kb_id=kb_id or None,
        q=q.strip(),
    )
    # Batch-fetch chunk counts to avoid N+1 queries per text file
    text_ids = [f["id"] for f in files if f.get("type") == "text"]
    counts = db.get_chunk_counts_batch(text_ids) if text_ids else {}
    results = []
    clean_states = db.get_clean_states_batch(text_ids) if text_ids else {}
    clean_flags = db.get_clean_flags_batch(text_ids) if text_ids else {}
    for f in files:
        info = _file_to_info(
            f,
            clean_state=clean_states.get(f["id"], "raw"),
            has_clean=clean_flags.get(f["id"], False),
        )
        info["chunk_count"] = counts.get(f["id"], 0)
        results.append(info)
    return {"files": results, "total": total}


@app.get("/api/files/{file_id}", response_model=models.FileInfo)
async def get_file_info(file_id: str):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    conv = db.get_conversion(file_id)
    clean_state = conv.get("clean_state", "raw") if conv else "raw"
    has_clean = False
    if conv and clean_state in ("cleaned", "edited"):
        raw_md = conv.get("raw_markdown") or conv.get("markdown") or ""
        has_clean = clean_state == "cleaned" or (conv.get("markdown") or "") != raw_md
    return _file_to_info(f, clean_state=clean_state, has_clean=has_clean)


@app.put("/api/files/{file_id}/meta", response_model=models.FileInfo,
           dependencies=[Depends(_require_api_key)])
async def update_file_meta(file_id: str, body: models.FileMetaUpdate):
    """Partial-update file metadata (mushroom_type / product_id / tenant_id /
    kb_id).

    If the file was already ingested (status='done') and any field changed,
    status is flipped to 'chunked' and staging is cleared so the next ingest
    publishes vectors with the new metadata atomically. Old data remains
    searchable until then.
    """
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")

    changed = db.update_file_meta(
        file_id,
        mushroom_type=body.mushroom_type,
        product_id=body.product_id,
        tenant_id=body.tenant_id,
        kb_id=body.kb_id,
    )
    if not changed:
        return _file_to_info(db.get_file(file_id))

    # Metadata affects Milvus entity fields; mark for re-ingest if stale.
    updated = db.get_file(file_id)
    if updated.get("status") == "done":
        db.clear_staging(file_id)
        db.update_file_status(file_id, "chunked")

    return _file_to_info(db.get_file(file_id))


@app.delete("/api/files/{file_id}", response_model=models.DeleteResponse,
            dependencies=[Depends(_require_api_key)])
async def delete_file(file_id: str):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    # Dedup insurance: run BEFORE removing the SQLite row — this file may be the
    # only Milvus copy of parent sections that other files were deduped against;
    # if so, those files must be re-ingested or the shared content silently
    # vanishes.
    dependents = dedup.invalidate_dependents(file_id)
    # Capture BEFORE the row disappears: the async purge can no longer count
    # same-named siblings once this file's row is gone.
    name_unique = db.count_files_by_name(f.get("name", "")) <= 1
    # Remove the SQLite row + local artifacts synchronously (fast) so the file
    # vanishes from every list the instant this returns. The Milvus vector purge
    # (delete + flush + load, seconds) runs in the background — it must not
    # block the DELETE response, which is why deletes used to feel unsynced.
    _delete_local_artifacts(f)
    db.delete_file(file_id)
    try:
        _dispatch_task(
            "purge_file_vectors", file_id, f["type"], f.get("name", ""), name_unique,
            f.get("tenant_id", "default"), f.get("kb_id", "default"),
        )
    except Exception:
        _log.warning("Failed to enqueue background vector purge for %s", file_id, exc_info=True)
    _log.info("Deleted file: %s (dedup dependents re-ingest: %s)", f["name"], dependents)
    return {"deleted": True, "dependents": dependents}


@app.post("/api/convert/{file_id}", response_model=models.TaskResponse,
          dependencies=[Depends(_require_api_key)])
async def trigger_convert(file_id: str):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    if f["type"] != "text":
        raise HTTPException(400, "Only text-type files can be converted")
    if f.get("status") in ("converting", "cleaning"):
        # Same file already being converted (double click / impatient repeat):
        # reject instead of enqueueing a second identical conversion.
        raise HTTPException(409, "该文件正在转换或清洗中，请等待完成后再试")
    from tasks.celery_worker import convert_document
    task = convert_document.delay(file_id)
    # Write the in-progress status synchronously (the worker also sets it, but
    # only when it actually starts) so the frontend's post-submit refresh shows
    # "转换中" immediately instead of racing the queue latency.
    db.update_file_status(file_id, "converting")
    return {"task_id": task.id}


@app.post("/api/clean/{file_id}", response_model=models.TaskResponse,
          dependencies=[Depends(_require_api_key)])
async def trigger_clean(file_id: str):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    if f["type"] != "text":
        raise HTTPException(400, "Only text-type files can be cleaned")
    if f.get("status") in ("converting", "cleaning", "chunking", "ingesting"):
        raise HTTPException(409, "该文件正在处理中，请等待当前任务完成后再清洗")
    conv = db.get_conversion(file_id, include_raw=False)
    if not conv:
        raise HTTPException(409, "请先转换文档，再执行清洗")
    from tasks.celery_worker import clean_document
    task = clean_document.delay(file_id)
    db.update_file_status(file_id, "cleaning")
    return {"task_id": task.id}


@app.get("/api/converted/{file_id}")
async def get_converted(file_id: str):
    conv = db.get_conversion(file_id)
    if not conv:
        raise HTTPException(404, "No conversion found")
    return {
        "file_id": file_id,
        "markdown": conv["markdown"],
        "raw_markdown": conv.get("raw_markdown") or conv["markdown"],
        "clean_state": conv.get("clean_state", "raw"),
        "clean_report": conv.get("clean_report") or {},
        "cleaner_version": conv.get("cleaner_version"),
        "cleaned_at": conv.get("cleaned_at"),
        "metadata": conv["metadata"],
    }


@app.put("/api/converted/{file_id}", response_model=models.SaveResponse,
         dependencies=[Depends(_require_api_key)])
async def update_converted(file_id: str, body: models.PreviewUpdate):
    conv = db.get_conversion(file_id)
    if not conv:
        raise HTTPException(404, "No conversion found")
    db.update_conversion_markdown(file_id, body.markdown, target=body.target)
    # Staged publishing (D11): do NOT purge old vectors / MySQL parents here.
    # Old data remains self-consistent (old markdown ↔ old MySQL ↔ old Milvus).
    # The next chunk + ingest cycle publishes a verified replacement atomically.
    f = db.get_file(file_id)
    if f:
        dedup.invalidate_dependents(file_id)
        db.update_file_status(file_id, "converted")
    return {"saved": True}


@app.post("/api/chunks/{file_id}", response_model=models.TaskResponse,
          dependencies=[Depends(_require_api_key)])
async def trigger_chunk(file_id: str, body: models.ChunkRequest):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    if f.get("status") in ("chunking", "cleaning"):
        raise HTTPException(409, "该文件正在切片或清洗中，请等待完成后再试")
    from tasks.celery_worker import chunk_document
    task = chunk_document.delay(
        file_id, body.chunk_size, body.overlap, body.max_parent_size,
        body.parent_lookback_tokens, body.child_lookback_tokens)
    db.update_file_status(file_id, "chunking")
    return {"task_id": task.id}


@app.get("/api/chunks/{file_id}", response_model=models.ChunkTreeResponse)
async def get_chunks_tree(file_id: str):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    chunks = db.get_chunks(file_id)

    # Before publish, MySQL has no active parent row yet. The authoritative
    # staged copy is used so the UI can still show the complete parent content.
    staged_by_id: dict[str, dict] = {}
    staging_json = db.get_staging(file_id)
    if staging_json:
        try:
            staged_rows = json.loads(staging_json)
            staged_by_id = {
                str(row.get("parent_id") or ""): row
                for row in staged_rows if isinstance(row, dict)
            }
        except (TypeError, ValueError):
            _log.warning("Invalid publish staging for file %s", file_id)

    # Group by parent_id; fetch authoritative title/content from MySQL.
    parents_map: dict[str, dict] = {}
    order: list[str] = []
    for c in chunks:
        key = c.get("parent_id") or f"__empty__{len(order)}"
        if key not in parents_map:
            pb = mysql_client.get_parent_content(
                key,
                tenant_id=f.get("tenant_id", "default"),
                kb_id=f.get("kb_id", "default"),
            ) if not key.startswith("__empty__") else None
            staged = staged_by_id.get(key, {})
            title = (pb["title"] or staged.get("title") or "") if pb else staged.get("title") or ""
            content = (pb["content"] or staged.get("content") or "") if pb else staged.get("content") or ""
            parents_map[key] = {
                "parent_id": c.get("parent_id") or "",
                "title": title,
                "content": content,
                "size": count_tokens(content) if content else int(c.get("parent_size") or 0),
                "children": [],
            }
            order.append(key)
        parents_map[key]["children"].append({
            "id": c["id"],
            "content": c["child_content"],
            "size": c["child_size"],
            "index": c["chunk_index"],
        })
    parents = [parents_map[k] for k in order]
    for p in parents:
        p["children"].sort(key=lambda x: x["index"])
        if not p["content"]:
            # Compatibility fallback for legacy chunks created before staged
            # publishing; this is display-only, not authoritative RAG content.
            p["content"] = "\n\n".join(
                child["content"] for child in p["children"]
            )
            p["size"] = count_tokens(p["content"])
    child_count = len(chunks)
    avg = sum(c["child_size"] for c in chunks) / child_count if child_count > 0 else 0
    return {
        "file_id": file_id,
        "file_name": f["name"],
        "parents": parents,
        "parent_count": len(parents),
        "child_count": child_count,
        "avg_child_size": round(avg, 1),
    }


@app.put("/api/chunks/{file_id}", response_model=models.SaveResponse,
         dependencies=[Depends(_require_api_key)])
async def update_chunks(file_id: str, body: models.ChunkUpdateRequest):
    """Delete or merge child chunks in SQLite, then mark for re-ingest.

    Staged publishing (D11): no direct Milvus writes here. The next ingest
    cycle inserts new-version vectors, verifies, then commits MySQL and cleans
    up stale vectors — keeping old data consistent until replacement is ready.
    """
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    if f["type"] != "text":
        raise HTTPException(400, "Only text-type files have chunks")
    if not body.chunk_ids:
        raise HTTPException(400, "chunk_ids is required")

    existing = db.get_chunks(file_id)
    by_id = {c["id"]: c for c in existing}
    requested = [cid for cid in body.chunk_ids if cid in by_id]
    if len(requested) != len(body.chunk_ids):
        raise HTTPException(400, "chunk_ids not found for this file")

    if body.action == "delete":
        sql_deleted = db.delete_chunks(file_id, requested)
        db.clear_staging(file_id)
        db.update_file_status(file_id, "chunked")
        return {"saved": True, "count": sql_deleted, "reingest": True}

    if body.action == "merge":
        if len(requested) < 2:
            raise HTTPException(400, "merge requires at least 2 chunks")
        rows = [by_id[cid] for cid in requested]
        rows.sort(key=lambda c: c["chunk_index"])
        survivor = rows[0]
        other_ids = [r["id"] for r in rows[1:]]
        merged_text = "\n\n".join(r["child_content"] for r in rows)
        db.update_chunk_content(
            file_id, survivor["id"], merged_text,
            child_size=count_tokens(merged_text),
        )
        if other_ids:
            db.delete_chunks(file_id, other_ids)
        db.clear_staging(file_id)
        db.update_file_status(file_id, "chunked")
        return {
            "saved": True,
            "count": len(other_ids),
            "merged_id": survivor["id"],
            "reingest": True,
        }

    return {"saved": True}


@app.post("/api/ingest/{file_id}", response_model=models.TaskResponse,
          dependencies=[Depends(_require_api_key)])
async def trigger_ingest(file_id: str):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    if f.get("status") in ("ingesting", "cleaning"):
        raise HTTPException(409, "该文件正在入库或清洗中，请等待完成后再试")
    if f.get("status") == "done":
        raise HTTPException(
            409,
            "该文件已完成入库；请先修改内容或元数据，再重新入库",
        )
    from tasks.celery_worker import ingest_document
    task = ingest_document.delay(file_id)
    db.update_file_status(file_id, "ingesting")
    return {"task_id": task.id}


@app.post("/api/ingest-image/{file_id}", response_model=models.TaskResponse,
          dependencies=[Depends(_require_api_key)])
async def trigger_ingest_image(file_id: str):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    if f.get("status") == "ingesting":
        raise HTTPException(409, "该文件正在入库中，请等待完成后再试")
    from tasks.celery_worker import ingest_image
    task = ingest_image.delay(file_id)
    db.update_file_status(file_id, "ingesting")
    return {"task_id": task.id}


@app.post("/api/ingest-full/{file_id}", response_model=models.TaskResponse,
          dependencies=[Depends(_require_api_key)])
async def trigger_ingest_full(file_id: str, body: models.IngestFullRequest):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    if f.get("status") in ("converting", "cleaning", "chunking", "ingesting"):
        raise HTTPException(409, "该文件正在处理中（转换/清洗/切片/入库），请等待完成后再试")
    from tasks.celery_worker import ingest_full_pipeline
    task = ingest_full_pipeline.delay(
        file_id, body.chunk_size, body.overlap, body.max_parent_size,
        body.parent_lookback_tokens, body.child_lookback_tokens)
    # Text pipeline starts at convert, image pipeline is a single ingest.
    db.update_file_status(file_id, "converting" if f["type"] == "text" else "ingesting")
    return {"task_id": task.id}


@app.get("/api/tasks/{task_id}", response_model=models.TaskStatus)
async def get_task_status(task_id: str):
    from tasks.celery_worker import get_task_status as _get_status
    return _get_status(task_id)


@app.get("/api/milvus/collections")
async def milvus_collections():
    """List collections that already exist; the UI never creates one implicitly."""
    return {
        "collections": mc.list_collections(),
        "default": app_config.milvus.text_collection,
    }


@app.get("/api/milvus/stats")
async def milvus_stats(collection: str = Query("")):
    if collection:
        if not mc.collection_exists(collection):
            raise HTTPException(404, f"Collection '{collection}' 不存在")
        return mc.get_stats(collection)
    text_stats = mc.get_stats(app_config.milvus.text_collection)
    image_stats = mc.get_stats(app_config.milvus.image_collection)
    return {"text": text_stats, "image": image_stats}


@app.get("/api/milvus/documents", response_model=models.MilvusDocListResponse)
async def milvus_documents(
    collection: str = Query(app_config.milvus.text_collection),
    source: str = Query(""),
    q: str = Query("", max_length=200),
    file_name: str = Query("", max_length=255),
    tenant_id: str = Query(""),
    kb_id: str = Query(""),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    if not mc.collection_exists(collection):
        raise HTTPException(404, f"Collection '{collection}' 不存在")

    pk_field, cast_pk = collection_primary_key(collection)
    source_ids: list[str] = []
    file_name_truncated = False
    if file_name.strip():
        # Milvus does not store filenames. Resolve filename text to source IDs
        # through the authoritative metadata table, then filter Milvus by source.
        source_ids, matched_total = db.get_file_ids_by_name(
            file_name,
            tenant_id=tenant_id or None,
            kb_id=kb_id or None,
            limit=500,
        )
        file_name_truncated = matched_total > len(source_ids)
        if not source_ids:
            return {
                "collection": collection, "documents": [], "total": 0,
                "page": page, "limit": limit,
                "schema_fields": [f.get("name", "") for f in mc.get_collection_schema(collection)["fields"]],
                "pk_field": pk_field,
                "file_name_truncated": False,
            }

    try:
        docs, total, schema_fields = mc.query_collection_documents(
            collection,
            q=q.strip(),
            source=source.strip(),
            source_ids=source_ids,
            tenant_id=tenant_id.strip(),
            kb_id=kb_id.strip(),
            page=page,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    schema_field_names = list(schema_fields)
    items = []
    for d in docs:
        src = d.get("source") or d.get("source_file_id") or d.get("file_id") or ""
        src_file = db.get_file(src) if src else None
        content = d.get("content") or d.get("description") or d.get("text") or ""
        items.append({
            "id": str(cast_pk(d.get(pk_field, d.get("id", "")))),
            "pk": str(cast_pk(d.get(pk_field, d.get("id", "")))),
            "content": content,
            "image_url": d.get("image_url", ""),
            "description": d.get("description", ""),
            "source": src,
            "source_name": (src_file["name"] if src_file else src),
            "mushroom_type": d.get("mushroom_type", ""),
            "product_id": d.get("product_id", ""),
            "tenant_id": d.get("tenant_id", ""),
            "kb_id": d.get("kb_id", ""),
            "parent_id": d.get("parent_id", ""),
            "chunk_index": d.get("chunk_index"),
            "doc_version": d.get("doc_version"),
            "data": d.get("data", {}),
        })
    return {
        "collection": collection, "documents": items, "total": total,
        "page": page, "limit": limit,
        "schema_fields": schema_field_names,
        "pk_field": pk_field,
        "file_name_truncated": file_name_truncated,
    }


@app.delete("/api/milvus/documents/{doc_id}", response_model=models.DeleteResponse,
            dependencies=[Depends(_require_api_key)])
async def milvus_delete_document(
    doc_id: str,
    collection: str = Query(app_config.milvus.text_collection),
    tenant_id: str = Query("default"),
    kb_id: str = Query("default"),
):
    pk_field, cast_pk = collection_primary_key(collection)
    expr = mx.and_expr(mx.eq(pk_field, cast_pk(doc_id)))
    if has_scalar_field(collection, "tenant_id") and has_scalar_field(collection, "kb_id"):
        expr = mx.and_expr(expr, mx.eq("tenant_id", tenant_id),
                           mx.eq("kb_id", kb_id))
    count = mc.delete_by_expr(expr, collection)
    return {"deleted": True, "count": count}


@app.post("/api/milvus/batch-delete", dependencies=[Depends(_require_api_key)])
async def milvus_batch_delete(
    ids: list[str] = [],
    collection: str = Query(app_config.milvus.text_collection),
    tenant_id: str = Query("default"),
    kb_id: str = Query("default"),
):
    """Batch delete documents by IDs."""
    if not ids:
        raise HTTPException(400, "ids list is required")
    pk_field, cast_pk = collection_primary_key(collection)
    expr = mx.and_expr(mx.in_expr(pk_field, [cast_pk(i) for i in ids]))
    if has_scalar_field(collection, "tenant_id") and has_scalar_field(collection, "kb_id"):
        expr = mx.and_expr(expr, mx.eq("tenant_id", tenant_id),
                           mx.eq("kb_id", kb_id))
    count = mc.delete_by_expr(expr, collection)
    return {"deleted": True, "count": count}


@app.post("/api/milvus/purge-file/{file_id}", response_model=models.DeleteResponse,
          dependencies=[Depends(_require_api_key)])
async def milvus_purge_file(file_id: str):
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    total = _purge_milvus_for_file(f)
    return {"deleted": True, "count": total}


@app.get("/api/config")
async def api_config():
    """Frontend-facing limits and feature flags."""
    return {
        "max_file_size_mb": app_config.app.max_file_size_mb,
        "max_image_size_mb": app_config.app.max_image_size_mb,
        "vlm_enabled": vlm.is_enabled(),
        "api_key_required": bool(app_config.security.api_key),
    }
