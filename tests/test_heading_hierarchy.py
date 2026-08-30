from src.heading_hierarchy import HeadingHierarchyConfig, repair_heading_hierarchy


def _title(text, x0, height, page=0, block_type="title", width=260.0):
    return {
        "type": block_type,
        "text": text,
        "page_idx": page,
        "bbox": [x0, 0.0, x0 + width, height],
    }


def test_rebuilds_h1_h2_h3_from_layout_style():
    md = "# 农场手册\n\n## 黑木耳\n\n## 多糖\n\n## 菌种制作\n\n正文内容。\n"
    content_list = [
        _title("农场手册", 10, 40, page=0),
        _title("黑木耳", 20, 28, page=1),
        _title("多糖", 60, 20, page=1),
        _title("菌种制作", 60, 20, page=1),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "# 农场手册" in result.markdown
    assert "## 黑木耳" in result.markdown
    assert "### 多糖" in result.markdown
    assert "### 菌种制作" in result.markdown
    assert result.report["adjusted_levels"] == 2


def test_same_area_headings_keep_existing_level_when_no_leader():
    md = "# 一级\n\n## 二级A\n\n## 二级B\n"
    content_list = [
        _title("一级", 5, 30, page=0),
        _title("二级A", 5, 30, page=1),
        _title("二级B", 200, 30, page=1),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "# 一级" in result.markdown
    assert "## 二级A" in result.markdown
    assert "## 二级B" in result.markdown
    assert result.report["clusters"] == 2
    assert result.report["adjusted_levels"] == 0


def test_text_blocks_are_never_promoted():
    md = "普通正文段落，不应该被升级成标题。\n"
    content_list = [
        {"type": "text", "text": "普通正文段落，不应该被升级成标题。", "page_idx": 0,
         "bbox": [10.0, 0.0, 300.0, 24.0]},
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert result.markdown == md
    assert result.report["adjusted_levels"] == 0


def test_missing_content_list_falls_back_unchanged():
    md = "## 保留原有级别\n"
    result = repair_heading_hierarchy(md, None)
    assert result.markdown == md
    assert result.report["layout_title_count"] == 0


def test_decorative_number_heading_is_removed():
    md = "# 农场手册\n\n## 01\n\n## 黑木耳\n"
    content_list = [
        _title("农场手册", 10, 40, page=0, width=300),
        _title("01", 20, 28, page=1, width=50),
        _title("黑木耳", 20, 24, page=1, width=300),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "## 01" not in result.markdown
    assert "## 黑木耳" in result.markdown
    assert result.report["removed_decorative"] >= 1


def test_custom_max_levels_caps_assigned_levels():
    md = "# A\n\n## B\n\n## C\n"
    content_list = [
        _title("A", 10, 40, page=0),
        _title("B", 20, 28, page=1, width=300),
        _title("C", 60, 15, page=1, width=100),
    ]
    result = repair_heading_hierarchy(
        md, content_list, config=HeadingHierarchyConfig(max_levels=2)
    )
    assert "# A" in result.markdown
    assert "## B" in result.markdown
    assert "## C" in result.markdown


def test_text_level_titles_are_used_with_ocr_spacing():
    md = "# 农场手册\n\n## 黑木耳\n\n## 多 糖\n\n正文。\n"
    content_list = [
        {"type": "text", "text": "农场手册", "text_level": 1,
         "page_idx": 0, "bbox": [10.0, 0.0, 300.0, 40.0]},
        {"type": "text", "text": "黑木耳", "text_level": 2,
         "page_idx": 1, "bbox": [80.0, 100.0, 300.0, 28.0]},
        {"type": "text", "text": "多糖", "text_level": 3,
         "page_idx": 1, "bbox": [140.0, 200.0, 200.0, 18.0]},
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "# 农场手册" in result.markdown
    assert "## 黑木耳" in result.markdown
    assert "### 多 糖" in result.markdown
    assert result.report["matched_headings"] == 3


def test_raw_heading_with_context_prefix_matches_layout_title():
    md = "# 农场手册\n\n## 03 有机黑木耳丝系列\n\n正文。\n"
    content_list = [
        {"type": "text", "text": "农场手册", "text_level": 1,
         "page_idx": 0, "bbox": [10.0, 0.0, 300.0, 40.0]},
        {"type": "text", "text": "有机黑木耳丝系列", "text_level": 2,
         "page_idx": 1, "bbox": [80.0, 100.0, 300.0, 20.0]},
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "## 03 有机黑木耳丝系列" in result.markdown
    assert result.report["matched_headings"] == 2


def test_overlong_body_block_is_filtered_from_titles():
    md = "# 农场手册\n\n## 03 有机黑木耳丝系列黑木耳又名黑菜、木耳、云耳，为我国珍贵的药食兼用胶质真菌，也是世界上公认的保健食品。\n\n正文。\n"
    long_text = "03 有机黑木耳丝系列黑木耳又名黑菜、木耳、云耳，为我国珍贵的药食兼用胶质真菌，也是世界上公认的保健食品。"
    content_list = [
        _title("农场手册", 10, 40, page=0, width=300),
        {"type": "title", "text": long_text, "page_idx": 1,
         "bbox": [100.0, 0.0, 900.0, 60.0]},
    ]
    result = repair_heading_hierarchy(
        md, content_list, config=HeadingHierarchyConfig(max_heading_chars=200, max_title_block_chars=50)
    )
    assert result.report["removed_decorative"] == 1
    assert result.report["matched_headings"] == 1
    assert "# 农场手册" in result.markdown


def test_equal_facets_without_leader_keep_original_level():
    md = "# 有机地标农场手册\n\n## 多 糖\n\n## 胶原蛋白\n\n## 黑色素\n"
    content_list = [
        _title("有机地标农场手册", 10, 50, page=0, width=500),
        _title("多 糖", 30, 24, page=7, width=80),
        _title("胶原蛋白", 30, 24, page=7, width=80),
        _title("黑色素", 200, 24, page=7, width=80),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "## 多 糖" in result.markdown
    assert "## 胶原蛋白" in result.markdown
    assert "## 黑色素" in result.markdown
    assert result.report["adjusted_levels"] == 0


def test_page_with_clear_leader_demotes_subitems_to_h3():
    md = "# 有机地标农场手册\n\n## 食用菌全产业链\n\n## 菌种制作\n\n## 木耳种植\n\n## 清洗切丝\n"
    content_list = [
        _title("有机地标农场手册", 10, 50, page=0, width=500),
        _title("食用菌全产业链", 20, 60, page=10, width=900),
        _title("菌种制作", 30, 24, page=10, width=90),
        _title("木耳种植", 200, 24, page=10, width=90),
        _title("清洗切丝", 400, 24, page=10, width=90),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "## 食用菌全产业链" in result.markdown
    assert "### 菌种制作" in result.markdown
    assert "### 木耳种植" in result.markdown
    assert "### 清洗切丝" in result.markdown


def test_first_page_non_title_headings_stay_h2():
    md = "# 有机地标农场手册\n\n## 基地简介\n\n## 使命\n\n## 愿景\n\n## 价值观\n"
    content_list = [
        _title("有机地标农场手册", 10, 50, page=0, width=500),
        _title("基地简介", 20, 120, page=0, width=800),
        _title("使命", 700, 18, page=0, width=50),
        _title("愿景", 700, 18, page=0, width=50),
        _title("价值观", 700, 18, page=0, width=50),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "## 基地简介" in result.markdown
    assert "## 使命" in result.markdown
    assert "## 愿景" in result.markdown
    assert "## 价值观" in result.markdown


def test_spurious_h1_becomes_h2_when_page_has_single_title():
    md = "# 有机地标农场手册\n\n# 羊肚菌营养成分\n\n正文。\n"
    content_list = [
        _title("有机地标农场手册", 10, 50, page=0, width=500),
        _title("羊肚菌营养成分", 60, 30, page=3, width=350),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "# 有机地标农场手册" in result.markdown
    assert "## 羊肚菌营养成分" in result.markdown
    assert result.report["adjusted_levels"] == 1
def test_single_title_pages_after_numbered_chapter_become_h3():
    md = (
        "# 有机地标农场手册\n\n"
        "## \u201c菌中之王\u201d——羊肚菌\n\n"
        "## 羊肚菌营养成分\n\n"
        "## 【基地种植加工实景图】\n\n"
        "## 多款精美礼盒\n\n"
        "## 商务合作\n"
    )
    content_list = [
        {"type": "text", "text": "有机地标农场手册", "text_level": 1,
         "page_idx": 0, "bbox": [10.0, 0.0, 600.0, 60.0]},
        {"type": "text", "text": "01", "page_idx": 1, "bbox": [100.0, 0.0, 200.0, 150.0]},
        {"type": "text", "text": "\u201c菌中之王\u201d——羊肚菌", "text_level": 2,
         "page_idx": 1, "bbox": [20.0, 0.0, 500.0, 80.0]},
        {"type": "text", "text": "羊肚菌营养成分", "text_level": 2,
         "page_idx": 2, "bbox": [100.0, 0.0, 600.0, 80.0]},
        {"type": "text", "text": "【基地种植加工实景图】", "text_level": 2,
         "page_idx": 3, "bbox": [100.0, 0.0, 600.0, 80.0]},
        {"type": "text", "text": "多款精美礼盒", "text_level": 2,
         "page_idx": 4, "bbox": [100.0, 0.0, 500.0, 60.0]},
        {"type": "text", "text": "商务合作", "text_level": 2,
         "page_idx": 5, "bbox": [100.0, 0.0, 400.0, 80.0]},
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "## \u201c菌中之王\u201d——羊肚菌" in result.markdown
    assert "### 羊肚菌营养成分" in result.markdown
    assert "### 【基地种植加工实景图】" in result.markdown
    assert "### 多款精美礼盒" in result.markdown
    assert "## 商务合作" in result.markdown


def test_last_single_title_page_keeps_h2():
    md = "# 有机地标农场手册\n\n## 检测报告与专利\n\n## 商务合作\n"
    content_list = [
        _title("有机地标农场手册", 10, 50, page=0, width=500),
        {"type": "text", "text": "11", "page_idx": 1, "bbox": [100.0, 0.0, 200.0, 150.0]},
        _title("检测报告与专利", 20, 60, page=1, width=400),
        _title("商务合作", 20, 60, page=2, width=300),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "## 检测报告与专利" in result.markdown
    assert "## 商务合作" in result.markdown


def test_number_prefixed_chapter_title_is_not_demoted():
    md = "# 有机地标农场手册\n\n## \u201c菌中之王\u201d——羊肚菌\n\n## 03 有机黑木耳丝系列\n"
    content_list = [
        _title("有机地标农场手册", 10, 50, page=0, width=500),
        {"type": "text", "text": "01", "page_idx": 1, "bbox": [100.0, 0.0, 200.0, 150.0]},
        _title("\u201c菌中之王\u201d——羊肚菌", 20, 40, page=1, width=400),
        _title("03 有机黑木耳丝系列", 20, 50, page=2, width=500),
    ]
    result = repair_heading_hierarchy(md, content_list)
    assert "## \u201c菌中之王\u201d——羊肚菌" in result.markdown
    assert "## 03 有机黑木耳丝系列" in result.markdown
