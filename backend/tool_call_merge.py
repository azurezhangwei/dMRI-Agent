from __future__ import annotations

from typing import Any, Dict

from langchain_core.messages import BaseMessage


def merge_tool_calls(acc: Dict[str, Dict[str, Any]], chunk: BaseMessage) -> None:
    tool_call_chunks = getattr(chunk, "tool_call_chunks", None)
    if tool_call_chunks:
        for item in tool_call_chunks:
            index = item.get("index")
            call_id = item.get("id")
            entry = None
            if call_id:
                entry = acc.setdefault(
                    call_id,
                    {
                        "id": call_id,
                        "name": None,
                        "args_str": "",
                        "args": None,
                        "index": index,
                    },
                )
            else:
                for candidate in acc.values():
                    if candidate.get("index") == index:
                        entry = candidate
                        break
                if entry is None:
                    synthetic_id = f"index:{index}"
                    entry = acc.setdefault(
                        synthetic_id,
                        {
                            "id": synthetic_id,
                            "name": None,
                            "args_str": "",
                            "args": None,
                            "index": index,
                        },
                    )
            if item.get("name"):
                entry["name"] = item["name"]
            if item.get("args"):
                entry["args_str"] += item["args"]
        return

    tool_calls = getattr(chunk, "tool_calls", None)
    if not tool_calls:
        extra = getattr(chunk, "additional_kwargs", {}) or {}
        tool_calls = extra.get("tool_calls")
    if not tool_calls:
        return

    for item in tool_calls:
        index = item.get("index")
        call_id = item.get("id") or item.get("tool_call_id")
        entry = None
        if call_id:
            entry = acc.setdefault(
                call_id,
                {
                    "id": call_id,
                    "name": None,
                    "args_str": "",
                    "args": None,
                    "index": index,
                },
            )
        else:
            for candidate in acc.values():
                if candidate.get("index") == index:
                    entry = candidate
                    break
            if entry is None:
                synthetic_id = f"index:{index}"
                entry = acc.setdefault(
                    synthetic_id,
                    {
                        "id": synthetic_id,
                        "name": None,
                        "args_str": "",
                        "args": None,
                        "index": index,
                    },
                )
        if "name" in item and item["name"]:
            entry["name"] = item["name"]
        if "function" in item and item["function"]:
            entry["name"] = item["function"].get("name") or entry["name"]
            args_fragment = item["function"].get("arguments")
            if isinstance(args_fragment, str):
                entry["args_str"] += args_fragment
        if "args" in item and item["args"] is not None:
            if isinstance(item["args"], dict):
                entry["args"] = item["args"]
            elif isinstance(item["args"], str):
                entry["args_str"] += item["args"]
