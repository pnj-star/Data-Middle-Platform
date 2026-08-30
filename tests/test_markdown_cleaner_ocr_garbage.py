"""Regression tests for OCR garbage fragment removal."""
from __future__ import annotations

from src.markdown_cleaner import clean_markdown


def test_ocr_fragments_under_headings_are_removed():
    md = (
        "## 胶原蛋白\n\n"
        "·黑 木耳 中 较\n\n"
        "## 黑色素\n\n"
        "·黑木耳 含 有 素，<sub>功</sub> ， <sup>，</sup><sub>疫</sub>\n"
    )
    result = clean_markdown(md)
    assert "黑木耳中较" not in result.markdown
    assert "功，，疫" not in result.markdown
    assert result.report["counts"]["removed_ocr_garbage_fragments"] == 2


def test_terminal_punct_garbage_fragment_removed():
    md = "## 菌种制作\n\n从菌种研发、拌料、装袋、接控，保证菌 菌、消毒、养 个把棒 菌质量。\n"
    result = clean_markdown(md)
    assert "个把棒菌质量" not in result.markdown
    assert result.report["counts"]["removed_ocr_garbage_fragments"] == 1


def test_short_remainder_fragments_under_headings_removed():
    md = (
        "## 木耳种植\n\n亩基地，生 栽智科实化与监督\n\n"
        "## 晾晒分拣\n\n无何工排 杂保保证 全安全优质\n"
    )
    result = clean_markdown(md)
    assert "生栽智科" not in result.markdown
    assert "保保证" not in result.markdown
    assert result.report["counts"]["removed_ocr_garbage_fragments"] == 2


def test_normal_ocr_spaced_paragraph_kept():
    md = r"## 多酚类\n\n· 黑 木 耳 中 含 有1.1%\~1.3%的多酚。多酚类物质具有较强的抗氧化作用，被作为功能性食品添加剂。\n"
    result = clean_markdown(md)
    assert r"黑木耳中含有1.1%\~1.3%的多酚" in result.markdown
    assert result.report["counts"]["removed_ocr_garbage_fragments"] == 0


def test_short_captions_are_kept():
    md = "基地\n\n直发\n\n80克礼盒装干净无沙，煲汤佳品\n"
    result = clean_markdown(md)
    assert "基地" in result.markdown
    assert "直发" in result.markdown
    assert "80克礼盒装干净无沙，煲汤佳品" in result.markdown
    assert result.report["counts"]["removed_ocr_garbage_fragments"] == 0


def test_single_space_captions_and_slogans_kept():
    md = (
        "## 实拍\n\n菌盖呈黑褐色 菌柄呈乳白色\n\n"
        "## 团队\n\n局长 申长雨\n\n"
        "## 标语\n\n为/家/人/健/康 选/有/机/食/材\n"
    )
    result = clean_markdown(md)
    assert "菌盖呈黑褐色菌柄呈乳白色" in result.markdown
    assert "局长申长雨" in result.markdown
    assert "为/家/人/健/康选/有/机/食/材" in result.markdown
    assert result.report["counts"]["removed_ocr_garbage_fragments"] == 0


def test_page_split_fragment_with_continuation_kept():
    md = "黑 木 耳 中 含\n\n有1.1%的多酚。\n"
    result = clean_markdown(md)
    assert "黑木耳中含有1.1%的多酚" in result.markdown
    assert result.report["counts"]["removed_ocr_garbage_fragments"] == 0
