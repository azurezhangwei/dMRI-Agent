from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.messages import BaseMessage, ToolMessage

from backend.tools import ToolSpec


ExecuteResult = Tuple[str, Optional[float]]


def handle_tool_calls(
    tool_calls: List[Dict[str, Any]],
    tool_specs: Dict[str, ToolSpec],
    history: List[BaseMessage],
    resolve_args: Callable[[Dict[str, Any]], Dict[str, Any]],
    execute_tool: Callable[[ToolSpec, Dict[str, Any]], ExecuteResult],
    on_request: Optional[Callable[[str, str, Dict[str, Any], ToolSpec], None]] = None,
    on_start: Optional[Callable[[str, str, Dict[str, Any], ToolSpec], None]] = None,
    on_response: Optional[
        Callable[[str, str, str, Optional[float]], None]
    ] = None,
    approve: Optional[Callable[[str, str, Dict[str, Any], ToolSpec], bool]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    sanitize_output: Optional[Callable[[str], str]] = None,
) -> bool:
    def clean(output: str) -> str:
        return sanitize_output(str(output)) if sanitize_output is not None else str(output)

    for call in tool_calls:
        call_id = str(call["id"])
        name = str(call["name"])
        args = call.get("args") or resolve_args(call)
        spec = tool_specs.get(name)
        if spec is None:
            output = clean(f"[error] unknown tool: {name}")
            if on_response:
                on_response(call_id, name, output, None)
            history.append(ToolMessage(tool_call_id=call_id, content=output))
            if should_stop is not None and should_stop():
                return True
            continue
        if on_request:
            on_request(call_id, name, args, spec)
        if approve is not None and not approve(call_id, name, args, spec):
            output = clean("[rejected] user denied tool execution")
            if on_response:
                on_response(call_id, name, output, None)
            history.append(ToolMessage(tool_call_id=call_id, content=output))
            if should_stop is not None and should_stop():
                return True
            continue
        if on_start:
            on_start(call_id, name, args, spec)
        output, elapsed = execute_tool(spec, args)
        output = clean(str(output))
        if on_response:
            on_response(call_id, name, str(output), elapsed)
        history.append(ToolMessage(tool_call_id=call_id, content=str(output)))
        if should_stop is not None and should_stop():
            return True
    return False
