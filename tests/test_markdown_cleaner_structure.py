"""Tests for the structural-repair rules added in cleaner 1.1.0."""
from __future__ import annotations

from src.markdown_cleaner import CleanConfig, clean_markdown


def test_page_residue_lines_are_removed():
    md = "PART 01\n\n02\n\n03 有机黑木耳丝系列标题。\n"
    result = clean_markdown(md)
    lines = [line.strip() for line in result.markdown.splitlines()]
    assert "PART 01" not in lines
    assert "02" not in lines
    assert "03 有机黑木耳丝系列标题。" in lines
    assert result.report["counts"]["removed_page_residue"] == 2


def test_paged_noise_heading_is_removed():
    md = "# PART 01\n\n正文。\n"
    result = clean_markdown(md)
    assert "# PART 01" not in result.markdown
    assert result.report["counts"]["removed_noise_headings"] == 1


def test_ocr_spacing_and_inline_tags_fixed():
    md = "# 有 机 地 标\n\n黑 木 耳 中 含 有 <sub>功</sub>能。\n"
    result = clean_markdown(md)
    assert "# 有机地标" in result.markdown
    assert "黑木耳中含有功能。" in result.markdown
    assert result.report["counts"]["normalized_ocr_spacing"] == 2
    assert result.report["counts"]["cleaned_inline_tags"] == 1


def test_numbered_body_sequence_becomes_list_only_for_runs():
    md = "01 原料必须来自认证基地。\n02 加工过程不得混入非有机原料。\n"
    result = clean_markdown(md)
    assert "- 01 原料必须来自认证基地。" in result.markdown
    assert "- 02 加工过程不得混入非有机原料。" in result.markdown
    assert result.report["counts"]["converted_numbered_bodies"] == 2

    single = clean_markdown("01 单独一段说明。\n")
    assert single.markdown.strip().startswith("01 ")
    assert single.report["counts"]["converted_numbered_bodies"] == 0


def test_repeated_paragraph_text_is_deduplicated():
    sentence = "这是有机地标2025年新推出的品牌故事。"
    md = sentence + sentence + "请记住这一行。\n"
    result = clean_markdown(md)
    assert result.markdown.count(sentence) == 1
    assert result.markdown.strip().endswith("请记住这一行。")
    assert result.report["counts"]["removed_repeated_paragraph_text"] == 1


def test_paged_long_line_split_into_heading_and_body():
    md = (
        "03 有机黑木耳丝系列"
        "黑木耳（black fungus）又名黑菜、木耳、云耳，为我国珍贵的药食兼用 "
        "黑木耳（blackfungus）又名黑菜、木耳、云耳，为我国珍贵的药食兼用"
        "胶质真菌，也是世界上公认的保健食品。我国是黑木耳的故乡，\n"
    )
    result = clean_markdown(md, config=CleanConfig(min_paged_split_chars=80))
    assert "## 有机黑木耳丝系列" in result.markdown
    assert "黑木耳（black fungus）又名黑菜" in result.markdown
    assert "黑木耳（blackfungus）又名黑菜" not in result.markdown
    assert result.report["counts"]["split_paged_long_lines"] == 1


def test_bullet_prefixes_are_normalized():
    md = (
        "·黑木耳多糖是黑木耳的主要活性成分。\n"
        "··世界卫生组织确认的人体必需微量元素有14种。\n"
    )
    result = clean_markdown(md)
    assert "- 黑木耳多糖是黑木耳的主要活性成分。" in result.markdown
    assert "- 世界卫生组织确认的人体必需微量元素有14种。" in result.markdown
    assert result.report["counts"]["normalized_list_bullets"] == 2

def test_invisible_chars_and_white_space_cleaned():
    md = "# 标\u200b题\n\n内容\u00a0A  B，测试。\n"
    result = clean_markdown(md)
    assert "\u200b" not in result.markdown
    assert "\u00a0" not in result.markdown
    assert "内容 A B，测试。" in result.markdown
    assert result.report["counts"]["removed_invisible_chars"] >= 1
    assert result.report["counts"]["collapsed_whitespace"] >= 1


def test_external_links_cleaned_while_internal_kept():
    md = "参考[官网](https://example.com/x)。\n\n[](http://x/)无效。\n\n[内部](docs/a.md)保留。\n"
    result = clean_markdown(md)
    assert "参考官网。" in result.markdown
    assert "无效。" in result.markdown
    assert "[内部](docs/a.md)" in result.markdown
    assert result.report["counts"]["cleaned_link_markup"] == 2


def test_broken_sentence_across_blank_line_merged():
    md = "## 价值观\n\n务实进取、努力奋斗、合作共赢、勇于创\n\n新，坚持乡村振兴三农发展为奋斗目标。\n"
    result = clean_markdown(md)
    assert "合作共赢、勇于创新，坚持" in result.markdown
    assert result.report["counts"]["merged_broken_sentences"] == 1


def test_soft_wrap_lines_merged():
    md = "这是第一行的一段正文内容\n紧接着下一行继续。\n\n## 标题\n"
    result = clean_markdown(md)
    assert "内容紧接着下一行继续。" in result.markdown
    assert result.report["counts"]["merged_soft_wraps"] == 1


def test_short_captions_are_not_merged():
    md = "基地\n\n直发\n\n## 标题\n"
    result = clean_markdown(md)
    assert "基地\n\n直发" in result.markdown
    assert result.report["counts"]["merged_broken_sentences"] == 0
