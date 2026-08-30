"""Re-level existing Markdown headings from MinerU layout artifacts.

The layout model decides whether a block is a title.  This module only
changes the level (H1/H2/H3) of headings that already exist in the Markdown;
it never promotes body text into a heading.

Strategy:
- Layout candidates are trusted only as titles; oversized body blocks that
  MinerU merged into a title block are dropped before matching.
- The first Markdown heading is treated as the document title (H1). Other
  headings on that page are H2.
- On later pages, a clearly dominant title is H2 and equally-sized subitems
  are H3. Pages without a clear leader keep MinerU's level; card grids with
  four or more equal tiles are treated as subitems (H3).
- Pages that follow a numbered chapter lead (e.g. ``01`` + chapter title)
  and contain only a small single title are chapter sub-pages (H3) until the
  next numbered chapter starts.
- Pure decorative page numbers such as ``01`` / ``11`` are removed from the
  heading flow entirely.
"""
from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HeadingHierarchyConfig:
    max_heading_chars: int = 120
    max_levels: int = 3
    # A page heading must be this many times larger than the second largest
    # heading before it is treated as a section leader (H2).
    min_area_ratio_h2: float = 1.25
    # Corner cases where MinerU merges a body paragraph into a title block.
    max_title_block_chars: int = 200
    # Pages with at least this many equal-sized tiles and no clear leader
    # are demoted to H3 (card grids instead of real sections).
    min_card_grid_items: int = 4
    # A single-title page after a numbered chapter lead page is treated as a
    # chapter sub-page (H3) until the next numbered chapter starts.
    demote_single_after_numbered_chapter: bool = True
    numbered_anchor_max_chars: int = 4


@dataclass
class HierarchyResult:
    markdown: str
    headings: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NOISE_RE = re.compile(r"^(?:part\s*)?\d{1,3}$|^(?:thanks|business|contents)$", re.I)
_NUMBER_PREFIX_RE = re.compile(r"^(?:part\s*)?\d{1,3}\s", re.I)


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "").lower()


def _load_payload(content_list: Any) -> Any:
    if isinstance(content_list, str):
        try:
            return _json.loads(content_list)
        except Exception:
            return []
    if isinstance(content_list, dict):
        return content_list
    return content_list


def _layout_titles(content_list: Any) -> list[dict[str, Any]]:
    """Extract title blocks from content_list/middle_json, trusting MinerU."""
    items: list[dict[str, Any]] = []
    payload = _load_payload(content_list)
    if isinstance(payload, dict):
        payload = payload.get("content_list") or payload.get("pdf_info") or []
    if not isinstance(payload, list):
        return items

    for item in payload:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        is_title = item.get("type") == "title" or bool(item.get("text_level"))
        if not is_title:
            continue
        bbox = item.get("bbox") or item.get("box") or []
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        page_idx = int(item.get("page_idx", 0) or 0)
        items.append(
            {
                "text": text,
                "page_idx": page_idx,
                "bbox": [float(v) for v in bbox[:4]],
            }
        )
    return items


def _original_level(headings: list[tuple[int, str, str]], text: str) -> int:
    norm_text = _normalize(text)
    for _idx, prefix, heading_text in headings:
        if _normalize(heading_text) == norm_text:
            return len(prefix)
    return 2


def _has_number_marker(text: str, max_chars: int) -> bool:
    text = text.strip()
    if len(text) > max_chars and not _NUMBER_PREFIX_RE.match(text):
        return False
    return bool(_NOISE_RE.match(text) or _NUMBER_PREFIX_RE.match(text))


def repair_heading_hierarchy(
    markdown: str,
    content_list: Any,
    middle_json: Any = None,
    *,
    config: HeadingHierarchyConfig | None = None,
) -> HierarchyResult:
    cfg = config or HeadingHierarchyConfig()
    payload = _load_payload(content_list)
    titles = _layout_titles(payload)
    raw_items: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        payload = payload.get("content_list") or payload.get("pdf_info") or []
    if isinstance(payload, list):
        raw_items = [item for item in payload if isinstance(item, dict)]

    lines = markdown.splitlines()
    headings: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            headings.append((idx, m.group(1), m.group(2)))

    report: dict[str, Any] = {
        "source": "content_list",
        "layout_title_count": len(titles),
        "markdown_heading_count": len(headings),
        "matched_headings": 0,
        "adjusted_levels": 0,
        "removed_decorative": 0,
        "clusters": 0,
    }
    if not titles or not headings:
        return HierarchyResult(markdown=markdown, headings=[], report=report)

    # Drop blocks that MinerU misclassified as titles before they can match a
    # Markdown heading: concatenated body paragraphs and pure page numbers.
    title_candidates: list[dict[str, Any]] = []
    for title in titles:
        if len(title["text"]) > cfg.max_title_block_chars or _NOISE_RE.match(title["text"]):
            report["removed_decorative"] += 1
            continue
        title_candidates.append(title)

    norm_to_title: dict[str, dict[str, Any]] = {}
    for title in title_candidates:
        norm_to_title[_normalize(title["text"])] = title

    matched: list[tuple[int, dict[str, Any]]] = []
    remove_line: set[int] = set()
    for line_idx, _prefix, text in headings:
        key = _normalize(text)
        title = norm_to_title.get(key)
        if not title and len(key) >= 2:
            title = next(
                (
                    cand
                    for cand_key, cand in norm_to_title.items()
                    if key in cand_key or cand_key in key
                ),
                None,
            )
        if title is None:
            continue
        if _NOISE_RE.match(text):
            remove_line.add(line_idx)
            report["removed_decorative"] += 1
            continue
        if len(text) > cfg.max_heading_chars:
            continue
        matched.append((line_idx, title))
    report["matched_headings"] = len(matched)
    if not matched:
        out = list(lines)
        for line_idx in remove_line:
            out[line_idx] = ""
        return HierarchyResult(markdown="\n".join(out), headings=[], report=report)

    page_groups: dict[int, list[int]] = {}
    for group_idx, (line_idx, item) in enumerate(matched):
        page_groups.setdefault(item["page_idx"], []).append(group_idx)

    def _area(group_idx: int) -> float:
        bbox = matched[group_idx][1]["bbox"]
        return max(bbox[3] - bbox[1], 1.0) * max(bbox[2] - bbox[0], 1.0)

    def _page_has_number(page: int) -> bool:
        for item in titles:
            if item["page_idx"] == page and _has_number_marker(item["text"], cfg.numbered_anchor_max_chars):
                return True
        for item in raw_items:
            if int(item.get("page_idx", 0) or 0) != page:
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if text and _has_number_marker(text, cfg.numbered_anchor_max_chars):
                return True
        return False

    level_for_line: dict[int, int] = {}
    heading_details: list[dict[str, Any]] = []
    first_page = min(page_groups)
    last_page = max(page_groups)
    last_numbered_chapter: int | None = None
    for page, ids in page_groups.items():
        ids = sorted(ids, key=lambda i: (-_area(i), -matched[i][1]["bbox"][0]))
        dominant = ids[0]
        dominant_area = _area(dominant)
        second_area = max((_area(i) for i in ids[1:]), default=0.0)
        ratio = dominant_area / second_area if second_area > 0.0 else float("inf")

        if page == first_page:
            first_heading_line = headings[0][0]
            for group_idx in ids:
                line_idx, item = matched[group_idx]
                level = 1 if line_idx == first_heading_line else 2
                level_for_line[line_idx] = min(level, cfg.max_levels)
                heading_details.append(
                    {"line": line_idx, "level": level, "text": item["text"], "page_idx": page, "cluster": 0 if level == 1 else 1}
                )
            if first_heading_line not in level_for_line:
                level_for_line[first_heading_line] = 1
            if _page_has_number(page):
                last_numbered_chapter = page
            continue

        if (
            cfg.demote_single_after_numbered_chapter
            and len(ids) == 1
            and page != last_page
            and last_numbered_chapter is not None
            and not _page_has_number(page)
        ):
            line_idx, item = matched[dominant]
            level = 3
            level_for_line[line_idx] = min(level, cfg.max_levels)
            heading_details.append(
                {"line": line_idx, "level": level, "text": item["text"], "page_idx": page, "cluster": 1}
            )
        elif ratio >= cfg.min_area_ratio_h2:
            for group_idx in ids:
                line_idx, item = matched[group_idx]
                level = 2 if group_idx == dominant else 3
                level_for_line[line_idx] = min(level, cfg.max_levels)
                heading_details.append(
                    {"line": line_idx, "level": level, "text": item["text"], "page_idx": page, "cluster": 0 if group_idx == dominant else 1}
                )
        elif len(ids) >= cfg.min_card_grid_items:
            for group_idx in ids:
                line_idx, item = matched[group_idx]
                level = 3
                level_for_line[line_idx] = min(level, cfg.max_levels)
                heading_details.append(
                    {"line": line_idx, "level": level, "text": item["text"], "page_idx": page, "cluster": 1}
                )
        else:
            for group_idx in ids:
                line_idx, item = matched[group_idx]
                level = _original_level(headings, item["text"])
                if level < 2:
                    level = 2
                level_for_line[line_idx] = min(level, cfg.max_levels)
                heading_details.append(
                    {"line": line_idx, "level": level, "text": item["text"], "page_idx": page, "cluster": 1}
                )

        if _page_has_number(page):
            last_numbered_chapter = page

    distinct_levels = {d["level"] for d in heading_details}
    report["clusters"] = len(distinct_levels)

    out = list(lines)
    adjusted = 0
    for line_idx, prefix, text in headings:
        if line_idx in remove_line or _NOISE_RE.match(text):
            if line_idx not in remove_line:
                report["removed_decorative"] += 1
            out[line_idx] = ""
            continue
        new_level = level_for_line.get(line_idx)
        if not new_level:
            continue
        new_prefix = "#" * new_level
        if new_prefix != prefix:
            out[line_idx] = f"{new_prefix} {text}"
            adjusted += 1
    report["adjusted_levels"] = adjusted
    return HierarchyResult(markdown="\n".join(out), headings=heading_details, report=report)
