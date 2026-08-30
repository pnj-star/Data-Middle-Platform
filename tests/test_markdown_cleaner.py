"""Conservative heading-cleaner unit tests (no database required)."""
from __future__ import annotations

import re
from pathlib import Path

from src.markdown_cleaner import CleanConfig, clean_markdown


OUTPUTS = Path(__file__).resolve().parents[1] / "outputs"


def test_bold_numbered_full_line_is_promoted_only():
    md = (
        "正文开始。\n\n"
        "**1、集团概括**\n\n"
        "集团介绍正文。\n\n"
        "列表项里的 **2、重点** 不是标题。\n\n"
        "**（1）** 括号编号也不该是标题。\n"
    )
    result = clean_markdown(md)
    assert "## 1、集团概括" in result.markdown
    assert "列表项里的 **2、重点** 不是标题。" in result.markdown
    assert "## （1）" not in result.markdown
    assert result.report["counts"]["converted_bold_headings"] == 1


def test_inline_bold_emphasis_is_never_promoted():
    md = "**注**：这是重点说明，不是标题。\n\n**重要**\n\n正文。\n"
    result = clean_markdown(md)
    assert "## 注" not in result.markdown
    assert "## 重要" not in result.markdown
    assert "**注**：这是重点说明，不是标题。" in result.markdown


def test_junk_headings_removed_but_functional_heading_kept():
    md = (
        "# THANKS\n\n"
        "## 01\n\n"
        "## 10\n\n"
        "## 实施内容：\n\n"
        "## 这是一个超过三十个字符的标题句子，它看起来很像正文段落。所以它会变成普通正文内容。\n\n"
        "正文。\n"
    )
    result = clean_markdown(md)
    assert "# THANKS" not in result.markdown
    assert "## 01" not in result.markdown
    assert "## 10" not in result.markdown
    assert "## 实施内容：" in result.markdown
    sentence = "这是一个超过三十个字符的标题句子，它看起来很像正文段落。所以它会变成普通正文内容。"
    assert sentence in result.markdown
    assert f"## {sentence}" not in result.markdown
    assert result.report["counts"]["removed_noise_headings"] >= 3
    assert result.report["counts"]["demoted_heading_lines"] == 1


def test_empty_headings_are_removed():
    md = "# 标题\n\n## \n\n##\n\n正文。\n"
    result = clean_markdown(md)
    assert not any(re.fullmatch(r"#{1,6}\s*", line) for line in result.markdown.splitlines())
    assert result.report["counts"]["removed_empty_headings"] == 2


def test_near_image_repeated_heading_merged_only_once():
    md = (
        "## 产品介绍\n\n"
        "<!-- image -->\n\n"
        "## 产品介绍\n\n"
        "<!-- image -->\n\n"
        "<!-- image -->\n"
    )
    result = clean_markdown(md)
    assert result.markdown.count("## 产品介绍") == 1
    assert result.report["counts"]["merged_repeated_headings"] == 1


def test_cross_section_repeated_heading_never_merged():
    md = (
        "## 产品介绍\n\n"
        "这是一段超过六十个字符的正文内容，用来证明同名标题之间如果隔着真实内容就绝不能合并到一起。\n\n"
        "## 产品介绍\n\n"
        "另一段足够长的正文内容，同样需要超过八十个字符，用来确保第二个同名标题不会被误删。\n"
    )
    result = clean_markdown(md)
    assert result.markdown.count("## 产品介绍") == 2
    assert result.report["counts"]["merged_repeated_headings"] == 0


def test_force_single_h1_is_optional():
    md = "# 第一章\n\n正文。\n\n# 第二章\n\n正文。\n"
    default = clean_markdown(md)
    assert default.markdown.splitlines().count("# 第一章") == 1
    assert default.markdown.splitlines().count("# 第二章") == 1

    forced = clean_markdown(md, config=CleanConfig(force_single_h1=True))
    assert forced.markdown.splitlines().count("# 第二章") == 0
    assert "## 第二章" in forced.markdown.splitlines()
    assert forced.report["counts"]["demoted_h1_after_first"] == 1


def test_pptx_always_forces_single_h1():
    md = "# Slide 1\n\n正文。\n\n# Slide 2\n\n正文。\n"
    result = clean_markdown(md, extension=".pptx")
    assert "## Slide 2" in result.markdown
    assert result.report["counts"]["demoted_h1_after_first"] == 1


def test_real_bold_numbered_document():
    path = OUTPUTS / "528f5b96d0434673b10c97847ff1f238.md"
    if not path.exists():
        return
    result = clean_markdown(path.read_text(encoding="utf-8"))
    assert "## 1、集团概括" in result.markdown
    assert "## 2、公司概括" in result.markdown
    assert not any(
        re.fullmatch(r"#{1,6}\s*", line)
        for line in result.markdown.splitlines()
    )
    assert result.report["counts"]["converted_bold_headings"] >= 2


def test_real_noise_heading_document():
    path = OUTPUTS / "2b087796dd984275965e7e660526d59e.md"
    if not path.exists():
        return
    result = clean_markdown(path.read_text(encoding="utf-8"))
    assert "## 01" not in result.markdown.splitlines()
    assert result.report["counts"]["removed_noise_headings"] >= 1
