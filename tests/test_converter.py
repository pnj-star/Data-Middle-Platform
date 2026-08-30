"""Tests for the document converter (src.converter).

Covers input validation, MinerU routing/error paths, the built-in plain-text
path (md/txt/html) and the markdown save/load helpers. No external document
engine is required.
"""
from __future__ import annotations

import requests
import pytest

from src.converter import (
    SUPPORTED_EXTENSIONS,
    convert,
    load_markdown,
    save_markdown,
    _validate,
)
from src.config import config


def _touch(path, content: bytes | str = b"") -> str:
    data = content.encode("utf-8") if isinstance(content, str) else content
    path.write_bytes(data)
    return str(path)


class TestValidate:
    def test_accepts_supported_extensions(self, tmp_path):
        for ext in SUPPORTED_EXTENSIONS:
            p = tmp_path / f"file{ext}"
            _touch(p)
            assert _validate(p) == p

    def test_rejects_unknown_extension(self, tmp_path):
        p = tmp_path / "file.xyz"
        _touch(p)
        with pytest.raises(ValueError, match="Unsupported file type"):
            _validate(p)

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _validate(tmp_path / "nope.pdf")

    def test_rejects_oversized_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config.app, "max_file_size_mb", 0)
        p = tmp_path / "big.pdf"
        _touch(p, b"x" * 1024)
        with pytest.raises(ValueError, match="File too large"):
            _validate(p)


class TestPlainConversion:
    def test_markdown_passes_through(self, tmp_path):
        content = "# 标题\n\n正文段落"
        p = tmp_path / "doc.md"
        _touch(p, content)
        result = convert(p)
        assert result.markdown.strip() == content
        assert result.metadata["title"] == "doc"
        assert result.metadata["engine"] == "plain"
        assert result.metadata["page_count"] == 0

    def test_txt_passes_through(self, tmp_path):
        content = "plain text line 1\nline 2"
        p = tmp_path / "note.txt"
        _touch(p, content)
        result = convert(p)
        assert result.markdown.strip() == content
        assert result.metadata["engine"] == "plain"

    def test_html_converts_structure(self, tmp_path):
        html = (
            "<html><body>"
            "<h1>Title</h1>"
            "<p>Hello <strong>world</strong>.</p>"
            "<ul><li>one</li><li>two</li></ul>"
            "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
            "</body></html>"
        )
        p = tmp_path / "page.html"
        _touch(p, html)
        md = convert(p).markdown
        assert md.startswith("# Title")
        assert "**world**" in md
        assert "- one" in md and "- two" in md
        assert "| A | B |" in md
        assert "| 1 | 2 |" in md

    def test_html_drops_script_and_style(self, tmp_path):
        html = (
            "<html><head><style>p{color:red}</style></head><body>"
            "<script>alert('x')</script>"
            "<p>visible</p>"
            "</body></html>"
        )
        p = tmp_path / "page.html"
        _touch(p, html)
        md = convert(p).markdown
        assert "alert" not in md
        assert "color:red" not in md
        assert "visible" in md

    def test_html_images_kept(self, tmp_path):
        html = '<p><img alt="pic" src="x.png"></p>'
        p = tmp_path / "page.html"
        _touch(p, html)
        result = convert(p)
        assert "![pic](x.png)" in result.markdown
        assert result.metadata["images"] == [{"alt": "pic", "ref": "x.png"}]


class TestMineruRouting:
    def test_disabled_raises_clear_error_for_pdf(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config.app, "mineru_enabled", False)
        p = tmp_path / "doc.pdf"
        _touch(p)
        with pytest.raises(RuntimeError, match="No converter available"):
            convert(p)

    def test_unreachable_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config.app, "mineru_enabled", True)

        def _boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError("conn refused")

        monkeypatch.setattr(requests, "post", _boom)
        p = tmp_path / "doc.pdf"
        _touch(p)
        with pytest.raises(RuntimeError, match="MinerU API unreachable"):
            convert(p)

    def test_http_error_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config.app, "mineru_enabled", True)

        class _Resp:
            status_code = 500
            text = "internal error"

        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
        p = tmp_path / "doc.pdf"
        _touch(p)
        with pytest.raises(RuntimeError, match="MinerU parse failed"):
            convert(p)


class TestSaveLoad:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config.app, "output_dir", str(tmp_path / "outputs"))
        path = save_markdown("abc", "# hi\n")
        assert path.endswith("abc.md")
        assert load_markdown("abc") == "# hi\n"
        assert load_markdown("missing") is None
