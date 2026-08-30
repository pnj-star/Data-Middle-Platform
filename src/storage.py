"""Object storage abstraction (ROADMAP P4-T5).

`get_storage()` returns LocalStorage (data/attachments) by default, or
S3Storage (S3 / MinIO) when `STORAGE_BACKEND=s3`. Attachment keys are the
attachment row ids; absolute-path keys written by older versions are still
readable/removable (LocalStorage treats them as direct paths).
"""
from __future__ import annotations

from pathlib import Path

from src.config import config as app_config


def get_storage():
    """Storage backend for attachment files (singleton per call is cheap)."""
    if app_config.app.storage_backend == "s3":
        return S3Storage(
            bucket=app_config.app.s3_bucket,
            endpoint=app_config.app.s3_endpoint,
            access_key=app_config.app.s3_access_key,
            secret_key=app_config.app.s3_secret_key,
        )
    return LocalStorage(Path(app_config.attachments_dir_abs))


class LocalStorage:
    """Files under data/attachments (the P3-T3 default)."""

    def __init__(self, base_dir: Path):
        self.base = base_dir

    def _path(self, key: str) -> Path:
        p = Path(key)
        return p if p.is_absolute() else self.base / key

    def save(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            pass


_s3_clients: dict = {}


class S3Storage:
    """S3 / MinIO-compatible object storage via boto3.

    The boto3 client is cached per (endpoint, access_key) so repeated
    `get_storage()` calls don't rebuild it, and retries are configured at the
    SDK level (P4-T2 修复).
    """

    def __init__(self, bucket: str, endpoint: str, access_key: str, secret_key: str):
        cache_key = (endpoint, access_key)
        if cache_key not in _s3_clients:
            import boto3
            from botocore.config import Config

            _s3_clients[cache_key] = boto3.client(
                "s3",
                endpoint_url=endpoint or None,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(retries={"max_attempts": 3, "mode": "standard"}),
            )
        self._client = _s3_clients[cache_key]
        self._bucket = bucket

    def save(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def read(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)
