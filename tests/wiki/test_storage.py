"""Object storage tests (ROADMAP P4-T5): local + S3 (client mocked — no moto)."""
from __future__ import annotations

import io
import sys
import types

_fake_converter = types.ModuleType("src.converter")
_fake_converter.SUPPORTED_EXTENSIONS = {".pdf", ".md"}
sys.modules["src.converter"] = _fake_converter

import pytest  # noqa: E402

from src.config import config as app_config  # noqa: E402
from src.storage import LocalStorage, S3Storage, get_storage  # noqa: E402


class FakeS3Client:
    """In-memory stand-in for a boto3 S3 client."""

    def __init__(self):
        self.objects: dict = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(f"{Key} not found")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def test_local_storage_roundtrip(tmp_path):
    s = LocalStorage(tmp_path)
    s.save("k1", b"data")
    assert s.read("k1") == b"data"
    s.delete("k1")
    with pytest.raises(Exception):
        s.read("k1")


def test_local_storage_absolute_key_compat(tmp_path):
    """Old absolute-path keys are still readable."""
    s = LocalStorage(tmp_path)
    p = tmp_path / "old"
    p.write_bytes(b"legacy")
    assert s.read(str(p)) == b"legacy"


def test_s3_storage_roundtrip():
    s = S3Storage(bucket="wiki", endpoint="", access_key="test", secret_key="test")
    s._client = FakeS3Client()
    s.save("k1", b"data")
    assert s.read("k1") == b"data"
    s.delete("k1")
    with pytest.raises(Exception):
        s.read("k1")


def test_get_storage_respects_backend(monkeypatch):
    monkeypatch.setattr(app_config.app, "storage_backend", "local")
    assert isinstance(get_storage(), LocalStorage)
    monkeypatch.setattr(app_config.app, "storage_backend", "s3")
    monkeypatch.setattr(app_config.app, "s3_bucket", "wiki")
    monkeypatch.setattr(app_config.app, "s3_endpoint", "http://localhost:9000")
    monkeypatch.setattr(app_config.app, "s3_access_key", "k")
    monkeypatch.setattr(app_config.app, "s3_secret_key", "s")
    assert isinstance(get_storage(), S3Storage)
