"""Conservative Markdown cleaning pass between conversion and chunking.

The cleaner rewrites only high-confidence structure problems:

- bold numbered lines that occupy a full line are promoted to ``##``;
- pure noise headings / oversized sentence-like headings are dropped or
  demoted back to body text;
- nearby duplicate headings are merged only when the gap and the following
  block are image-dominant and text-poor;
- empty heading lines are removed so they never become empty parent blocks;
- page-number residue (``PART 01``, bare ``02``) is removed;
- OCR spacing inside CJK runs and stray ``<sub>/<sup>`` tags are cleaned;
- OCR garbage fragments from failed recognition are dropped when they show
  strong OCR artifacts and are not a page-split continuation;
- long numbered lines with duplicated body text are split into heading + body;
- numbered body line sequences become Markdown lists;
- adjacent repeated paragraph text is deduplicated without crossing sections.

Inline bold phrases (``**注**：正文``), list-item bold text and long
cross-section repeats are deliberately left untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_EMPTY_HEADING_RE = re.compile(r"^#{1,6}\s*$")
_H1_RE = re.compile(r"^#(?!#)\s+(.*)$")
_BOLD_NUM_TITLE_RE = re.compile(r"^\s*\*\*(\d+[、.．)）)]+\s*[^*]+?)\*\*\s*$")
_IMAGE_LINE_RE = re.compile(r"^(?:<!--\s*image\s*-->|!\[[^\]]*\]\([^)]*\))\s*$", re.IGNORECASE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_SENTENCE_END_RE = re.compile(r"[。；;]")
_PAGE_RESIDUE_RE = re.compile(
    r"^\s*(?:(?:PART|PAGE|页)\s*)?0?\d{1,3}(?:\s*[-–]\s*\d{1,3})?\s*$",
    re.IGNORECASE,
)
_NUMBERED_BODY_RE = re.compile(r"^\s*(\d{1,2}(?:[、.．)）]))\s+(\S.*?)\s*$")
_NUMBERED_WORD_BODY_RE = re.compile(r"^\s*(\d{1,2})\s+(\S.*?)\s*$")
_NUMBERED_TIGHT_BODY_RE = re.compile(r"^\s*(\d{1,2})([^\d\s].*?)\s*$")
_CJK_SPACE_RE = re.compile(
    r"(?<=[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef])\s+"
    r"(?=[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef])"
)
_SUBSUP_TAG_RE = re.compile(r"</?(?:sub|sup)>", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^\s*[·•]{1,4}\s*")
_NUM_TITLE_PREFIX_RE = re.compile(r"^\s*\d{1,3}(?:[.、．)）]?)?\s*")
_INVISIBLE_CHAR_MAP = str.maketrans({
    "\u00a0": " ",
    "\u3000": " ",
})
_INVISIBLE_CHAR_RE = re.compile(r"[\u200b\u200c\u200d\u00ad]")
_EXTERNAL_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_EMPTY_LINK_RE = re.compile(r"\[\]\(\)")
_HORIZONTAL_SPACE_RE = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*+]\s+|"
    r"[·•]{1,4}\s+|"
    r"\d{1,3}(?:[、.．）)]|\s)\s*\S)"
)
_OCR_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]\s+|·•{1,4}\s+)")
_OCR_REPEATED_PUNCT_RE = re.compile(r"([，。；：、！？])\1")
_OCR_REPEATED_CJK_RE = re.compile(r"([\u2e80-\u9fff])\1")
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?")
_HTML_TAG_RE = re.compile(r"^\s*</?[a-zA-Z][^>]*>\s*$")
_HORIZONTAL_RULE_RE = re.compile(r"^\s*[-*_](?:[-*_]\s*){2,}$")
_CJK_CHAR_RE = re.compile(r"[\u2e80-\u9fff\u3000-\u303f]")
_TERMINAL_END_RE = re.compile(r"[\u3002\uff01\uff1f\u2026]\s*$")
_BROKEN_BOUNDARY_END_RE = re.compile(r"[\u2e80-\u9fff，、：；％%]\s*$")

CLEANER_VERSION = "1.3.0"


@dataclass
class CleanConfig:
    force_single_h1: bool = False
    max_heading_chars: int = 120
    merge_repeated_headings: bool = True
    max_repeated_gap_lines: int = 16
    max_gap_text_chars: int = 20
    max_repeated_body_text_chars: int = 30
    require_image_gap: bool = True
    require_image_body: bool = True
    allow_inline_bold_heading: bool = False
    noise_terms: list[str] = field(default_factory=lambda: [
        "THANKS", "BUSINESS", "CONTENTS", "TABLE OF CONTENTS", "目录",
        "01", "02", "03", "04", "05", "06", "07", "08", "09", "10",
    ])
    remove_page_residue: bool = True
    split_paged_long_lines: bool = True
    min_paged_split_chars: int = 100
    convert_numbered_bodies: bool = True
    max_numbered_list_chars: int = 360
    dedupe_repeated_text: bool = True
    min_duplicate_text_chars: int = 14
    normalize_ocr_spacing: bool = True
    clean_inline_tags: bool = True
    normalize_list_bullets: bool = True
    clean_invisible_chars: bool = True
    collapse_whitespace: bool = True
    clean_external_links: bool = True
    remove_ocr_garbage_fragments: bool = True
    min_ocr_garbage_chars: int = 4
    max_ocr_garbage_chars: int = 48
    merge_soft_wraps: bool = True
    merge_broken_sentences: bool = True
    max_merged_line_chars: int = 600
    min_broken_merge_chars: int = 8
    layout_mode: bool = False


@dataclass
class CleanResult:
    markdown: str
    report: dict[str, Any]


def _normalize_noise(text: str) -> str:
    return re.sub(r"\s+", "", text or "").upper()


def _heading_text(line: str) -> str | None:
    m = _HEADING_RE.match(line)
    if not m:
        return None
    return m.group(2).strip()


def _record(report: dict[str, Any], kind: str, line: str) -> None:
    report["counts"][kind] += 1
    if len(report["details"]) < 200:
        report["details"].append({"kind": kind, "line": line[:200]})


def _visible_text(lines: list[str]) -> str:
    """Return body text after dropping blank lines and image markers."""
    pieces: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or _IMAGE_LINE_RE.match(s):
            continue
        s = _MARKDOWN_IMAGE_RE.sub("", s).strip()
        if s:
            pieces.append(s)
    return "".join(pieces)


def _code_fence_indices(lines: list[str]) -> set[int]:
    """Return line indices inside fenced code, so cleaners leave code alone."""
    guarded: set[int] = set()
    active = False
    for idx, line in enumerate(lines):
        if line.strip().startswith("```"):
            active = not active
            continue
        if active:
            guarded.add(idx)
    return guarded


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Normalize text for duplicate detection and map chars back to original."""
    normalized: list[str] = []
    pairs: list[int] = []
    for idx, ch in enumerate(text):
        if ch.isspace():
            continue
        normalized.append(ch.lower() if ch.isascii() else ch)
        pairs.append(idx)
    return "".join(normalized), pairs


def _find_adjacent_duplicate(
    text: str, min_len: int = 14
) -> tuple[int, int, int, list[int]] | None:
    """Return ``(start1, start2, length, pairs)`` for an adjacent repeat."""
    norm, pairs = _norm_map(text)
    n = len(norm)
    if n < min_len * 2:
        return None
    max_len = min(120, n // 2)
    for k in range(max_len, min_len - 1, -1):
        limit = n - 2 * k + 1
        for start in range(limit):
            if norm[start:start + k] == norm[start + k:start + 2 * k]:
                return start, start + k, k, pairs
    return None


def _convert_bold_headings(lines: list[str], config: CleanConfig,
                           report: dict[str, Any]) -> list[str]:
    if config.layout_mode:
        return lines
    out: list[str] = []
    for line in lines:
        m = _BOLD_NUM_TITLE_RE.match(line)
        if m:
            title = m.group(1).strip()
            if 0 < len(title) <= config.max_heading_chars:
                out.append(f"## {title}")
                _record(report, "converted_bold_headings", line)
                continue
        out.append(line)
    return out


def _demote_junk_headings(lines: list[str], config: CleanConfig,
                          report: dict[str, Any]) -> list[str]:
    noise = {_normalize_noise(t) for t in config.noise_terms if t}
    out: list[str] = []
    for line in lines:
        text = _heading_text(line)
        if text is None:
            out.append(line)
            continue
        normalized = _normalize_noise(text)
        pure_page_number = bool(re.fullmatch(r"0?\d{1,2}", normalized))
        page_residue = bool(_PAGE_RESIDUE_RE.match(text))
        if normalized in noise or pure_page_number or page_residue:
            _record(report, "removed_noise_headings", line)
            continue
        too_long = len(text) > config.max_heading_chars
        sentence_like = len(text) >= 30 and bool(_SENTENCE_END_RE.search(text))
        if too_long or sentence_like:
            out.append(text)
            _record(report, "demoted_heading_lines", line)
            continue
        out.append(line)
    return out


def _remove_empty_headings(lines: list[str], report: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if _EMPTY_HEADING_RE.match(line):
            _record(report, "removed_empty_headings", line)
            continue
        out.append(line)
    return out


def _normalize_ocr_and_tags(lines: list[str], config: CleanConfig,
                            report: dict[str, Any],
                            guarded: set[int]) -> list[str]:
    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in guarded or _IMAGE_LINE_RE.match(line.strip()):
            out.append(line)
            continue
        cleaned = line
        if config.clean_inline_tags:
            cleaned = _SUBSUP_TAG_RE.sub("", cleaned)
            if cleaned != line:
                _record(report, "cleaned_inline_tags", line)
        if config.normalize_ocr_spacing:
            spaced = _CJK_SPACE_RE.sub("", cleaned)
            if spaced != cleaned:
                _record(report, "normalized_ocr_spacing", cleaned)
                cleaned = spaced
        if config.clean_invisible_chars:
            invisible = _INVISIBLE_CHAR_RE.sub("", cleaned)
            invisible = invisible.translate(_INVISIBLE_CHAR_MAP)
            if invisible != cleaned:
                _record(report, "removed_invisible_chars", line)
                cleaned = invisible
        if config.collapse_whitespace and not _TABLE_ROW_RE.match(cleaned):
            collapsed = _HORIZONTAL_SPACE_RE.sub(" ", cleaned)
            if collapsed != cleaned:
                _record(report, "collapsed_whitespace", cleaned)
                cleaned = collapsed
        out.append(cleaned)
    return out


def _remove_page_residue(lines: list[str], config: CleanConfig,
                         report: dict[str, Any],
                         guarded: set[int]) -> list[str]:
    if not config.remove_page_residue:
        return lines
    out: list[str] = []
    for idx, line in enumerate(lines):
        if (
            idx not in guarded
            and _heading_text(line) is None
            and not _IMAGE_LINE_RE.match(line.strip())
            and _PAGE_RESIDUE_RE.match(line.strip())
        ):
            _record(report, "removed_page_residue", line)
            continue
        out.append(line)
    return out


def _is_structural_line(line: str, guarded: set[int], idx: int) -> bool:
    """True when a line must never be merged with neighboring text."""
    s = line.strip()
    if idx in guarded or not s:
        return True
    if _heading_text(line) is not None:
        return True
    if _IMAGE_LINE_RE.match(s):
        return True
    if _MARKDOWN_IMAGE_RE.search(line) or _TABLE_ROW_RE.match(line):
        return True
    if _BULLET_PREFIX_RE.match(s):
        return True
    if _LIST_ITEM_RE.match(line):
        return True
    if _BLOCKQUOTE_RE.match(line) or _HTML_TAG_RE.match(line):
        return True
    if _HORIZONTAL_RULE_RE.match(line):
        return True
    return False


def _is_plain_prose(line: str, guarded: set[int], idx: int) -> bool:
    return not _is_structural_line(line, guarded, idx)
def _is_ocr_continuation(lines: list[str], start: int) -> bool:
    """Return True when the next body line continues a page-split sentence."""
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if not s:
            continue
        if (
            _heading_text(lines[j]) is not None
            or _IMAGE_LINE_RE.match(s)
            or _TABLE_ROW_RE.match(lines[j])
            or _LIST_ITEM_RE.match(lines[j])
            or _BLOCKQUOTE_RE.match(lines[j])
            or _HTML_TAG_RE.match(lines[j])
            or _HORIZONTAL_RULE_RE.match(lines[j])
        ):
            return False
        return bool(_CJK_CHAR_RE.match(s[0]))
    return False


def _remove_ocr_garbage_fragments(lines: list[str], config: CleanConfig,
                                  report: dict[str, Any],
                                  guarded: set[int]) -> list[str]:
    """Drop OCR failure fragments before they enter the chunker.

    The detector is deliberately conservative: strong artifacts are repeated
    sub/sup tags, doubled punctuation, repeated CJK runs, or many single-char
    CJK gaps. A weak short fragment is kept when a following plain CJK line
    looks like a page-split continuation.
    """
    if not config.remove_ocr_garbage_fragments:
        return lines
    out: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if (
            idx in guarded
            or not stripped
            or _heading_text(line) is not None
            or _IMAGE_LINE_RE.match(stripped)
            or _MARKDOWN_IMAGE_RE.search(line)
            or _TABLE_ROW_RE.match(line)
            or _BLOCKQUOTE_RE.match(line)
            or _HTML_TAG_RE.match(line)
            or _HORIZONTAL_RULE_RE.match(line)
        ):
            out.append(line)
            continue
        content = _OCR_LIST_MARKER_RE.sub("", stripped, count=1)
        compact = _SUBSUP_TAG_RE.sub("", content)
        compact = _CJK_SPACE_RE.sub("", compact)
        compact = _INVISIBLE_CHAR_RE.sub("", compact)
        compact = _HORIZONTAL_SPACE_RE.sub(" ", compact)
        compact = re.sub(r"\s+", "", compact)
        length = len(compact)
        if (
            length < config.min_ocr_garbage_chars
            or length > config.max_ocr_garbage_chars
        ):
            out.append(line)
            continue
        has_terminal = bool(_TERMINAL_END_RE.search(compact))
        has_repeated_punct = bool(_OCR_REPEATED_PUNCT_RE.search(compact))
        has_repeated_cjk = bool(_OCR_REPEATED_CJK_RE.search(compact))
        fragment_spaces = len(_CJK_SPACE_RE.findall(content))
        strong = has_repeated_punct or (
            has_repeated_cjk and fragment_spaces >= 1
        )
        weak_fragment = fragment_spaces >= 2 or (
            "，" in compact and fragment_spaces == 1
        )
        if has_terminal:
            junk = strong and fragment_spaces >= 2
        else:
            junk = strong or (
                weak_fragment
                and not _is_ocr_continuation(lines, idx)
            )
        if junk:
            _record(report, "removed_ocr_garbage_fragments", line)
            continue
        out.append(line)
    return out


def _join_wrapped(a: str, b: str) -> str:
    a = a.rstrip()
    b = b.lstrip()
    if not a or not b:
        return (a + b).strip()
    if _CJK_CHAR_RE.search(a[-1:]) and _CJK_CHAR_RE.search(b[:1]):
        return a + b
    return a + " " + b


def _merge_wrapped_lines(lines: list[str], config: CleanConfig,
                         report: dict[str, Any],
                         guarded: set[int]) -> list[str]:
    """Merge PDF hard wraps and page-split sentences into real paragraphs.

    Adjacent plain-text lines without a blank line are joined as one soft
    wrap. A blank-separated paragraph is joined only when the previous line
    ends mid-sentence at a CJK boundary and the next paragraph also starts
    with CJK text, so short captions like ``基地`` / ``直发`` stay separate.
    Headings, lists, tables, images and code fences never merge.
    """
    if not config.merge_soft_wraps and not config.merge_broken_sentences:
        return lines
    out: list[str] = []
    saw_blank = True
    last_text = ""
    last_plain = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            saw_blank = True
            continue
        if _is_structural_line(line, guarded, idx):
            out.append(line)
            saw_blank = False
            last_text = line
            last_plain = False
            continue
        prev = last_text
        if (
            last_plain
            and not saw_blank
            and config.merge_soft_wraps
            and len(prev.strip()) + len(stripped) <= config.max_merged_line_chars
        ):
            out[-1] = _join_wrapped(prev, line)
            last_text = out[-1]
            last_plain = True
            saw_blank = False
            _record(report, "merged_soft_wraps", line)
            continue
        if (
            last_plain
            and saw_blank
            and config.merge_broken_sentences
            and len(prev.strip()) + len(stripped) >= config.min_broken_merge_chars
            and len(prev.strip()) + len(stripped) <= config.max_merged_line_chars
            and _BROKEN_BOUNDARY_END_RE.search(prev.strip())
            and _CJK_CHAR_RE.search(stripped[:1])
        ):
            j = len(out) - 1
            while j >= 0 and not out[j].strip():
                j -= 1
            joined = _join_wrapped(prev, line)
            if j >= 0:
                out[j] = joined
                if j + 1 < len(out) and not out[j + 1].strip():
                    out.pop(j + 1)
            else:
                out.append(joined)
            last_text = joined
            last_plain = True
            saw_blank = False
            _record(report, "merged_broken_sentences", line)
            continue
        out.append(line)
        last_text = line
        last_plain = True
        saw_blank = False
    return out


def _clean_external_links(lines: list[str], config: CleanConfig,
                          report: dict[str, Any],
                          guarded: set[int]) -> list[str]:
    """Keep visible link text but drop external URL markup and empty links."""
    if not config.clean_external_links:
        return lines
    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in guarded:
            out.append(line)
            continue
        changed = _EXTERNAL_LINK_RE.sub(
            lambda m: m.group(1).strip(), line)
        changed = _EMPTY_LINK_RE.sub("", changed)
        if changed != line:
            _record(report, "cleaned_link_markup", line)
        out.append(changed)
    return out


def _merge_repeated_headings(lines: list[str], config: CleanConfig,
                             report: dict[str, Any]) -> list[str]:
    """Merge repeated headings that are near, image-dominant and text-poor.

    A repeated heading is only removed when:
    - it appears within a small line window;
    - the gap contains almost no visible text and at least one image;
    - the block directly after the duplicate is also short, image-dominant.
    Same-named headings separated by real sections are never merged.
    """
    if not config.merge_repeated_headings or config.max_repeated_gap_lines <= 0:
        return lines
    removed: set[int] = set()
    i = 0
    while i < len(lines):
        text = _heading_text(lines[i])
        if not text:
            i += 1
            continue
        limit = min(len(lines), i + 1 + config.max_repeated_gap_lines)
        for j in range(i + 1, limit):
            if j in removed:
                continue
            if _heading_text(lines[j]) != text:
                continue
            gap_lines = lines[i + 1:j]
            gap_text = _visible_text(gap_lines)
            gap_images = sum(
                1 for line in gap_lines if _IMAGE_LINE_RE.match(line.strip())
            )
            body_lines: list[str] = []
            for k in range(j + 1, min(len(lines), j + 1 + 30)):
                if _heading_text(lines[k]) is not None:
                    break
                body_lines.append(lines[k])
            body_text = _visible_text(body_lines)
            body_images = sum(
                1 for line in body_lines if _IMAGE_LINE_RE.match(line.strip())
            )
            body_short = (
                len(body_text) <= config.max_repeated_body_text_chars
                and not _SENTENCE_END_RE.search(body_text)
            )
            gap_image_ok = not config.require_image_gap or gap_images > 0
            body_image_ok = not config.require_image_body or body_images > 0
            if (
                len(gap_text) <= config.max_gap_text_chars
                and body_short
                and gap_image_ok
                and body_image_ok
            ):
                removed.add(j)
                _record(report, "merged_repeated_headings", lines[j])
            break
        i += 1
    return [line for idx, line in enumerate(lines) if idx not in removed]


def _convert_numbered_bodies_to_list(lines: list[str], config: CleanConfig,
                                     report: dict[str, Any],
                                     guarded: set[int]) -> list[str]:
    """Convert short numbered body lines when a small local window repeats."""
    if not config.convert_numbered_bodies:
        return lines

    def numbered_line(line: str) -> bool:
        if not line.strip() or _heading_text(line) is not None:
            return False
        if _IMAGE_LINE_RE.match(line.strip()):
            return False
        m = (
            _NUMBERED_BODY_RE.match(line)
            or _NUMBERED_WORD_BODY_RE.match(line)
            or _NUMBERED_TIGHT_BODY_RE.match(line)
        )
        if not m:
            return False
        body = m.group(2)
        if len(body) > config.max_numbered_list_chars:
            return False
        if re.fullmatch(r"[\d\s.,;:，。；：、]+", body):
            return False
        return True

    matches = [
        idx
        for idx, line in enumerate(lines)
        if idx not in guarded and numbered_line(line)
    ]
    targets: set[int] = set()
    for idx in matches:
        nearby = [
            j for j in range(max(0, idx - 8), min(len(lines), idx + 9))
            if j != idx and j in matches
        ]
        if nearby:
            targets.add(idx)

    out = list(lines)
    for idx in sorted(targets):
        original = out[idx]
        out[idx] = f"- {original.strip()}"
        _record(report, "converted_numbered_bodies", original)
    return out


def _split_paged_long_lines(lines: list[str], config: CleanConfig,
                            report: dict[str, Any],
                            guarded: set[int]) -> list[str]:
    """Split ``03 标题 + duplicated long body`` into a heading and one body."""
    if not config.split_paged_long_lines:
        return lines
    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in guarded or _IMAGE_LINE_RE.match(line.strip()):
            out.append(line)
            continue
        heading = _heading_text(line)
        content = line
        if heading is not None:
            m = _HEADING_RE.match(line)
            if m:
                content = m.group(2)
        if len(content) < config.min_paged_split_chars:
            out.append(line)
            continue
        m = _NUMBERED_WORD_BODY_RE.match(content) or _NUMBERED_BODY_RE.match(content)
        if not m:
            out.append(line)
            continue
        found = _find_adjacent_duplicate(
            content, min_len=config.min_duplicate_text_chars
        )
        if not found:
            out.append(line)
            continue
        start1, start2, length, pairs = found
        if start1 <= 0 or start2 + length > len(pairs):
            out.append(line)
            continue
        title = _NUM_TITLE_PREFIX_RE.sub("", content[:pairs[start1]]).strip()
        if (
            not title
            or len(title) > config.max_heading_chars
            or _SENTENCE_END_RE.search(title)
            or re.fullmatch(r"[\d\s]+", title)
        ):
            out.append(line)
            continue
        first_start = pairs[start1]
        dup_start = pairs[start2]
        dup_end = pairs[start2 + length - 1] + 1
        body = (content[first_start:dup_start] + content[dup_end:]).strip()
        if len(body) < 20:
            out.append(line)
            continue
        out.append(f"## {title}")
        out.append(body)
        _record(report, "split_paged_long_lines", line)
    return out


def _dedupe_paragraph_repeats(lines: list[str], config: CleanConfig,
                              report: dict[str, Any],
                              guarded: set[int]) -> list[str]:
    if not config.dedupe_repeated_text:
        return lines
    out: list[str] = []
    previous_body: str | None = None
    for idx, line in enumerate(lines):
        if (
            idx in guarded
            or _heading_text(line) is not None
            or _IMAGE_LINE_RE.match(line.strip())
            or not line.strip()
        ):
            previous_body = None
            out.append(line)
            continue
        if previous_body is not None and line.strip() == previous_body:
            _record(report, "removed_repeated_paragraph_text", line)
            continue
        changed = line
        found = _find_adjacent_duplicate(
            line, min_len=config.min_duplicate_text_chars
        )
        if found:
            _start1, start2, length, pairs = found
            if start2 + length <= len(pairs):
                dup_start = pairs[start2]
                dup_end = pairs[start2 + length - 1] + 1
                changed = line[:dup_start] + line[dup_end:]
                if changed != line:
                    _record(report, "removed_repeated_paragraph_text", line)
        previous_body = changed.strip()
        out.append(changed)
    return out


def _normalize_list_bullets(lines: list[str], config: CleanConfig,
                            report: dict[str, Any],
                            guarded: set[int]) -> list[str]:
    if not config.normalize_list_bullets:
        return lines
    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in guarded or _heading_text(line) is not None:
            out.append(line)
            continue
        changed = _BULLET_PREFIX_RE.sub("- ", line, count=1)
        if changed != line:
            _record(report, "normalized_list_bullets", line)
        out.append(changed)
    return out


def _force_single_h1(lines: list[str], report: dict[str, Any]) -> list[str]:
    out: list[str] = []
    h1_seen = 0
    for line in lines:
        m = _H1_RE.match(line)
        if not m:
            out.append(line)
            continue
        h1_seen += 1
        if h1_seen == 1:
            out.append(line)
        else:
            out.append(f"## {m.group(1).strip()}")
            _record(report, "demoted_h1_after_first", line)
    return out


def clean_markdown(
    markdown: str,
    *,
    extension: str = "",
    force_single_h1: bool | None = None,
    config: CleanConfig | None = None,
) -> CleanResult:
    """Clean ``markdown`` and return the new text plus an action report."""
    cfg = config or CleanConfig()
    force_h1 = cfg.force_single_h1 if force_single_h1 is None else force_single_h1
    # PPT / promo decks are slide-oriented: a single H1 keeps parent blocks
    # scoped to the document instead of every slide title becoming a parent.
    if extension.lower() == ".pptx":
        force_h1 = True

    report: dict[str, Any] = {
        "version": CLEANER_VERSION,
        "counts": {
            "converted_bold_headings": 0,
            "demoted_heading_lines": 0,
            "removed_noise_headings": 0,
            "removed_empty_headings": 0,
            "merged_repeated_headings": 0,
            "demoted_h1_after_first": 0,
            "removed_page_residue": 0,
            "removed_ocr_garbage_fragments": 0,
            "split_paged_long_lines": 0,
            "converted_numbered_bodies": 0,
            "removed_repeated_paragraph_text": 0,
            "normalized_ocr_spacing": 0,
            "cleaned_inline_tags": 0,
            "normalized_list_bullets": 0,
            "merged_soft_wraps": 0,
            "merged_broken_sentences": 0,
            "removed_invisible_chars": 0,
            "collapsed_whitespace": 0,
            "cleaned_link_markup": 0,
        },
        "details": [],
    }
    source_lines = markdown.splitlines()
    lines = _convert_bold_headings(source_lines, cfg, report)
    lines = _demote_junk_headings(lines, cfg, report)
    lines = _remove_empty_headings(lines, report)
    guarded = _code_fence_indices(lines)
    lines = _remove_ocr_garbage_fragments(lines, cfg, report, guarded)
    guarded = _code_fence_indices(lines)
    lines = _normalize_ocr_and_tags(lines, cfg, report, guarded)
    lines = _remove_page_residue(lines, cfg, report, guarded)
    guarded = _code_fence_indices(lines)
    lines = _merge_wrapped_lines(lines, cfg, report, guarded)
    lines = _clean_external_links(lines, cfg, report, guarded)
    lines = _merge_repeated_headings(lines, cfg, report)
    lines = _convert_numbered_bodies_to_list(lines, cfg, report, guarded)
    lines = _split_paged_long_lines(lines, cfg, report, guarded)
    lines = _dedupe_paragraph_repeats(lines, cfg, report, guarded)
    lines = _normalize_list_bullets(lines, cfg, report, guarded)
    if force_h1:
        lines = _force_single_h1(lines, report)

    cleaned = "\n".join(lines)
    if markdown.endswith(("\n", "\r")):
        cleaned += "\n"
    return CleanResult(markdown=cleaned, report=report)
