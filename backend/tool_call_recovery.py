from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List, Tuple

_INVOKE_RE = re.compile(
    r'<[^>]*invoke\s+name="(?P<name>[^"]+)"[^>]*>(?P<body>.*?)(?=(?:<[^>]*invoke\s+name=")|\Z)',
    re.IGNORECASE | re.DOTALL,
)
_PARAM_RE = re.compile(
    r'<[^>]*parameter\s+name="(?P<name>[^"]+)"(?P<attrs>[^>]*)>(?P<value>.*?)</[^>]*parameter>',
    re.IGNORECASE | re.DOTALL,
)


def parse_tool_args_blob(args_blob: str) -> Dict[str, Any]:
    parsed = _parse_json_or_literal(args_blob)
    return parsed if isinstance(parsed, dict) else {}


def recover_tool_calls_from_text(text: str) -> List[Dict[str, Any]]:
    content = str(text or "")
    if "invoke name=" not in content:
        return []

    recovered: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for match in _INVOKE_RE.finditer(content):
        tool_name = str(match.group("name") or "").strip()
        if not tool_name:
            continue
        body = str(match.group("body") or "")
        args: Dict[str, Any] = {}
        for param_match in _PARAM_RE.finditer(body):
            param_name = str(param_match.group("name") or "").strip()
            if not param_name:
                continue
            attrs = str(param_match.group("attrs") or "").lower()
            raw_value = str(param_match.group("value") or "").strip()
            if not raw_value:
                continue
            if 'string="true"' in attrs:
                args[param_name] = raw_value
                continue
            parsed_value = _parse_json_or_literal(raw_value)
            args[param_name] = raw_value if parsed_value is None else parsed_value
        if not args:
            continue
        dedupe_key = f"{tool_name}:{_json_dumps_safe(args)}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        recovered.append({"name": tool_name, "args": args})
    return recovered


def resolve_calls_with_recovery(
    raw_calls: List[Dict[str, Any]],
    full_text: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    recovered = recover_tool_calls_from_text(full_text)
    recovered_by_name: Dict[str, List[Dict[str, Any]]] = {}
    for item in recovered:
        name = str(item.get("name") or "").strip()
        args = item.get("args")
        if not name or not isinstance(args, dict):
            continue
        recovered_by_name.setdefault(name, []).append(args)

    resolved: List[Dict[str, Any]] = []
    recovered_used = 0
    for idx, call in enumerate(raw_calls):
        call_id = str(call.get("id") or f"index:{idx}")
        name = str(call.get("name") or "").strip()
        if not name:
            continue

        args: Dict[str, Any] = {}
        if isinstance(call.get("args"), dict):
            args = call["args"]
        else:
            args_str = call.get("args_str")
            if isinstance(args_str, str) and args_str.strip():
                args = parse_tool_args_blob(args_str)

        if not args:
            bucket = recovered_by_name.get(name, [])
            if bucket:
                args = bucket.pop(0)
                recovered_used += 1

        resolved.append({"id": call_id, "name": name, "args": args})

    # If the model failed to emit structured tool_calls but DSML content exists,
    # still attempt one recovered call path instead of dropping the turn.
    if not resolved and recovered:
        for idx, item in enumerate(recovered):
            name = str(item.get("name") or "").strip()
            args = item.get("args")
            if not name or not isinstance(args, dict):
                continue
            resolved.append({"id": f"recovered:{idx}", "name": name, "args": args})
            recovered_used += 1

    deduped: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    dedup_removed = 0
    for call in resolved:
        dedupe_key = f"{call['name']}:{_json_dumps_safe(call['args'])}"
        if dedupe_key in seen_keys:
            dedup_removed += 1
            continue
        seen_keys.add(dedupe_key)
        deduped.append(call)

    stats = {
        "recovered_candidates": len(recovered),
        "recovered_used": recovered_used,
        "dedup_removed": dedup_removed,
        "raw_calls": len(raw_calls),
        "resolved_calls": len(deduped),
    }
    return deduped, stats


def _parse_json_or_literal(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        return ast.literal_eval(raw)
    except Exception:
        return None


def _json_dumps_safe(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        return str(value)
