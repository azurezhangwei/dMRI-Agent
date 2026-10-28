"""Qt rich-text conversation layout; display grouping never changes stored events."""
from __future__ import annotations

import html
import textwrap
from collections.abc import Callable, Sequence
from typing import Any

from .markdown_render import render_inline

ACTIVITY_ROLES = {"TOOL", "THINKING"}


def _link(target: str, label: str) -> str:
    return f'<a href="{target}" style="color:#94A3B8;text-decoration:none;">{label}</a>'


def _activity_group(entries: Sequence[dict[str, Any]], start: int, expanded: set, working: bool, reply_index: int | None = None) -> str:
    tools = [(start + offset, entry) for offset, entry in enumerate(entries) if entry.get("role") == "TOOL"]
    failures = sum(str(entry.get("summary", "")).startswith(("Failed", "Rejected", "Cancelled")) for _, entry in tools)
    running = next((entry.get("summary", "") for _, entry in reversed(tools)
                    if str(entry.get("summary", "")).startswith(("Running", "Awaiting approval"))), "")
    key = f"activity:{start}"
    opened = key in expanded
    title = "Processing" if working and (running or not tools) else "Processing history"
    count = f" · {len(tools)} operations" if tools else ""
    warning = f' <span style="color:#FBBF24;">· {failures} failed or stopped</span>' if failures else ""
    body = (f'<p style="margin:0;color:#94A3B8;font-size:12px;">'
            f'{_link(key, ("▾ " if opened else "▸ ") + title + count)}{warning}</p>')
    if reply_index is not None:
        body += '<p style="margin:4px 0 0;font-size:12px;">' + _link(f"reply:{reply_index}", "Read reply ↓") + '</p>'
    if running:
        body += f'<p style="margin:5px 0 0;color:#CBD5E1;font-size:12px;">{html.escape(running)}</p>'
    if opened:
        for offset, entry in enumerate(entries):
            index = start + offset
            summary = str(entry.get("summary") or entry.get("text") or "").strip()
            if entry.get("role") == "TOOL":
                state, separator, action = summary.partition(" · ")
                color = "#FBBF24" if state in {"Failed", "Rejected", "Cancelled"} else "#86EFAC" if state == "Completed" else "#93C5FD"
                details = index in expanded
                body += (f'<p style="margin:9px 0 3px;font-size:12px;color:#CBD5E1;">'
                         f'<span style="color:{color};">{html.escape(state)}</span>'
                         f'{" · " + html.escape(action) if separator else ""}<br/>'
                         f'{_link(f"entry:{index}", "Hide log" if details else "View log")}</p>')
                if details:
                    # Plain text preserves JSON, paths and code without interpreting tool output as Markdown.
                    detail = html.escape(str(entry.get("text") or "")).replace("\n", "<br/>")
                    body += f'<p style="margin:4px 0 12px;color:#94A3B8;font-size:12px;font-family:monospace;">{detail}</p>'
            else:
                body += f'<p style="margin:7px 0;color:#94A3B8;font-size:12px;">{html.escape(summary)}</p>'
    return '<div style="margin:6px 0 20px;">' + body + '</div>'


def render_conversation(
    entries: Sequence[dict[str, Any]],
    expanded: set,
    markdown: Callable[[str, str], str],
    *,
    working: bool = False,
) -> str:
    """Render full replies, compact user requests and expandable operation groups."""
    blocks = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        role = entry.get("role", "SYSTEM")
        if role in ACTIVITY_ROLES:
            end = index + 1
            while end < len(entries) and entries[end].get("role") in ACTIVITY_ROLES:
                end += 1
            reply_index = end if end < len(entries) and entries[end].get("role") in {"AGENT", "PLAN"} else None
            blocks.append(_activity_group(entries[index:end], index, expanded, working and end == len(entries), reply_index))
            index = end
            continue
        text = textwrap.dedent(str(entry.get("text") or "")).strip("\n")
        if role == "ME":
            compact = len(text) > 280 or len(text.splitlines()) > 6
            shown = text if not compact or index in expanded else text[:160].rstrip() + "…"
            body = render_inline(shown).replace("\n", "<br/>")
            if compact:
                body += '<br/>' + _link(f"entry:{index}", "Show less" if index in expanded else "Read full request")
            blocks.append('<div style="margin:2px 0 18px;">'
                          '<p style="margin:0 0 6px;color:#94A3B8;font-size:12px;">You</p>'
                          f'<p style="margin:0;color:#E2E8F0;font-size:13px;line-height:145%;">{body}</p></div>')
        else:
            label, color = {"AGENT": ("dMRI-Agent", "#E2E8F0"), "PLAN": ("Plan", "#93C5FD"),
                            "ERROR": ("Attention", "#FCA5A5")}.get(role, ("Notice", "#CBD5E1"))
            blocks.append(f'<a name="message-{index}"></a><div style="margin:10px 0 22px;">'
                          f'<p style="margin:0 0 9px;color:{color};font-size:12px;font-weight:600;">{label}</p>'
                          + markdown(text, color) + '</div>')
        index += 1
    return "".join(blocks) or ('<p style="margin:18px 6px 8px;color:#E2E8F0;font-size:15px;">'
                               'What would you like to do with your data?</p>'
                               '<p style="margin:0 6px;color:#94A3B8;font-size:13px;">'
                               'Describe a task, or choose a file from Files.</p>')
