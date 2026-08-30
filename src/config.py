"""Configuration loader: reads .env into typed dataclasses."""
from dataclasses import dataclass, field
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes")


@dataclass
class MilvusConfig:
    host: str = field(default_factory=lambda: _env("MILVUS_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("MILVUS_PORT", 19530))
    user: str = field(default_factory=lambda: _env("MILVUS_USER", ""))
    password: str = field(default_factory=lambda: _env("MILVUS_PASSWORD", ""))
    secure: bool = field(default_factory=lambda: _env_bool("MILVUS_SECURE"))
    # NOTE: `db` is NOT passed to connections.connect (see src/milvus_client.py);
    # the rag-shared + wiki collections live in Milvus' default database. The
    # MILVUS_DB value is retained for reference only — do not enable until the
    # collections are actually migrated into that database.
    db: str = field(default_factory=lambda: _env("MILVUS_DB", "mushroom_rag"))
    text_collection: str = field(default_factory=lambda: _env("MILVUS_TEXT_COLLECTION", "mushroom_knowledge"))
    image_collection: str = field(default_factory=lambda: _env("MILVUS_IMAGE_COLLECTION", "mushroom_images"))
    # Wiki-domain collection (ROADMAP D1): separate from the rag-shared ones.
    wiki_collection: str = field(default_factory=lambda: _env("WIKI_MILVUS_COLLECTION", "wiki_knowledge"))
    dim: int = field(default_factory=lambda: _env_int("MILVUS_DIM", 512))
    health_check_interval: int = field(default_factory=lambda: _env_int("MILVUS_HEALTH_CHECK_INTERVAL", 300))


@dataclass
class RedisConfig:
    host: str = field(default_factory=lambda: _env("REDIS_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("REDIS_PORT", 6379))
    password: str = field(default_factory=lambda: _env("REDIS_PASSWORD", ""))
    db: int = field(default_factory=lambda: _env_int("REDIS_DB", 1))


@dataclass
class AppConfig:
    upload_dir: str = field(default_factory=lambda: _env("UPLOAD_DIR", "uploads"))
    output_dir: str = field(default_factory=lambda: _env("OUTPUT_DIR", "outputs"))
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "data"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/pipeline.db"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    max_file_size_mb: int = field(default_factory=lambda: _env_int("MAX_FILE_SIZE_MB", 50))
    max_image_size_mb: int = field(default_factory=lambda: _env_int("MAX_IMAGE_SIZE_MB", 10))
    chunk_size_default: int = field(default_factory=lambda: _env_int("CHUNK_SIZE_DEFAULT", 300))
    chunk_overlap_default: int = field(default_factory=lambda: _env_int("CHUNK_OVERLAP_DEFAULT", 40))
    parent_lookback_default: int = field(default_factory=lambda: _env_int("PARENT_LOOKBACK_DEFAULT", 80))
    max_parent_size_default: int = field(default_factory=lambda: _env_int("MAX_PARENT_SIZE_DEFAULT", 2000))
    recover_stale_seconds: int = field(default_factory=lambda: _env_int("RECOVER_STALE_SECONDS", 3600))
    # Trash retention (ROADMAP P3-T2): soft-deleted pages are purged permanently
    # after this many days (vectors + rows), via the cleanup task.
    trash_retention_days: int = field(default_factory=lambda: _env_int("TRASH_RETENTION_DAYS", 30))
    # Object storage (ROADMAP P4-T5): "local" (data/attachments) or "s3" (S3/MinIO).
    storage_backend: str = field(default_factory=lambda: _env("STORAGE_BACKEND", "local"))
    s3_endpoint: str = field(default_factory=lambda: _env("S3_ENDPOINT", ""))
    s3_bucket: str = field(default_factory=lambda: _env("S3_BUCKET", "wiki"))
    s3_access_key: str = field(default_factory=lambda: _env("S3_ACCESS_KEY", ""))
    s3_secret_key: str = field(default_factory=lambda: _env("S3_SECRET_KEY", ""))
    sentence_transformers_model: str = field(default_factory=lambda: _env("SENTENCE_TRANSFORMERS_MODEL", "BAAI/bge-small-zh-v1.5"))
    embedder_local_files_only: bool = field(default_factory=lambda: _env_bool("EMBEDDER_LOCAL_FILES_ONLY", True))
    clip_model: str = field(default_factory=lambda: _env("CLIP_MODEL", "OFA-Sys/chinese-clip-vit-base-patch16"))
    # Auto-start the Celery worker as a child of the API process when the API
    # boots (dev convenience: one click = API + worker). Set false in Docker
    # (separate worker service) or tests. See main.py `_start_celery_worker`.
    auto_start_worker: bool = field(default_factory=lambda: _env_bool("AUTO_START_WORKER", True))
    # MinerU conversion backend (pdf/docx/pptx) — an external mineru-api
    # service. Start it once with scripts/start_mineru_api.bat, then point
    # MINERU_BASE_URL at it. When MINERU_ENABLED=true, pdf/docx/pptx are routed
    # to MinerU (GPU pipeline); html/md/txt use the built-in plain-text path.
    # If the API is unreachable/disabled, pdf/docx/pptx conversion raises a
    # clear error — there is no fallback engine anymore.
    mineru_enabled: bool = field(default_factory=lambda: _env_bool("MINERU_ENABLED", True))
    mineru_base_url: str = field(default_factory=lambda: _env("MINERU_BASE_URL", "http://127.0.0.1:8010"))
    # Backend passed to mineru-api: "pipeline" (fast, hallucination-free,
    # 4GB+ VRAM) or "hybrid-engine" (higher accuracy, minimal 8GB VRAM).
    mineru_backend: str = field(
        default_factory=lambda: _env("MINERU_BACKEND", "pipeline").lower()
    )
    # Hard cap for one MinerU sync parse call — must stay under the Celery
    # task soft time limit (1500s) so the HTTP wait never gets killed first.
    mineru_timeout_seconds: int = field(default_factory=lambda: _env_int("MINERU_TIMEOUT_SECONDS", 1500))
    # Auto-start the mineru-api child process when the API boots (same pattern
    # as the Celery worker): one command = API + worker + MinerU service. Set
    # false in Docker/tests, or when you manage mineru-api yourself.
    mineru_auto_start: bool = field(default_factory=lambda: _env_bool("MINERU_AUTO_START", True))
    # Path to the mineru-api executable (mineru_env venv). Falls back to
    # "mineru-api" on PATH when this path does not exist.
    mineru_executable: str = field(
        default_factory=lambda: _env("MINERU_EXECUTABLE", r"D:\my_env\mineru_env\Scripts\mineru-api.exe")
    )
    mineru_return_content_list: bool = field(
        default_factory=lambda: _env_bool("MINERU_RETURN_CONTENT_LIST", True)
    )
    mineru_return_middle_json: bool = field(
        default_factory=lambda: _env_bool("MINERU_RETURN_MIDDLE_JSON", True)
    )
    heading_hierarchy_mode: str = field(
        default_factory=lambda: _env("HEADING_HIERARCHY_MODE", "auto").lower()
    )


@dataclass
class LLMConfig:
    """Vision-LLM config for automatic image description (reserved keys in .env)."""
    base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", ""))
    api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    model: str = field(default_factory=lambda: _env("LLM_MODEL", ""))
    enabled: bool = field(default_factory=lambda: _env_bool("VLM_ENABLED", True))
    timeout_seconds: int = field(default_factory=lambda: _env_int("VLM_TIMEOUT_SECONDS", 30))
    max_tokens: int = field(default_factory=lambda: _env_int("VLM_MAX_TOKENS", 512))


@dataclass
class PostgresConfig:
    """Wiki-domain PostgreSQL (ROADMAP P0-T3/P0-T6)."""
    host: str = field(default_factory=lambda: _env("POSTGRES_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("POSTGRES_PORT", 5433))
    user: str = field(default_factory=lambda: _env("POSTGRES_USER", "wiki"))
    password: str = field(default_factory=lambda: _env("POSTGRES_PASSWORD", "wiki"))
    db: str = field(default_factory=lambda: _env("POSTGRES_DB", "wiki"))
    # Dedicated test database on the same Postgres (ROADMAP P0-T6): wiki tests
    # run against real Postgres — never SQLite — to surface dialect differences.
    test_db: str = field(default_factory=lambda: _env("POSTGRES_TEST_DB", "wiki_test"))
    pool_size: int = field(default_factory=lambda: _env_int("POSTGRES_POOL_SIZE", 10))
    max_overflow: int = field(default_factory=lambda: _env_int("POSTGRES_MAX_OVERFLOW", 20))

    @property
    def dsn(self) -> str:
        return f"postgresql+psycopg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


@dataclass
class MysqlConfig:
    """MySQL for RAG parent blocks (rag_parent_block)."""
    host: str = field(default_factory=lambda: _env("MYSQL_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("MYSQL_PORT", 3307))
    user: str = field(default_factory=lambda: _env("MYSQL_USER", "root"))
    password: str = field(default_factory=lambda: _env("MYSQL_PASSWORD", ""))
    database: str = field(default_factory=lambda: _env("MYSQL_DATABASE", "rag_test"))


@dataclass
class MarkdownCleanConfig:
    """Structure-repair rules for the conversion->chunk gap.

    All knobs are environment-driven so the rule set can be tuned without
    changing code. ``noise_terms`` is held outside the cleaner module on
    purpose: it is configuration, not source code.
    """
    force_single_h1: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_FORCE_SINGLE_H1", False))
    max_heading_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MAX_HEADING_CHARS", 120))
    merge_repeated_headings: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_MERGE_REPEATED_HEADINGS", True))
    max_repeated_gap_lines: int = field(default_factory=lambda: _env_int("MD_CLEAN_MAX_REPEATED_GAP_LINES", 16))
    max_gap_text_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MAX_GAP_TEXT_CHARS", 20))
    max_repeated_body_text_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MAX_REPEATED_BODY_TEXT_CHARS", 30))
    require_image_gap: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_REQUIRE_IMAGE_GAP", True))
    require_image_body: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_REQUIRE_IMAGE_BODY", True))
    allow_inline_bold_heading: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_ALLOW_INLINE_BOLD_HEADING", False))
    remove_page_residue: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_REMOVE_PAGE_RESIDUE", True))
    split_paged_long_lines: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_SPLIT_PAGED_LONG_LINES", True))
    min_paged_split_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MIN_PAGED_SPLIT_CHARS", 100))
    convert_numbered_bodies: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_CONVERT_NUMBERED_BODIES", True))
    max_numbered_list_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MAX_NUMBERED_LIST_CHARS", 360))
    dedupe_repeated_text: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_DEDUPE_REPEATED_TEXT", True))
    min_duplicate_text_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MIN_DUPLICATE_TEXT_CHARS", 14))
    normalize_ocr_spacing: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_NORMALIZE_OCR_SPACING", True))
    clean_inline_tags: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_CLEAN_INLINE_TAGS", True))
    normalize_list_bullets: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_NORMALIZE_LIST_BULLETS", True))
    clean_invisible_chars: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_CLEAN_INVISIBLE_CHARS", True))
    collapse_whitespace: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_COLLAPSE_WHITESPACE", True))
    clean_external_links: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_CLEAN_EXTERNAL_LINKS", True))
    merge_soft_wraps: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_MERGE_SOFT_WRAPS", True))
    merge_broken_sentences: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_MERGE_BROKEN_SENTENCES", True))
    max_merged_line_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MAX_MERGED_LINE_CHARS", 600))
    min_broken_merge_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MIN_BROKEN_MERGE_CHARS", 8))
    remove_ocr_garbage_fragments: bool = field(default_factory=lambda: _env_bool("MD_CLEAN_REMOVE_OCR_GARBAGE", True))
    min_ocr_garbage_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MIN_OCR_GARBAGE_CHARS", 4))
    max_ocr_garbage_chars: int = field(default_factory=lambda: _env_int("MD_CLEAN_MAX_OCR_GARBAGE_CHARS", 48))
    noise_terms: list[str] = field(
        default_factory=lambda: [
            t.strip() for t in _env(
                "MD_CLEAN_NOISE_TERMS",
                "THANKS,BUSINESS,CONTENTS,TABLE OF CONTENTS,目录,"
                "01,02,03,04,05,06,07,08,09,10",
            ).split(",") if t.strip()
        ]
    )


@dataclass
class SecurityConfig:
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"))
    api_key: str = field(default_factory=lambda: _env("API_KEY", ""))
    # JWT auth for wiki users (Phase 2). Empty secret disables the token
    # endpoints (they return 503) — set a strong value in production.
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", ""))
    jwt_expire_minutes: int = field(default_factory=lambda: _env_int("JWT_EXPIRE_MINUTES", 10080))
    # Open registration (P2-T4 hardening): set false in production to disable
    # anonymous /api/wiki/auth/register (invite-only onboarding).
    allow_registration: bool = field(default_factory=lambda: _env_bool("ALLOW_REGISTRATION", False))
    # Login anti-enumeration (P4-T2): after this many failed attempts a username
    # is locked out for login_lockout_seconds (Redis-backed, per-username).
    login_max_attempts: int = field(default_factory=lambda: _env_int("LOGIN_MAX_ATTEMPTS", 5))
    login_lockout_seconds: int = field(default_factory=lambda: _env_int("LOGIN_LOCKOUT_SECONDS", 900))
    # Generic per-IP rate limit (P4-T2): max requests/minute from one IP; 0 = off.
    ip_rate_limit_per_minute: int = field(default_factory=lambda: _env_int("IP_RATE_LIMIT_PER_MINUTE", 120))


@dataclass
class Config:
    milvus: MilvusConfig = field(default_factory=MilvusConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    app: AppConfig = field(default_factory=AppConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    mysql: MysqlConfig = field(default_factory=MysqlConfig)
    clean: MarkdownCleanConfig = field(default_factory=MarkdownCleanConfig)

    @property
    def redis_url(self) -> str:
        pw = f":{self.redis.password}@" if self.redis.password else ""
        return f"redis://{pw}{self.redis.host}:{self.redis.port}/{self.redis.db}"

    @property
    def db_path_abs(self) -> str:
        p = Path(self.app.db_path)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    @property
    def data_dir_abs(self) -> str:
        p = Path(self.app.data_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    @property
    def attachments_dir_abs(self) -> str:
        """Local attachment storage (ROADMAP P3-T3; object storage in Phase 4)."""
        p = Path(self.data_dir_abs) / "attachments"
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    @property
    def upload_dir_abs(self) -> str:
        p = Path(self.app.upload_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    @property
    def output_dir_abs(self) -> str:
        p = Path(self.app.output_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return str(p)


config = Config()
