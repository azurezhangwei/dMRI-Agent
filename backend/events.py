from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Event:
    type: str
    value: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": self.type}
        if self.value is not None:
            payload["value"] = self.value
        return payload


def content_event(text: str) -> Event:
    return Event(type="content", value=text)


def turn_started_event(turn_id: str) -> Event:
    return Event(type="turn_started", value={"turn_id": turn_id})


def thought_event(description: str, subject: str = "") -> Event:
    return Event(type="thought", value={"subject": subject, "description": description})


def tool_call_request_event(call_id: str, name: str, args: Dict[str, Any]) -> Event:
    return Event(
        type="tool_call_request",
        value={"callId": call_id, "name": name, "args": args},
    )


def tool_call_response_event(call_id: str, name: str, output: str) -> Event:
    return Event(
        type="tool_call_response",
        value={
            "callId": call_id,
            "name": name,
            "output": output,
        },
    )


def tool_call_start_event(call_id: str, name: str, args: Dict[str, Any]) -> Event:
    return Event(type="tool_call_start", value={"callId": call_id, "name": name, "args": args})


def stage_start_event(
    stage: str, call_id: Optional[str] = None, tool: Optional[str] = None
) -> Event:
    payload: Dict[str, Any] = {"stage": stage}
    if call_id:
        payload["callId"] = call_id
    if tool:
        payload["tool"] = tool
    return Event(type="stage_start", value=payload)


def stage_done_event(
    stage: str,
    status: str,
    call_id: Optional[str] = None,
    tool: Optional[str] = None,
    elapsed: Optional[float] = None,
) -> Event:
    payload: Dict[str, Any] = {"stage": stage, "status": status}
    if call_id:
        payload["callId"] = call_id
    if tool:
        payload["tool"] = tool
    if elapsed is not None:
        payload["elapsed"] = elapsed
    return Event(type="stage_done", value=payload)


def plan_ready_event(
    plan_id: str,
    steps: list[str],
    current: Optional[str] = None,
    raw: Optional[str] = None,
) -> Event:
    payload: Dict[str, Any] = {"planId": plan_id, "steps": steps}
    if current:
        payload["current"] = current
    if raw:
        payload["raw"] = raw
    return Event(type="plan_ready", value=payload)


def plan_cleared_event(plan_id: str, reason: str) -> Event:
    return Event(
        type="plan_cleared",
        value={"planId": plan_id, "reason": reason},
    )


def usage_event(usage: Dict[str, Any]) -> Event:
    return Event(type="usage", value=usage)


def finished_event(reason: str, usage: Optional[Dict[str, Any]] = None) -> Event:
    return Event(
        type="finished",
        value={"reason": reason, "usageMetadata": usage or {}},
    )


def info_event(message: str) -> Event:
    return Event(type="info", value=message)


def error_event(message: str, status: Optional[int] = None) -> Event:
    return Event(
        type="error",
        value={"error": {"message": message, "status": status}},
    )


def shutdown_complete_event() -> Event:
    return Event(type="shutdown_complete")
