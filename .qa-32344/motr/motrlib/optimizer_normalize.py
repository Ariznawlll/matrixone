"""Normalize EXPLAIN/AP plan text before strcmp for ``10_optimizer`` baselines."""

from __future__ import annotations

import re

_RE_ROWSIZE = re.compile(r"\browsize=\d+\.\d+")
_RE_CN_CORE = re.compile(r"\bCN\(\s*(?:\d+|N|<N>)\s+core\)")
_RE_ERR_COL = re.compile(r"(syntax error at line 1 column )\d+( near)")
_JOIN_COND_SIMPLE = re.compile(
    r"^(?P<ind>\s*)Join Cond: \((?P<lhs>[^=]+)=\s*(?P<rhs>[^)]+)\)\s*$"
)
_JOIN_ARROW = re.compile(r"^\s*->\s+Join\b")
_FILTER_COND = re.compile(r"^(?P<ind>\s*)Filter Cond:\s*(?P<predicate>\S.*?)\s*$")
_BLOCK_FILTER_COND = re.compile(
    r"^(?P<ind>\s*)Block Filter Cond:\s*(?P<predicate>\S.*?)\s*$"
)


def normalize_optimizer_compare_text(text: str) -> str:
    """Fold unstable planner presentation into deterministic text for strcmp."""

    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = _RE_ROWSIZE.sub("rowsize=<RS>", raw)
        line = _RE_CN_CORE.sub("CN(<N> core)", line)
        line = _RE_ERR_COL.sub(r"\1<C>\2", line)
        line = _canonical_join_cond_line(line)
        line = line.replace(" range unbounded preceding", "")
        lines.append(line)

    return reorder_scan_siblings_under_join(
        _drop_redundant_block_filter_conditions(lines)
    )


def normalize_cn_core_compare_text(text: str) -> str:
    """Fold only CN core-count presentation noise in an EXPLAIN baseline."""

    return _RE_CN_CORE.sub("CN(<N> core)", text)


def _drop_redundant_block_filter_conditions(lines: list[str]) -> list[str]:
    """Remove only a duplicated physical filter annotation.

    Different MatrixOne branches may render a storage-level Block Filter Cond
    immediately after the logical Filter Cond.  The pair is presentation noise
    only when both its indentation and predicate are exactly the same.
    """

    normalized: list[str] = []
    for line in lines:
        block_filter = _BLOCK_FILTER_COND.match(line)
        previous_filter = _FILTER_COND.match(normalized[-1]) if normalized else None
        if (
            block_filter
            and previous_filter
            and block_filter.group("ind") == previous_filter.group("ind")
            and block_filter.group("predicate") == previous_filter.group("predicate")
        ):
            continue
        normalized.append(line)
    return normalized


def _canonical_join_cond_line(line: str) -> str:
    m = _JOIN_COND_SIMPLE.match(line)
    if not m:
        return line
    ind = m.group("ind")
    left = m.group("lhs").strip()
    right = m.group("rhs").strip()
    a, b = sorted([left, right])
    return f"{ind}Join Cond: ({a} = {b})"


_SCAN_LINE = re.compile(r"^\s*->\s+Table Scan\b")


def reorder_scan_siblings_under_join(lines: list[str]) -> str:
    """Sort consecutive sibling ``Table Scan`` blocks beneath a ``-> Join`` node."""

    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if _JOIN_ARROW.match(ln):
            out.append(ln)
            i = _consume_join_children(lines, out, i + 1)
            continue
        out.append(ln)
        i += 1
    return "\n".join(out)


def _consume_join_children(lines: list[str], out: list[str], start: int) -> int:
    """Emit join metadata then sorted ``Table Scan`` sibling blocks."""

    i = start
    n = len(lines)

    while i < n and not lines[i].strip():
        out.append(lines[i])
        i += 1

    while i < n and lines[i].strip():
        st = lines[i].lstrip()
        if _SCAN_LINE.match(lines[i]):
            break
        if st.startswith("->"):
            out.append(lines[i])
            i += 1
            continue
        if st.startswith(("Join Cond:", "Join Type:", "Output:",)
                          ) or st.startswith(("Filter Cond:",
                                              "Block Filter Cond:",
                                              "hashmap")):
            out.append(lines[i])
            i += 1
            continue
        if st.startswith(("Semi ", "Semi\t", "Anti ", "Anti\t")):
            out.append(lines[i])
            i += 1
            continue
        # Other join internals (aliases, casts, ...) — preserve order until scans
        out.append(lines[i])
        i += 1

    if i >= n or not _SCAN_LINE.match(lines[i]):
        return i

    base_indent = _indent(lines[i])

    scans: list[tuple[str, list[str]]] = []
    while i < n and lines[i].strip():
        ind = _indent(lines[i])
        if ind != base_indent or not _SCAN_LINE.match(lines[i]):
            break
        block, i = _collect_indented_block(lines, i)
        scans.append((_scan_sort_key(block), block))

    for _, block in sorted(scans, key=lambda t: t[0]):
        out.extend(block)

    return i


def _indent(raw: str) -> int | None:
    if not raw.strip():
        return None
    return len(raw) - len(raw.lstrip())


def _collect_indented_block(lines: list[str], start_idx: int) -> tuple[list[str], int]:
    block = [lines[start_idx]]
    base = _indent(lines[start_idx])
    i = start_idx + 1
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            block.append(ln)
            i += 1
            continue
        ind = _indent(ln)
        if base is None or ind is None:
            break
        if ind <= base:
            break
        block.append(ln)
        i += 1
    return block, i


def _scan_sort_key(block: list[str]) -> str:
    for ln in block:
        stripped = ln.lstrip()
        stripped = stripped.removeprefix("->").strip().lower()
        if stripped.startswith("table scan"):
            return stripped
    return ""


__all__ = ["normalize_cn_core_compare_text", "normalize_optimizer_compare_text"]
