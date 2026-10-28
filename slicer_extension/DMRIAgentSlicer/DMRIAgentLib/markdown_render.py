"""Small, deterministic Markdown renderer for Slicer's Qt rich-text subset."""

from __future__ import annotations

import html
import re
from typing import Optional


_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_UNORDERED_RE = re.compile(r"^\s*[-+*]\s+(.+?)\s*$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```\s*([\w.+-]*)\s*$")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def _render_plain_inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\[([^\]]+)]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<strong>\1</strong>", escaped)
    return escaped


def render_inline(text: str) -> str:
    """Escape untrusted text while preserving common inline Markdown."""

    parts = re.split(r"(`[^`\n]+`)", text)
    rendered: list[str] = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            rendered.append(f"<code>{html.escape(part[1:-1], quote=False)}</code>")
        else:
            rendered.append(_render_plain_inline(part))
    return "".join(rendered)


def _table_cells(line: str) -> list[str]:
    # Pipes inside inline code or escaped pipes are cell content, not delimiters.
    cells, current, in_code = [], [], False
    escaped = False
    for char in line.strip().strip("|"):
        if escaped:
            if char != "|":
                current.append("\\")
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            in_code = not in_code
            current.append(char)
        elif char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return "|" in line and len(cells) > 1 and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def _render_table(rows: list[list[str]]) -> str:
    width = len(rows[0])
    rendered = []
    for index, row in enumerate(rows):
        cells = (row + [""] * width)[:width]
        tag = "th" if index == 0 else "td"
        background = "#1E293B" if index == 0 else "#111827"
        rendered.append("<tr>" + "".join(
            f'<{tag} align="left" style="background:{background};color:#E2E8F0;'
            f'border-bottom:1px solid #334155;font-size:12px;">{render_inline(cell)}</{tag}>'
            for cell in cells) + "</tr>")
    return '<table width="100%" cellspacing="0" cellpadding="6">' + "".join(rendered) + "</table><p></p>"


def render_markdown_fragment(text: str) -> str:
    """Render workflow-oriented Markdown without depending on Qt Markdown support."""

    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    paragraph: list[str] = []
    list_kind: Optional[str] = None
    list_items: list[str] = []
    code_lines: list[str] = []
    code_language = ""
    in_code = False

    def flush_paragraph() -> None:
        if not paragraph:
            return
        output.append("<p>" + "<br/>".join(render_inline(line) for line in paragraph) + "</p>")
        paragraph.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if not list_kind:
            return
        output.append(
            f"<{list_kind}>"
            + "".join(f"<li>{render_inline(item)}</li>" for item in list_items)
            + f"</{list_kind}>"
        )
        list_items.clear()
        list_kind = None

    def flush_code() -> None:
        nonlocal code_language
        language_attr = (
            f' data-language="{html.escape(code_language, quote=True)}"' if code_language else ""
        )
        output.append(
            f"<pre{language_attr}><code>{html.escape(chr(10).join(code_lines), quote=False)}</code></pre>"
        )
        code_lines.clear()
        code_language = ""

    position = 0
    while position < len(lines):
        line = lines[position]
        position += 1
        fence = _FENCE_RE.match(line)
        if fence:
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                code_language = fence.group(1)
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if "|" in line and position < len(lines) and _table_separator(lines[position]):
            flush_paragraph()
            flush_list()
            rows = [_table_cells(line)]
            position += 1
            while position < len(lines) and "|" in lines[position] and lines[position].strip():
                rows.append(_table_cells(lines[position]))
                position += 1
            output.append(_render_table(rows))
            continue

        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            continue

        if _RULE_RE.match(line):
            flush_paragraph()
            flush_list()
            output.append("<hr/>")
            continue

        unordered = _UNORDERED_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if unordered or ordered:
            flush_paragraph()
            next_kind = "ul" if unordered else "ol"
            if list_kind and list_kind != next_kind:
                flush_list()
            list_kind = next_kind
            list_items.append((unordered or ordered).group(1))
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            output.append(f"<blockquote>{render_inline(stripped[1:].lstrip())}</blockquote>")
            continue

        flush_list()
        paragraph.append(line.strip())

    if in_code:
        flush_code()
    flush_paragraph()
    flush_list()
    return "".join(output)
