"""Unit tests for the token-based chunker module."""
from __future__ import annotations

import pytest
from src.chunker import chunk, ChunkTree, ParentChunk, ChildChunk, _find_boundary, _split_into_children, count_tokens, _preprocess_bold_titles, _BOLD_NUM_TITLE_RE


class TestFindBoundary:
    def test_period_newline(self):
        text = "一些文本。\n新段落"
        pos = _find_boundary(text)
        assert pos > 0

    def test_period_priority(self):
        text = "第一句。第二句。第三句\n"
        pos = _find_boundary(text)
        assert pos == 4

    def test_no_boundary(self):
        text = "这是一段没有标点符号的连续文本"
        pos = _find_boundary(text)
        assert pos == 0

    def test_comma_fallback(self):
        text = "没有句号，但有逗号"
        pos = _find_boundary(text)
        assert text[pos - 1] == "，"


class TestSplitIntoChildren:
    def test_single_short_text(self):
        children = _split_into_children("hello", 100, 20)
        assert len(children) == 1
        assert children[0].content == "hello"

    def test_multiple_chunks(self):
        text = "A" * 800
        children = _split_into_children(text, 30, 5)
        assert len(children) >= 2
        for c in children:
            assert c.size > 0

    def test_empty_text(self):
        children = _split_into_children("   ", 100, 10)
        assert len(children) == 0

    def test_large_parent_does_not_skip_middle_content(self):
        text = "".join(f"唯一段落{number}。" for number in range(400))
        children = _split_into_children(text, 120, 20)
        joined = "".join(child.content for child in children)

        assert len(children) > 10
        for number in range(400):
            assert f"唯一段落{number}" in joined


class TestTokenCounting:
    def test_chinese_token_count(self):
        # Chinese chars are typically ~1-2 tokens each in cl100k_base.
        tokens = count_tokens("你好世界")
        assert 1 <= tokens <= 8

    def test_english_token_count(self):
        tokens = count_tokens("hello world")
        assert tokens >= 1

    def test_empty_string(self):
        assert count_tokens("") == 0


class TestOversizedTableSplit:
    def test_small_table_stays_atomic(self):
        table = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        children = _split_into_children(table, chunk_size=500, overlap=0)
        assert len(children) == 1
        assert "[表格截断]" not in children[0].content

    def test_oversized_table_splits_with_marker(self):
        rows = "\n".join(f"| col_{i} | data_{i} |" for i in range(100))
        table = f"| H1 | H2 |\n|---|---|\n{rows}"
        children = _split_into_children(table, chunk_size=50, overlap=0)
        assert len(children) > 1
        truncated = [c for c in children if "表格截断" in c.content]
        assert len(truncated) >= 1

    def test_multiple_oversized_tables_keep_monotonic_indexes(self):
        def table(tag: str) -> str:
            rows = "\n".join(f"| {tag}{i} | data_{i} |" for i in range(60))
            return f"| {tag}_H1 | {tag}_H2 |\n|---|---|\n{rows}"

        text = table("A") + "\n普通段落。\n" + table("B")
        children = _split_into_children(text, chunk_size=40, overlap=0)
        assert [c.index for c in children] == list(range(len(children)))

    def test_single_huge_table_row_respects_token_limit(self):
        huge_cell = "很长的单元格内容" * 300
        table = f"| H1 | H2 |\n|---|---|\n| R1 | {huge_cell} |"
        children = _split_into_children(table, chunk_size=50, overlap=0)
        assert len(children) > 1
        assert all(c.size <= 50 for c in children)


class TestOversizedParentSplit:
    def test_large_parent_gets_split(self):
        content = "这是一个很长的段落。" * 500
        md = f"# 标题\n## 章节\n{content}"
        tree = chunk(md, chunk_size=100, overlap=20,
                     max_parent_size=300)
        assert tree.parent_count > 1
        assert all(p.title == "## 章节" for p in tree.parents)

    def test_parent_without_punctuation_still_has_hard_cap(self):
        md = "## 标题\n" + ("没有标点的连续长句" * 1000)
        tree = chunk(md, chunk_size=80, overlap=0,
                     max_parent_size=200)
        assert tree.parent_count > 1
        assert all(p.size <= 200 for p in tree.parents)

    def test_normal_parent_not_split(self):
        md = "## 正常章节\n这是一段正常长度的内容。"
        tree = chunk(md, chunk_size=300, overlap=40,
                     max_parent_size=2000)
        assert tree.parent_count == 1


class TestChunk:
    def test_headings_split(self):
        markdown = "## 标题1\n内容11。内容12。\n## 标题2\n内容21。内容22。"
        tree = chunk(markdown, chunk_size=30, overlap=0)
        assert tree.parent_count == 2
        assert tree.parents[0].content.startswith("## 标题1\n")
        assert tree.parents[1].content.startswith("## 标题2\n")

    def test_parent_content_includes_heading_title(self):
        markdown = "## 独立章节\n这里是一段正文。"
        tree = chunk(markdown, chunk_size=50, overlap=0)
        assert tree.parents[0].content.startswith("## 独立章节\n")
        assert "这里是一段正文" in tree.parents[0].content
        assert any(
            "## 独立章节" in child.content
            for child in tree.parents[0].children
        )

    def test_no_headings(self):
        markdown = "只是一段普通文本，没有任何标题。"
        tree = chunk(markdown)
        assert tree.parent_count == 1
        assert tree.child_count >= 1

    def test_small_h2_stays_isolated(self):
        markdown = "## 大标题\n" + "A" * 600 + "\n## 小标题\n短"
        tree = chunk(markdown, chunk_size=200, overlap=0)
        assert tree.parent_count == 2
        assert tree.parents[1].title == "## 小标题"


    def test_h1_without_h2_produces_no_parent(self):
        md = "# 有机地标农场手册\n\n为/家/人/健/康 选/有/机/食/材"
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 0

    def test_h1_bodies_before_first_h2_merge_in_order(self):
        md = (
            "# A 产品\n\n"
            + "A 开场白。" * 3
            + "\n\n# B 产品\n\n"
            + "B 开场白。" * 3
            + "\n\n## B1 规则\n\n"
            + "B1 规则内容。" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 1
        content = tree.parents[0].content
        assert content.index("A 开场白") < content.index("B 开场白")
        assert content.index("B 开场白") < content.index("B1 规则内容")
        assert "# B 产品" in content


class TestParentId:
    def test_parent_id_assigned(self):
        md = "## A\n" + "x" * 200 + "\n## B\n" + "y" * 200
        tree = chunk(md, chunk_size=100, overlap=0)
        ids = [p.parent_id for p in tree.parents]
        assert len(ids) == 2
        assert all(ids)
        assert ids[0] != ids[1]

    def test_no_headings_parent_has_id(self):
        tree = chunk("没有标题的一段普通文本。", chunk_size=50)
        assert tree.parent_count == 1
        assert tree.parents[0].parent_id


class TestBoldTitleBoundary:
    def test_inline_bold_title_split_into_new_parent(self):
        md = "## A\n" + "x" * 300 + "\n" + "**3、公司理念**；有机地标专注于食用菌产品。\n" + "y" * 200
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 2
        assert tree.parents[1].title == "**3、公司理念**"

    def test_standalone_bold_number_title_splits(self):
        md = "## A\n" + "x" * 300 + "\n**4、公司信念**\n" + "y" * 200
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 2
        assert tree.parents[1].title == "**4、公司信念**"

    def test_h3_and_inline_bold_stay_in_parent(self):
        md = "## A\n" + "x" * 300 + "\n### 小节\n" + "y" * 200 + "\n正文中有 **强调** 但不拆分。\n" + "z" * 200
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 1

    def test_bold_number_title_regex_shape(self):
        assert _BOLD_NUM_TITLE_RE.match("**3、公司理念**")
        assert _BOLD_NUM_TITLE_RE.match("**1. Overview**")
        assert not _BOLD_NUM_TITLE_RE.match("正文 **3、公司理念** 结尾")
        assert not _BOLD_NUM_TITLE_RE.match("**重要提示**：请勿在段落中拆分")

    def test_preprocess_keeps_tables_and_paragraphs(self):
        md = "| a | b |\n| - | - |\n\n段落里面没有标题。\n"
        cleaned = _preprocess_bold_titles(md)
        assert cleaned == md



class TestH2Isolation:
    def test_oversized_h2_slices_all_keep_prefix_and_title(self):
        md = "## 大标题\n" + "A" * 5250 + "\n## 小标题\n" + "B" * 1000
        tree = chunk(md, chunk_size=200, overlap=0, max_parent_size=700)
        assert tree.parents[-1].title == "## 小标题"
        for parent in tree.parents[:-1]:
            assert parent.title == "## 大标题"
            assert parent.content.startswith("## 大标题\n")

    def test_small_h2_never_merges_into_previous_section(self):
        md = "## 大标题\n" + "A" * 5250 + "\n## 小标题\n" + "B" * 30
        tree = chunk(md, chunk_size=200, overlap=0, max_parent_size=700)
        assert tree.parents[-1].title == "## 小标题"
        assert ("B" * 30) in tree.parents[-1].content


class TestH1HeaderLock:
    def test_h1_prefix_attached_to_every_h2_parent(self):
        md = (
            "# 文档总标题\n\n"
            "## 1、第一章节\n\n"
            + "第一章节很短的内容。" * 3
            + "\n\n## 2、第二章节\n\n"
            + "第二章节很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长的内容。" * 3
            + "\n\n## 3、第三章节\n\n"
            + "第三章节很短的内容。" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 3
        for parent in tree.parents:
            assert "# 文档总标题" in parent.content
        assert "## 1、第一章节" in tree.parents[0].content
        assert tree.parents[1].title == "## 2、第二章节"
        assert "第二章节很长" in tree.parents[1].content
        assert "第三章节很短" in tree.parents[2].content

    def test_oversized_h2_is_sliced_but_prefix_kept(self):
        md = (
            "# 文档总标题\n\n"
            "## 1、超大章节\n\n"
            + "超大的章节正文。" * 80
        )
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=100)
        assert tree.parent_count > 1
        for parent in tree.parents:
            assert parent.title == "## 1、超大章节"
            assert "# 文档总标题" in parent.content
            assert "## 1、超大章节" in parent.content


    def test_preamble_and_h1_body_merge_into_first_h2(self):
        md = (
            "![](/media/images/cover.jpg)\n\n"
            "为/家/人/健/康 选/有/机/食/材\n\n"
            "# 文档总标题\n\n"
            "## 1、基地简介\n\n"
            + "基地简介正文内容。" * 3
            + "\n\n## 2、使命\n\n"
            + "使命正文内容。" * 3
            + "\n\n## 3、愿景\n\n"
            + "愿景正文内容。" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 3
        assert tree.parents[0].title == "## 1、基地简介"
        assert "# 文档总标题" in tree.parents[0].content
        assert "为/家/人/健/康" in tree.parents[0].content
        assert "基地简介正文内容" in tree.parents[0].content
        assert "## 2、使命" not in tree.parents[0].content
        assert tree.parents[1].title == "## 2、使命"
        assert tree.parents[2].title == "## 3、愿景"
        assert "愿景正文内容" in tree.parents[2].content

    def test_h1_body_merges_into_first_h2(self):
        md = (
            "# 文档总标题\n\n"
            + "大标题下的正文。" * 20
            + "\n\n## 1、第一章节\n\n"
            + "第一章节正文内容。" * 50
            + "\n\n## 2、第二章节\n\n"
            + "第二章节正文内容。" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=900)
        assert tree.parent_count == 2
        assert tree.parents[0].title == "## 1、第一章节"
        assert "大标题下的正文" in tree.parents[0].content
        assert "第一章节正文内容" in tree.parents[0].content
        assert "## 2、第二章节" not in tree.parents[0].content
        assert tree.parents[1].title == "## 2、第二章节"
        assert "第二章节正文内容" in tree.parents[1].content

class TestH1GroupMerge:
    def test_short_h2_merges_within_same_h1_group(self):
        md = (
            "# A\u4ea7\u54c1\n\n"
            "## A\u957f\u7ae0\u8282\n\n"
            + "A \u957f\u7ae0\u8282\u5185\u5bb9\u3002" * 40
            + "\n\n## A\u5c0f\u7ae0\u8282\n\n"
            + "A \u4ea7\u54c1\u5185\u5bb9\u3002" * 3
            + "\n\n# B\u4ea7\u54c1\n\n"
            "## B\u5c0f\u8282\u6548\n\n"
            + "B \u4ea7\u54c1\u529f\u6548\u5185\u5bb9\u3002" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0,
                     max_parent_size=2000)
        assert tree.parent_count == 3
        assert "# A\u4ea7\u54c1" in tree.parents[0].content
        assert "A \u957f\u7ae0\u8282\u5185\u5bb9" in tree.parents[0].content
        assert "A \u4ea7\u54c1\u5185\u5bb9" in tree.parents[1].content
        assert "B \u4ea7\u54c1\u529f\u6548\u5185\u5bb9" not in tree.parents[1].content
        assert "# B\u4ea7\u54c1" in tree.parents[2].content
        assert "B \u4ea7\u54c1\u529f\u6548\u5185\u5bb9" in tree.parents[2].content
        assert "A \u4ea7\u54c1\u5185\u5bb9" not in tree.parents[2].content

    def test_cross_h1_short_sections_never_merge(self):
        md = (
            "# A\u4ea7\u54c1\n\n"
            "## A\u4ea7\u54c1\u7981\u5fcc\n\n"
            + "A \u4ea7\u54c1\u7981\u5fcc\u5185\u5bb9\u3002" * 3
            + "\n\n# B\u4ea7\u54c1\n\n"
            "## B\u4ea7\u54c1\u529f\u6548\n\n"
            + "B \u4ea7\u54c1\u529f\u6548\u5185\u5bb9\u3002" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0,
                     max_parent_size=2000)
        assert tree.parent_count == 2
        a = tree.parents[0].content
        b = tree.parents[1].content
        assert "A \u4ea7\u54c1\u7981\u5fcc\u5185\u5bb9" in a
        assert "B \u4ea7\u54c1\u529f\u6548\u5185\u5bb9" in b
        assert "B \u4ea7\u54c1\u529f\u6548\u5185\u5bb9" not in a
        assert "A \u4ea7\u54c1\u7981\u5fcc\u5185\u5bb9" not in b

    def test_no_h1_short_sections_stay_isolated(self):
        md = (
            "## \u7b2c\u4e00\u5c0f\u8282\n\n"
            + "\u7b2c\u4e00\u5c0f\u8282\u5185\u5bb9\u3002" * 3
            + "\n\n## \u7b2c\u4e8c\u5c0f\u8282\n\n"
            + "\u7b2c\u4e8c\u5c0f\u8282\u5185\u5bb9\u3002" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0,
                     max_parent_size=2000)
        assert tree.parent_count == 2
        assert "\u7b2c\u4e00\u5c0f\u8282\u5185\u5bb9" in tree.parents[0].content
        assert "\u7b2c\u4e8c\u5c0f\u8282\u5185\u5bb9" in tree.parents[1].content

    def test_no_h1_three_short_sections_stay_isolated(self):
        md = (
            "## \u7b2c\u4e00\u8282\n\n"
            + "\u7b2c\u4e00\u8282\u5185\u5bb9\u3002" * 3
            + "\n\n## \u7b2c\u4e8c\u8282\n\n"
            + "\u7b2c\u4e8c\u8282\u5185\u5bb9\u3002" * 3
            + "\n\n## \u7b2c\u4e09\u8282\n\n"
            + "\u7b2c\u4e09\u8282\u5185\u5bb9\u3002" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0,
                     max_parent_size=2000)
        assert tree.parent_count == 3
        assert "\u7b2c\u4e00\u8282\u5185\u5bb9" in tree.parents[0].content
        assert "\u7b2c\u4e8c\u8282\u5185\u5bb9" in tree.parents[1].content
        assert "\u7b2c\u4e09\u8282\u5185\u5bb9" in tree.parents[2].content

    def test_two_h1_groups_after_preamble_stay_separate(self):
        md = (
            "前置说明。\n\n"
            "# A 产品\n\n"
            "## A1 禁忌\n\n"
            + "A1 禁忌内容。" * 3
            + "\n\n## A2 功效\n\n"
            + "A2 功效内容。" * 3
            + "\n\n# B 产品\n\n"
            "## B1 规则\n\n"
            + "B1 规则内容。" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0,
                     max_parent_size=2000)
        assert tree.parent_count == 3
        assert "前置说明。" in tree.parents[0].content
        assert "# A 产品" in tree.parents[0].content
        assert "A1 禁忌内容" in tree.parents[0].content
        assert "A2 功效内容" not in tree.parents[0].content
        assert "A2 功效内容" in tree.parents[1].content
        assert "# B 产品" in tree.parents[2].content
        assert "B1 规则内容" in tree.parents[2].content
        assert "A1 禁忌内容" not in tree.parents[2].content

    def test_h1_body_merges_into_first_oversized_h2(self):
        md = (
            "# 大标题A\n\n"
            + "大标题下的正文。" * 15
            + "\n\n## A1 超大\n\n"
            + "第一章节正文内容。" * 20
            + "\n\n# B 产品\n\n"
            "## B1 规则\n\n"
            + "B1 规则内容。" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0,
                     max_parent_size=700)
        assert tree.parent_count == 2
        assert tree.parents[0].title == "## A1 超大"
        assert "大标题下的正文" in tree.parents[0].content
        assert "第一章节正文内容" in tree.parents[0].content
        assert "## B1 规则" not in tree.parents[0].content
        assert tree.parents[1].title == "## B1 规则"
        assert "# B 产品" in tree.parents[1].content
        assert "B1 规则内容" in tree.parents[1].content
        assert "第一章节正文内容" not in tree.parents[1].content

    def test_h1_without_h2_body_is_skipped(self):
        md = (
            "# A 产品\n\n"
            "# B 产品\n\n"
            "## B1 规则\n\n"
            + "B1 规则内容。" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 1
        assert tree.parents[0].title == "## B1 规则"
        assert "# B 产品" in tree.parents[0].content
        assert "B1 规则内容" in tree.parents[0].content

class TestBulletBoldTitle:
    def test_bullet_heading_merges_into_first_section(self):
        md = (
            "- **\u516c\u53f8\u4ecb\u7ecd**\n\n"
            "**1\u3001\u96c6\u56e2\u6982\u62ec**\n\n"
            + "\u96c6\u56e2\u6982\u62ec\u6b63\u6587\u5185\u5bb9\u3002" * 120
            + "\n\n**2\u3001\u516c\u53f8\u6982\u62ec**\n\n"
            + "\u516c\u53f8\u6982\u62ec\u6b63\u6587\u5185\u5bb9\u3002" * 3
        )
        tree = chunk(md, chunk_size=100, overlap=0,
                     max_parent_size=2000)
        assert tree.parent_count == 2
        assert "- **\u516c\u53f8\u4ecb\u7ecd**" in tree.parents[0].content
        assert "**1\u3001\u96c6\u56e2\u6982\u62ec**" in tree.parents[0].content
        assert "\u96c6\u56e2\u6982\u62ec\u6b63\u6587\u5185\u5bb9" in tree.parents[0].content
        assert "**2\u3001\u516c\u53f8\u6982\u62ec**" not in tree.parents[0].content
        assert tree.parents[1].title == "**2\u3001\u516c\u53f8\u6982\u62ec**"

class TestChildSplitLookback:
    def test_sentence_boundary_in_lookback_window(self):
        text = "".join(f"这是第{i}句。" for i in range(1, 50))
        children = _split_into_children(text, 30, 5)

        assert len(children) >= 2
        assert all(c.size <= 30 for c in children)
        assert all(c.content.endswith("。") for c in children)

    def test_no_boundary_hard_cuts_to_token_window(self):
        text = "无" * 1000
        children = _split_into_children(text, 30, 5)

        assert len(children) >= 2
        assert all(c.size <= 30 for c in children)
        assert all(c.content for c in children)

    def test_tail_fragment_is_preserved_and_loop_stops(self):
        sentinel = "这是唯一结尾标记。"
        text = "".join(f"这是第{i}句。" for i in range(1, 30)) + sentinel
        children = _split_into_children(text, 30, 5)

        assert children
        assert sentinel in children[-1].content
        assert children[-1].content.endswith("。")
        assert all(c.content for c in children)

    def test_semicolon_within_lookback_window_is_boundary(self):
        text = "这是第一段长内容，没有句号；" + "继续填充" * 200
        children = _split_into_children(text, 30, 5)

        assert children[0].content.endswith("；")

    def test_sentence_before_lookback_window_prevents_hard_cut(self):
        text = "已" * 10 + "结束。" + "哨兵" + "已" * 1000
        children = _split_into_children(text, chunk_size=50, overlap=5)

        assert children[0].content.endswith("。")
        assert any("哨兵" in child.content for child in children)

    def test_sentence_end_wins_over_later_comma(self):
        text = "已" * 10 + "结束。继续，继续继续" + "已" * 1000
        children = _split_into_children(text, chunk_size=50, overlap=5)

        assert children[0].content.endswith("。")



class TestLookbackParams:
    def test_child_lookback_tokens_overrides_default_window(self):
        text = "\u5df2" * 10 + "\u7ed3\u675f\u3002" + "\u54e8\u5175" + "\u5df2" * 1000
        default = _split_into_children(text, chunk_size=50, overlap=5)
        narrow = _split_into_children(
            text, chunk_size=50, overlap=5, lookback_tokens=1)
        wide = _split_into_children(
            text, chunk_size=50, overlap=5, lookback_tokens=50)

        assert default[0].content.endswith("\u3002")
        assert wide[0].content.endswith("\u3002")
        # A 1-token lookback cannot reach back to the sentence end.
        assert not narrow[0].content.endswith("\u3002")

    def test_parent_lookback_keeps_oversized_slices_on_sentence_boundaries(self):
        body = "\u8fd9\u662f\u4e00\u53e5\u5b8c\u6574\u7684\u8bdd\u3002" * 400
        md = f"## \u7ae0\u8282\n{body}"
        tree = chunk(md, chunk_size=50, overlap=0, max_parent_size=150,
                     parent_lookback_tokens=80)

        assert tree.parent_count > 1
        assert all(p.content.rstrip().endswith("\u3002") for p in tree.parents)


def test_image_markdown_stripped_and_collected():
    md = "## 图\n\n![茶](tea.png)\n\n正文内容。\n"
    tree = chunk(md, chunk_size=100, overlap=0)
    assert tree.images == [{"alt": "茶", "ref": "tea.png"}]
    assert "![茶](tea.png)" not in tree.parents[0].content
    assert tree.parents[0].images == [{"alt": "茶", "ref": "tea.png"}]
    assert "正文内容。" in tree.parents[0].content

class TestChildPrefixInheritance:
    def test_every_child_keeps_h1_h2_prefix(self):
        body = "\u9ed1\u6728\u8033\u552e\u540e\u8bf4\u660e\u3002\u7834\u635f\u5305\u8d54\u3002" * 400
        md = f"# \u8fbe\u4eba\u4ea7\u54c1\u653f\u7b56\n## \u9ed1\u6728\u8033\u4ea7\u54c1\u89c4\u5219\n{body}"
        tree = chunk(md, chunk_size=100, overlap=10, max_parent_size=2000)

        multi = [p for p in tree.parents if len(p.children) >= 2]
        assert multi
        for parent in multi:
            for child in parent.children:
                assert child.content.startswith(
                    "# \u8fbe\u4eba\u4ea7\u54c1\u653f\u7b56\n## \u9ed1\u6728\u8033\u4ea7\u54c1\u89c4\u5219")
                assert child.content.count("# \u8fbe\u4eba\u4ea7\u54c1\u653f\u7b56") == 1
                assert child.size <= 100

    def test_child_prefix_covers_h2_without_h1(self):
        md = "## \u5355\u7ea7\u8282\n" + "\u8fd9\u662f\u6b63\u6587\u5185\u5bb9\u3002" * 300
        tree = chunk(md, chunk_size=100, overlap=0, max_parent_size=2000)
        assert tree.parent_count == 1
        assert len(tree.parents[0].children) >= 2
        for child in tree.parents[0].children:
            assert child.content.startswith("## \u5355\u7ea7\u8282\n")
            assert child.size <= 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
