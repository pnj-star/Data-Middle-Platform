"""Document conversion engine: MinerU (pdf/docx/pptx) -> Markdown, plus a
lightweight built-in path for plain-text formats (html/md/txt)."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .config import config as app_config
from .logging_config import get_logger

_log = get_logger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".md", ".txt"}
# Files routed to the MinerU API (GPU layout/OCR/formula/table extraction).
# html/md/txt need no document model and use the built-in plain-text path.
MINERU_EXTENSIONS = {".pdf", ".docx", ".pptx"}
PLAIN_EXTENSIONS = {".html", ".md", ".txt"}


@dataclass
class ConvertResult:
    markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _validate(file_path: str | Path) -> Path:
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"File not found: {fp}")
    if fp.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {fp.suffix}. Supported: {SUPPORTED_EXTENSIONS}")
    limit_mb = app_config.app.max_file_size_mb
    size_mb = fp.stat().st_size / (1024 * 1024)
    if size_mb > limit_mb:
        raise ValueError(f"File too large: {size_mb:.1f}MB > {limit_mb}MB")
    return fp


class _MinerUUnavailable(RuntimeError):
    """Raised when the MinerU API cannot be reached (connection/timeout).

    Genuine parse failures (HTTP errors, empty results) raise a plain
    RuntimeError instead, so a broken pipeline never masks its own failures as
    a connectivity problem.
    """


def _uses_mineru(fp: Path) -> bool:
    """True when this file should be converted by the configured MinerU API."""
    return app_config.app.mineru_enabled and fp.suffix.lower() in MINERU_EXTENSIONS


def _mineru_parse(fp: Path, artifact_key: str | None = None) -> ConvertResult:
    """Convert one file through the mineru-api sync ``/file_parse`` endpoint.

    Uploads the file and asks for markdown + extracted images in one multipart
    POST. Images returned as base64 data-URIs are persisted under
    ``outputs/images`` (the directory ``/media/images`` serves) and the
    markdown image refs are rewritten to those URLs.
    """
    import requests as _requests

    base = app_config.app.mineru_base_url.rstrip("/")
    backend = app_config.app.mineru_backend
    timeout = app_config.app.mineru_timeout_seconds

    _log.info("MinerU parse: %s (backend=%s) via %s", fp.name, backend, base)
    try:
        with open(fp, "rb") as fh:
            resp = _requests.post(
                f"{base}/file_parse",
                files={"files": (fp.name, fh)},
                data=[
                    ("backend", backend),
                    ("parse_method", "auto"),
                    ("formula_enable", "true"),
                    ("table_enable", "true"),
                    ("return_md", "true"),
                    ("return_images", "true"),
                    *(
                        [("return_content_list", "true")]
                        if app_config.app.mineru_return_content_list
                        else []
                    ),
                    *(
                        [("return_middle_json", "true")]
                        if app_config.app.mineru_return_middle_json
                        else []
                    ),
                ],
                timeout=(30, timeout),
            )
    except _requests.exceptions.RequestException as exc:
        raise _MinerUUnavailable(f"MinerU API unreachable at {base}: {exc}") from exc

    if resp.status_code != 200:
        detail = (resp.text or "")[:500] or f"HTTP {resp.status_code}"
        raise RuntimeError(f"MinerU parse failed ({resp.status_code}): {detail}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"MinerU returned non-JSON response: {resp.text[:200]}") from exc

    results = payload.get("results") or {}
    if fp.stem not in results:
        raise RuntimeError(f"MinerU response missing result for {fp.name}")
    result = results[fp.stem]
    markdown = result.get("md_content") or ""
    if not markdown.strip():
        raise RuntimeError(f"MinerU returned empty markdown for {fp.name}")

    markdown, images = _persist_mineru_images(markdown, result.get("images") or {})
    layout_meta = _persist_layout_json(
        result.get("content_list"), result.get("middle_json"), fp,
        artifact_key=artifact_key,
    )
    meta: dict[str, Any] = {
        "title": fp.stem,
        "page_count": _pdf_page_count(fp) if fp.suffix.lower() == ".pdf" else 0,
        "images": images,
        "table_count": markdown.count("\n|"),
        "engine": "mineru",
        "backend": backend,
        **layout_meta,
    }
    _log.info("MinerU conversion done: %s, %d chars, %d images, %d tables",
              fp.name, len(markdown), len(images), meta["table_count"])
    return ConvertResult(markdown=markdown, metadata=meta)


def _persist_layout_json(content_list, middle_json, fp, *, artifact_key: str | None = None) -> dict:
    """Persist raw MinerU layout artifacts beside the converted markdown."""
    if content_list is None and middle_json is None:
        return {}
    out_dir = Path(app_config.output_dir_abs)
    out_dir.mkdir(parents=True, exist_ok=True)
    import json as _json

    stem = artifact_key or fp.stem
    meta: dict = {}
    if content_list is not None:
        if isinstance(content_list, str):
            content_list = _json.loads(content_list)
        path = out_dir / f"{stem}.content_list.json"
        path.write_text(_json.dumps(content_list, ensure_ascii=False), encoding="utf-8")
        meta["content_list_path"] = str(path)
    if middle_json is not None:
        if isinstance(middle_json, str):
            middle_json = _json.loads(middle_json)
        path = out_dir / f"{stem}.middle_json.json"
        path.write_text(_json.dumps(middle_json, ensure_ascii=False), encoding="utf-8")
        meta["middle_json_path"] = str(path)
    if meta:
        meta["hierarchy_source"] = "content_list"
        return {"layout": meta}
    return {}


def _persist_mineru_images(markdown: str, images: dict[str, str]) -> tuple[str, list[dict[str, str]]]:
    """Persist MinerU base64 images into outputs/images and rewrite markdown refs.

    MinerU returns extracted images as base64 data-URIs keyed by their filename
    and references them as ``![](images/<name>)``. We write them into the same
    ``outputs/images`` directory main.py mounts at ``/media/images`` and rewrite
    the refs to ``/media/images/<name>`` so the URLs are stable and servable.
    Returns the rewritten markdown plus the ``metadata["images"]`` entries.
    """
    out_dir = Path(app_config.output_dir_abs) / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    for name, data_uri in images.items():
        try:
            header, _, b64 = data_uri.partition(",")
            if not b64 or "base64" not in header:
                continue
            safe_name = Path(name).name
            if safe_name != name or not safe_name:  # reject path traversal
                continue
            target = out_dir / safe_name
            target.write_bytes(base64.b64decode(b64))
            saved[name] = f"/media/images/{safe_name}"
        except Exception:
            _log.warning("Failed to persist MinerU image %s", name, exc_info=True)

    def _rewrite(match: re.Match[str]) -> str:
        name = match.group(2)
        if name in saved:
            return f"{match.group(1)}{saved[name]}{match.group(3)}"
        return match.group(0)

    rewritten = re.sub(r"(!\[[^\]]*\]\()images/([^)\s]+)(\))", _rewrite, markdown)
    refs = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", rewritten)
    return rewritten, [{"alt": alt, "ref": ref} for alt, ref in refs]


def _pdf_page_count(fp: Path) -> int:
    """Page count of a PDF via PyMuPDF; 0 on any failure (informational only)."""
    try:
        import fitz

        doc = fitz.open(str(fp))
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception:
        return 0


class _HtmlToMarkdown(HTMLParser):
    """Minimal HTML -> Markdown converter (headings, lists, tables, code, ...).

    Intentionally lightweight: html uploads are converted with the stdlib
    parser rather than a document engine. ``<script>``/``<style>`` blocks are
    dropped; link ``href`` values and attribute-level styling are not kept.
    """

    _HEADING_MARKS = {
        "h1": "# ", "h2": "## ", "h3": "### ",
        "h4": "#### ", "h5": "##### ", "h6": "###### ",
    }
    _PARA_TAGS = {"p", "div", "section", "article", "header", "footer", "main"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._lines: list[str] = []
        self._text: list[str] = []      # inline text of the current block
        self._prefix: str = ""          # current block prefix (#, -, > ...)
        self._lists: list[str] = []     # marker stack ("- " / "0. " per level)
        self._in_pre = False
        self._in_cell = False
        self._skip_tag: str | None = None
        self._rows: list[list[str]] = []
        self._cells: list[str] = []
        self._cell: list[str] = []

    @property
    def markdown(self) -> str:
        text = "\n".join(self._lines) + "\n"
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    def _flush(self) -> None:
        text = "".join(self._text).strip()
        if text:
            self._lines.append(f"{self._prefix}{text}")
        self._text = []

    def _finish_cell(self) -> None:
        self._cells.append("".join(self._cell).strip())
        self._cell = []

    def _flush_table(self) -> None:
        if self._cells:
            self._rows.append(self._cells)
            self._cells = []
        if not self._rows:
            return
        width = max(len(r) for r in self._rows)
        rows = [r + [""] * (width - len(r)) for r in self._rows]
        out = [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join(["---"] * width) + " |",
        ]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        self._lines.append("\n".join(out))
        self._rows = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs or [])
        if tag in ("script", "style"):
            self._skip_tag = tag
            return
        if tag in self._HEADING_MARKS:
            self._flush()
            self._prefix = self._HEADING_MARKS[tag]
        elif tag in self._PARA_TAGS:
            self._flush()
            self._prefix = ""
        elif tag == "ul":
            self._flush()
            self._lists.append("- ")
        elif tag == "ol":
            self._flush()
            self._lists.append("0. ")
        elif tag == "li":
            self._flush()
            if self._lists and self._lists[-1].rstrip().endswith("."):
                n = int(self._lists[-1].split(".")[0]) + 1
                self._lists[-1] = f"{n}. "
            marker = self._lists[-1] if self._lists else "- "
            self._prefix = "  " * (len(self._lists) - 1) + marker
        elif tag == "blockquote":
            self._flush()
            self._prefix = "> "
        elif tag == "pre":
            self._flush()
            self._prefix = ""
            self._in_pre = True
            self._lines.append("```")
        elif tag in ("table", "tr"):
            self._flush()
        elif tag in ("td", "th"):
            self._cell = []
            self._in_cell = True
        elif tag == "img":
            attrs = dict(attrs or [])
            alt = attrs.get("alt", "") or ""
            src = attrs.get("src", "") or ""
            self._text.append(f"![{alt}]({src})")
        elif tag == "hr":
            self._flush()
            self._lines.append("---")
        elif tag == "br":
            self._text.append("\n")
        elif tag == "strong" or tag == "b":
            self._text.append("**")
        elif tag == "em" or tag == "i":
            self._text.append("*")
        elif tag == "code":
            self._text.append("`")

    def handle_endtag(self, tag: str) -> None:
        if tag == self._skip_tag:
            self._skip_tag = None
            return
        if tag == "pre":
            self._lines.append("```")
            self._in_pre = False
        elif tag in self._HEADING_MARKS or tag in self._PARA_TAGS:
            self._flush()
            self._prefix = ""
        elif tag in ("ul", "ol"):
            self._flush()
            if self._lists:
                self._lists.pop()
            self._prefix = ""
        elif tag == "li":
            self._flush()
        elif tag == "blockquote":
            self._flush()
            self._prefix = ""
        elif tag == "td" or tag == "th":
            self._finish_cell()
            self._in_cell = False
        elif tag == "tr":
            self._finish_cell()
            if self._cells:
                self._rows.append(self._cells)
                self._cells = []
        elif tag == "table":
            self._flush_table()
        elif tag == "strong" or tag == "b":
            self._text.append("**")
        elif tag == "em" or tag == "i":
            self._text.append("*")
        elif tag == "code":
            self._text.append("`")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs or [])
        if tag == "img":
            alt = attrs.get("alt", "") or ""
            src = attrs.get("src", "") or ""
            self._text.append(f"![{alt}]({src})")
        elif tag == "hr":
            self._flush()
            self._lines.append("---")
        elif tag == "br":
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_tag:
            return
        if self._in_pre:
            self._lines.extend(data.splitlines() or [""])
            return
        if self._in_cell:
            self._cell.append(data)
        else:
            self._text.append(data)


def _convert_plain(fp: Path) -> ConvertResult:
    """Convert html/md/txt without any external document engine.

    Markdown/text files pass through as-is; html files go through a small
    stdlib parser (headings/lists/tables/code kept, scripts/styles dropped).
    """
    suffix = fp.suffix.lower()
    raw = fp.read_text(encoding="utf-8", errors="replace")
    if suffix == ".html":
        parser = _HtmlToMarkdown()
        parser.feed(raw)
        parser.close()
        markdown = parser.markdown
    else:
        markdown = raw.strip() + "\n"
    img_refs = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", markdown)
    meta: dict[str, Any] = {
        "title": fp.stem,
        "page_count": 0,
        "images": [{"alt": alt, "ref": ref} for alt, ref in img_refs],
        "table_count": markdown.count("\n|"),
        "engine": "plain",
    }
    _log.info("Plain conversion done: %s, %d chars, %d images, %d tables",
              fp.name, len(markdown), len(meta["images"]), meta["table_count"])
    return ConvertResult(markdown=markdown, metadata=meta)


def convert(file_path: str | Path, *, artifact_key: str | None = None) -> ConvertResult:
    """Convert a document file to Markdown.

    pdf/docx/pptx are converted by the configured MinerU API (GPU layout/OCR/
    formula/table extraction in one pass); html/md/txt use the built-in
    plain-text path. There is no fallback engine anymore: when MinerU is
    unreachable or disabled, pdf/docx/pptx conversion raises a clear error
    instead of silently degrading.

    Args:
        file_path: Path to the source document.

    Returns:
        ConvertResult with markdown text and extracted metadata.

    Raises:
        FileNotFoundError, ValueError on invalid input.
        RuntimeError when MinerU is unavailable/disabled for pdf/docx/pptx,
        or when the MinerU parse itself fails.
    """
    fp = _validate(file_path)
    if _uses_mineru(fp):
        return _mineru_parse(fp, artifact_key=artifact_key)
    if fp.suffix.lower() in PLAIN_EXTENSIONS:
        return _convert_plain(fp)
    raise RuntimeError(
        f"No converter available for {fp.name}: MINERU_ENABLED=false or the "
        "MinerU service is unavailable/not configured for pdf/docx/pptx."
    )


def save_markdown(file_id: str, markdown: str) -> str:
    """Save converted markdown to outputs/{file_id}.md. Returns the file path."""
    out_dir = Path(app_config.output_dir_abs)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{file_id}.md"
    out_path.write_text(markdown, encoding="utf-8")
    _log.debug("Markdown saved: %s", out_path)
    return str(out_path)


def load_markdown(file_id: str) -> str | None:
    """Load saved markdown from outputs/{file_id}.md."""
    out_path = Path(app_config.output_dir_abs) / f"{file_id}.md"
    if out_path.exists():
        return out_path.read_text(encoding="utf-8")
    return None
