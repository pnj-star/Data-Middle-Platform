"""Tests for the VLM image-description client (no real network)."""
from __future__ import annotations

import types

import pytest

from src import vlm
from src.config import config


def _enable_llm(monkeypatch) -> None:
    monkeypatch.setattr(config.llm, "enabled", True)
    monkeypatch.setattr(config.llm, "base_url", "http://llm.local/v1")
    monkeypatch.setattr(config.llm, "api_key", "test-key")
    monkeypatch.setattr(config.llm, "model", "vision-model")


class TestVlm:
    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setattr(config.llm, "enabled", False)
        assert vlm.generate_image_description("x.jpg") == ""

    def test_misconfigured_returns_empty(self, monkeypatch):
        monkeypatch.setattr(config.llm, "enabled", True)
        monkeypatch.setattr(config.llm, "base_url", "")
        assert vlm.generate_image_description("x.jpg") == ""

    def test_enabled_returns_stripped_content(self, monkeypatch, tmp_path):
        _enable_llm(monkeypatch)

        class _FakeCompletions:
            def create(self, **kwargs):
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(
                        message=types.SimpleNamespace(content=" 一张蘑菇图 "))]
                )

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeOpenAI:
            def __init__(self, *a, **k):
                self.chat = _FakeChat()

        monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
        img = tmp_path / "i.png"
        img.write_bytes(b"fake-png-bytes")
        assert vlm.generate_image_description(str(img)) == "一张蘑菇图"

    def test_enabled_error_returns_empty(self, monkeypatch, tmp_path):
        _enable_llm(monkeypatch)

        class _BoomOpenAI:
            def __init__(self, *a, **k):
                raise RuntimeError("boom")

        monkeypatch.setattr("openai.OpenAI", _BoomOpenAI)
        img = tmp_path / "i.png"
        img.write_bytes(b"fake-png-bytes")
        assert vlm.generate_image_description(str(img)) == ""
