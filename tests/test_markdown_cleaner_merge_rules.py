"""Regression tests for the conservative repeated-heading merge rule."""
from __future__ import annotations

from pathlib import Path

from src.markdown_cleaner import CleanConfig, clean_markdown


OUTPUTS = Path(__file__).resolve().parents[1] / "outputs"


def test_repeated_heading_without_images_never_merged():
    md = "## 标题\n\n短正文\n\n## 标题\n\n短正文\n"
    result = clean_markdown(md)
    assert result.markdown.count("## 标题") == 2
    assert result.report["counts"]["merged_repeated_headings"] == 0


def test_repeated_heading_with_real_subtitle_not_merged():
    md = (
        "## 产品故事的卖点\n\n"
        "产品故事的卖点\n\n"
        "产品展示：适合烤着吃的蜜薯，口感超级赞\n\n"
        "<!-- image -->\n\n"
        "<!-- image -->\n\n"
        "## 产品故事的卖点\n\n"
        "产品故事的卖点\n\n"
        "产品展示：线上版本，紧凑固定，方便运输\n\n"
        "<!-- image -->\n"
    )
    result = clean_markdown(md)
    assert result.markdown.count("## 产品故事的卖点") == 2
    assert result.report["counts"]["merged_repeated_headings"] == 0


def test_image_dominant_repeated_heading_merge_can_be_relaxed():
    md = (
        "## 实用新型专利证书\n\n"
        "这里是一段超过二十个字符并配有图片的简短文字说明\n\n"
        "<!-- image -->\n\n"
        "<!-- image -->\n\n"
        "## 实用新型专利证书\n\n"
        "<!-- image -->\n"
    )
    strict = clean_markdown(md)
    assert strict.markdown.splitlines().count("## 实用新型专利证书") == 2

    relaxed = clean_markdown(
        md,
        config=CleanConfig(
            max_gap_text_chars=60,
            max_repeated_body_text_chars=80,
        ),
    )
    assert relaxed.markdown.splitlines().count("## 实用新型专利证书") == 1
    assert relaxed.report["counts"]["merged_repeated_headings"] == 1


def test_real_brochure_repeated_section_not_merged():
    path = OUTPUTS / "f2314f4bee0244a7b4ea05b9f0c51e6f.md"
    if not path.exists():
        return
    result = clean_markdown(path.read_text(encoding="utf-8"))
    assert result.report["counts"]["merged_repeated_headings"] == 0


def test_real_certificate_image_merges_kept():
    path = OUTPUTS / "2b087796dd984275965e7e660526d59e.md"
    if not path.exists():
        return
    result = clean_markdown(path.read_text(encoding="utf-8"))
    assert result.report["counts"]["merged_repeated_headings"] > 0
