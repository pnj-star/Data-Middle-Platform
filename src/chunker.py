"""Token-based parent-child chunking engine.

Parent blocks are built with H1-buffer + H2-lock isolation: each H1 is a
permanent context prefix for every H2 under it, and an H2 never merges with
neighbouring sections. An H1 heading is a buffer only: it never becomes a
parent itself, its direct body merges into the first following H2 section,
and an H1 with no H2 under it is dropped. Oversized H2 bodies are cut at the
parent budget
(``max_parent_size`` minus prefix tokens) and every slice keeps the H1+H2
prefix. Children are sliding-window sub-chunks measured in tiktoken tokens;
each child also inherits the H1+H2 prefix (body budget = ``chunk_size`` minus
prefix tokens) so no sub-chunk is ever context-free. Tables are atomic unless
they exceed the child-chunk budget, in which case they are force-split with a
truncation marker.
"""
from __future__ import annotations

import re
import threading
import uuid
from bisect import bisect_left
from dataclasses import dataclass, field

from .logging_config import get_logger

_log = get_logger(__name__)

_encoding = None
_enc_lock = threading.Lock()


def _get_encoding():
    global _encoding
    if _encoding is None:
        with _enc_lock:
            if _encoding is None:
                import tiktoken
                _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding


def count_tokens(text: str) -> int:
    return len(_get_encoding().encode(text))


@dataclass
class ChildChunk:
    content: str
    size: int  # token count
    index: int


@dataclass
class ParentChunk:
    title: str
    content: str
    size: int  # token count
    parent_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    children: list[ChildChunk] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ChunkTree:
    parents: list[ParentChunk]
    images: list[dict[str, str]] = field(default_factory=list)

    @property
    def parent_count(self) -> int:
        return len(self.parents)

    @property
    def child_count(self) -> int:
        return sum(len(p.children) for p in self.parents)


_BOLD_NUM_TITLE_RE = re.compile(r"^\s*\*\*\d+[、.．)）)]+[^*]+\*\*\s*$")
_BULLET_BOLD_TITLE_RE = re.compile(r"^\s*[-*]\s*\*\*[^*]+\*\*\s*$")
_INLINE_BOLD_TITLE_RE = re.compile(r"^\s*(\*\*[^*]+\*\*)([；;：:\s]+)(?=\S)")
_TABLE_ROW_RE = re.compile(r"^\|.+\|$")
_BOUNDARY_WINDOW = 100  # chars to search around target boundary
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def _preprocess_bold_titles(markdown: str) -> str:
    """Split inline bold titles from body text so they can act as boundaries."""
    out: list[str] = []
    for line in markdown.split("\n"):
        out.append(_INLINE_BOLD_TITLE_RE.sub(r"\1\n\2", line))
    return "\n".join(out)


def extract_image_refs(markdown: str) -> list[dict[str, str]]:
    """Collect image alt/ref pairs without keeping them in retrieval text."""
    return [
        {"alt": alt.strip(), "ref": ref.strip()}
        for alt, ref in _MARKDOWN_IMAGE_RE.findall(markdown or "")
    ]


def strip_image_refs(markdown: str) -> str:
    """Remove image syntax from text used for chunking and embedding."""
    text = _MARKDOWN_IMAGE_RE.sub("", markdown or "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    had_trailing_newline = text.endswith("\n")
    return text.strip("\n") + ("\n" if had_trailing_newline else "")



def _is_table_line(line: str) -> bool:
    return bool(_TABLE_ROW_RE.match(line.strip()))


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    return bool(s.startswith("|")) and bool(re.match(r"^[\|\s\-:]+$", s))


def _table_blocks(text: str) -> list[tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    in_block = False
    bs = 0
    idx = 0
    for line in text.split("\n"):
        if _is_table_line(line) or _is_table_separator(line):
            if not in_block:
                bs = idx
                in_block = True
        elif in_block:
            blocks.append((bs, idx))
            in_block = False
        idx += len(line) + 1
    if in_block:
        blocks.append((bs, len(text)))
    return blocks


def _table_block_at(pos: int, blocks: list[tuple[int, int]]) -> tuple[int, int] | None:
    for bs, be in blocks:
        if bs <= pos < be:
            return (bs, be)
    return None


def _table_cut_by(pos: int, blocks: list[tuple[int, int]]) -> tuple[int, int] | None:
    """Return the table block whose *interior* contains *pos* (strict)."""
    for bs, be in blocks:
        if bs < pos < be:
            return (bs, be)
    return None


def _boundary_candidates(text: str) -> list[int]:
    patterns = [r"。", r"\n\n", r"！", r"\n", r"？", r"；", r"，", r"、", r"  "]
    values: list[int] = []
    seen: set[int] = set()
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            value = m.end()
            if value not in seen:
                seen.add(value)
                values.append(value)
    return values


def _find_boundary(text: str) -> int:
    values = _boundary_candidates(text)
    return values[0] if values else 0

def _sentence_boundary_candidates(text: str) -> list[int]:
    """Return character offsets after sentence-ending punctuation."""
    return [m.end() for m in re.finditer(r"[。！？]", text)]


def _hard_token_slices(text: str, max_tokens: int) -> list[str]:
    """Force text into token-budget slices when no natural boundary exists."""
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    enc = _get_encoding()
    token_ids = enc.encode(text)
    return [
        enc.decode(token_ids[start:start + max_tokens])
        for start in range(0, len(token_ids), max_tokens)
    ]


def _split_oversized_table(text: str, start: int, end: int,
                           max_tokens: int) -> list[ChildChunk]:
    segment = text[start:end]
    children: list[ChildChunk] = []
    lines = segment.split("\n")
    buf: list[str] = []
    buf_tokens = 0
    header: str | None = None

    if len(lines) >= 2 and _is_table_line(lines[0]) and _is_table_separator(lines[1]):
        header = "\n".join(lines[:2])
        body_start = 2
    else:
        body_start = 0

    header_tokens = count_tokens(header) if header else 0
    truncation_tokens = count_tokens(" [表格截断]")
    newline_tokens = count_tokens("\n")
    # Keep room for the header separator and marker on non-final fragments;
    # otherwise a 300-token limit can quietly become 313.
    row_budget = max(
        max_tokens - header_tokens - newline_tokens - truncation_tokens, 1)

    for line in lines[body_start:]:
        lt = count_tokens(line + "\n")
        # One huge cell must not turn one "row-sized" child into an oversized
        # embedding. Preserve the header on every partial row for readability.
        if header_tokens + lt > max_tokens:
            if buf:
                content = "\n".join(buf)
                if header:
                    content = header + "\n" + content
                children.append(ChildChunk(
                    content=content.strip() + " [表格截断]",
                    size=count_tokens(content),
                    index=len(children),
                ))
                buf = []
                buf_tokens = 0

            row_slices = _hard_token_slices(line, row_budget)
            for slice_no, row_slice in enumerate(row_slices):
                content = row_slice
                if slice_no < len(row_slices) - 1:
                    content += " [表格截断]"
                if header:
                    content = header + "\n" + content
                children.append(ChildChunk(
                    content=content.strip(),
                    size=count_tokens(content),
                    index=len(children),
                ))
            continue

        projected = buf_tokens + lt + header_tokens + truncation_tokens
        if buf and projected > max_tokens:
            content = "\n".join(buf)
            if header:
                content = header + "\n" + content
            children.append(ChildChunk(
                content=content.strip() + " [表格截断]",
                size=count_tokens(content),
                index=len(children),
            ))
            buf = [line]
            buf_tokens = lt
        else:
            buf.append(line)
            buf_tokens += lt

    if buf:
        content = "\n".join(buf)
        if header:
            content = header + "\n" + content
        children.append(ChildChunk(
            content=content.strip(),
            size=count_tokens(content),
            index=len(children),
        ))
    return children


def _split_into_children(text: str, chunk_size: int, overlap: int,
                         lookback_tokens: int | None = None) -> list[ChildChunk]:
    if not text.strip():
        return []

    enc = _get_encoding()
    # Tokenize the parent once and retain the character offset where each
    # token starts. Re-encoding text[start:] in every loop made chunking cost
    # grow quadratically on large parents.
    token_ids = enc.encode(text)
    _, token_starts = enc.decode_with_offsets(token_ids)
    token_bounds = [*token_starts, len(text)]
    total_tokens = len(token_ids)

    def token_index_at(pos: int) -> int:
        return min(bisect_left(token_bounds, pos), total_tokens)

    blocks = _table_blocks(text)
    lookback = lookback_tokens if lookback_tokens is not None else overlap + 15
    step = max(chunk_size - overlap, 1)
    children: list[ChildChunk] = []
    start = 0
    start_token = 0
    text_len = len(text)

    while start < text_len:
        start_token = token_index_at(start)
        tb = _table_block_at(start, blocks)
        if tb:
            table_text = text[tb[0]:tb[1]]
            t_tokens = count_tokens(table_text)
            if t_tokens <= chunk_size:
                children.append(ChildChunk(
                    content=table_text.strip(), size=t_tokens,
                    index=len(children),
                ))
                start = tb[1]
            else:
                base_index = len(children)
                for offset, child in enumerate(_split_oversized_table(
                        text, tb[0], tb[1], chunk_size)):
                    child.index = base_index + offset
                    children.append(child)
                start = tb[1]
                start_token = token_index_at(start)
            continue

        remaining_tokens = total_tokens - start_token
        if remaining_tokens <= chunk_size:
            end = text_len
        else:
            end = min(token_bounds[start_token + chunk_size], text_len)

        real_end = end
        if end < text_len:
            search_left_token = max(
                start_token + chunk_size - lookback,
                start_token + 1,
            )
            search_left = token_bounds[search_left_token]
            sentence_boundary = max(_sentence_boundary_candidates(
                text[search_left:end]), default=None)
            if sentence_boundary is not None:
                real_end = search_left + sentence_boundary
            else:
                fallback_left_token = max(
                    start_token, search_left_token - lookback)
                fallback_left = token_bounds[fallback_left_token]
                sentence_boundary = max(_sentence_boundary_candidates(
                    text[fallback_left:end]), default=None)
                if sentence_boundary is not None:
                    real_end = fallback_left + sentence_boundary
                else:
                    boundary = max(_boundary_candidates(
                        text[search_left:end]), default=None)
                    if boundary is not None:
                        real_end = search_left + boundary

        chunk_text = text[start:real_end]
        actual = count_tokens(chunk_text)
        if chunk_text.strip() and actual > 0:
            children.append(ChildChunk(
                content=chunk_text.strip(), size=actual,
                index=len(children),
            ))

        if real_end >= text_len:
            break

        next_token = max(token_index_at(real_end) - overlap, 0)
        next_start = token_bounds[next_token]
        if next_start <= start:
            safe_advance = min(
                start_token + max(chunk_size - overlap, 1), total_tokens)
            next_start = token_bounds[safe_advance]
        if next_start <= start:
            next_start = start + step
        start = next_start

    return children


def chunk(
    markdown: str,
    chunk_size: int = 300,
    overlap: int = 40,
    max_parent_size: int = 2000,
    parent_lookback_tokens: int = 80,
    child_lookback_tokens: int | None = None,
) -> ChunkTree:
    """Parent-child chunking of Markdown (H1-buffer + H2-lock isolation).

    Sections are cut only at H1/H2-level boundaries (``## `` headings,
    ``# `` headings and bold titles). Every H2 body keeps the current H1 as a
    permanent prefix; an H2 is never merged with neighbouring sections. H1
    headings never produce parents on their own: their direct body merges into
    the first following H2 section, and an H1 with no H2 under it is dropped.
    When a section exceeds ``max_parent_size`` its body is sliced at the parent
    budget (max_parent_size minus prefix tokens) and each slice keeps the
    prefix.
    Children inherit the same H1+H2 prefix: they are split from the body at
    ``chunk_size`` minus prefix tokens and re-prefixed afterwards.
    """
    markdown = _preprocess_bold_titles(markdown)
    lines = markdown.split("\n")

    # ---- collect sections: (title, body, current_h1) ----
    sections: list[tuple[str, str, str]] = []
    current_title = ""
    current_body: list[str] = []
    current_h1 = ""

    def flush() -> None:
        nonlocal current_title, current_body
        if current_title:
            sections.append(
                (current_title, "\n".join(current_body).strip(), current_h1))
            current_title = ""
            current_body = []

    for line in lines:
        stripped = line.strip()
        is_h1 = bool(re.match(r"^#\s", line))
        is_h2 = bool(re.match(r"^##\s", line))
        is_h1_like = is_h1 or _BULLET_BOLD_TITLE_RE.match(stripped)
        is_section = is_h2 or _BOLD_NUM_TITLE_RE.match(stripped)
        if is_h1_like or is_section:
            flush()
            if is_h1_like:
                current_h1 = stripped
            current_title = stripped
        else:
            current_body.append(line)
    flush()

    if not sections:
        # No headings at all: treat the whole document as one section.
        first_h1 = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
        title = f"# {first_h1.group(1).strip()}" if first_h1 else ""
        sections = [(title, markdown.strip(), "")]

    # ---- build parents: prefix is never cut, body slides ----
    # H1 headings are buffers only: they never become a parent themselves and
    # their direct body merges into the first following H2 section. A document
    # with H1 but no H2 at all produces no parent (an H1-only block is
    # meaningless for retrieval).
    parents: list[ParentChunk] = []
    pending_h1_body: list[str] = []
    for title, body, h1 in sections:
        if h1 and h1 == title:
            # H1-level section (H1 or bullet-bold title): buffer only.
            if body:
                pending_h1_body.append(body)
            continue
        if not body and not pending_h1_body:
            continue  # title-only section: nothing to index
        merged_body = "\n\n".join(
            [*pending_h1_body, body]).strip() if pending_h1_body else body
        pending_h1_body = []
        prefix = f"{h1}\n{title}" if (h1 and h1 != title) else title
        full = f"{prefix}\n{merged_body}".strip()
        if count_tokens(full) <= max_parent_size:
            blocks: list[str] = [merged_body]
        else:
            prefix_tokens = count_tokens(prefix + "\n")
            budget = max(max_parent_size - prefix_tokens, 1)
            slices = _split_into_children(
                merged_body, budget, 0, lookback_tokens=parent_lookback_tokens)
            blocks = [s.content for s in slices if s.content.strip()]
        prefix_tokens = count_tokens(prefix + "\n")
        body_budget = max(chunk_size - prefix_tokens, 1)
        for block in blocks:
            content = f"{prefix}\n{block}".strip()
            images = extract_image_refs(content)
            content = strip_image_refs(content)
            block = strip_image_refs(block)
            ptokens = count_tokens(content)
            children = _split_into_children(
                block, body_budget, overlap,
                lookback_tokens=child_lookback_tokens)
            # Every child inherits the H1+H2 prefix so no sub-chunk is
            # ever context-free (mirrors the parent-side fixed-prefix rule).
            for c in children:
                c.content = f"{prefix}\n{c.content}".strip()
                c.size = count_tokens(c.content)
            parents.append(ParentChunk(
                title=title, content=content, size=ptokens,
                children=children, images=images))

    total_children = sum(len(p.children) for p in parents)
    _log.info("Chunked markdown: %d parents (%d tokens), %d children",
              len(parents), sum(p.size for p in parents), total_children)
    return ChunkTree(
        parents=parents,
        images=[img for parent in parents for img in parent.images],
    )
