from __future__ import annotations

import datetime as dt
import os
import platform as system_platform
from backend.platform import IS_WINDOWS
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from backend.model_config import DEFAULT_CONTEXT_WINDOW_TOKENS
from backend.protocol_registry import protocol_registry_json


CONTINUITY_TESTAMENT_ROOT = "continuity_testament"
CONTINUITY_TESTAMENT_VERSION = "1"
CONTINUITY_TESTAMENT_REQUIRED_SECTIONS = (
    "overall_goal",
    "binding_directives",
    "data_scope",
    "operation_ledger",
    "decisions_and_deviations",
    "failures_and_recovery",
    "artifacts_and_files",
    "current_state",
    "next_actions",
)
CONTINUITY_OPERATION_REQUIRED_FIELDS = (
    "stage",
    "data",
    "action",
    "parameters",
    "status",
    "outputs",
    "qc_and_evidence",
)


COMPRESSION_PROMPT = """
You create the continuity testament used when dMRI-Agent compresses its context.
The testament becomes the agent's authoritative historical memory. Return ONLY
the XML document described below. Do not expose scratchpad or private reasoning.

Retention policy (mandatory):
- Preserve the user's goal, binding constraints, requested protocol/algorithm,
  approvals, and any explicit output locations.
- Preserve the identity and scope of data handled by EVERY completed or active
  stage: dataset, center, subject, session, modality, and exact relevant paths.
- Preserve a chronological operation ledger. For each stage record what data it
  targeted, what macro operation was performed, key reproducibility parameters,
  status, actual outputs, QC/evidence, and any unresolved issue.
- Preserve decisions, deviations from protocol, failures, recovery attempts,
  software/environment facts that affect reproducibility, current state, and the
  exact next action.
- Never turn an unverified claim into a fact. Use "unknown" or "not recorded"
  when evidence is absent.

Discard policy (mandatory):
- Omit raw shell output, repeated polling text, duplicate stack traces, verbose
  file listings, token accounting chatter, internal reasoning, UI chatter, and
  low-level command mechanics after their outcome is represented in the ledger.
- Do not retain secrets or credentials.

The XML must be well formed and use this exact top-level structure:

<continuity_testament version="1">
  <overall_goal>High-level objective and success condition.</overall_goal>
  <binding_directives>User constraints, protocol/algorithm, approvals.</binding_directives>
  <data_scope>Authoritative datasets, centers, subjects, sessions, inputs and roots.</data_scope>
  <operation_ledger>
    <operation>
      <stage>Stage name.</stage>
      <data>Exact data scope and inputs targeted.</data>
      <action>Macro operation performed.</action>
      <parameters>Only key reproducibility parameters.</parameters>
      <status>completed|partial|failed|pending.</status>
      <outputs>Actual output paths, or none.</outputs>
      <qc_and_evidence>QC result and evidence paths, or not recorded.</qc_and_evidence>
    </operation>
  </operation_ledger>
  <decisions_and_deviations>Important choices and protocol deviations.</decisions_and_deviations>
  <failures_and_recovery>Failures, affected data, recovery and remaining impact.</failures_and_recovery>
  <artifacts_and_files>Created or modified artifacts needed later.</artifacts_and_files>
  <current_state>Completed, active and blocked work.</current_state>
  <next_actions>Ordered next actions and unresolved user decisions.</next_actions>
</continuity_testament>

Escape XML special characters. Every required top-level section must be present,
even when its value is "none" or "not recorded".
""".strip()


def token_limit_for_model(model: str) -> int:
    del model  # The selected profile exports the authoritative per-model limit.
    raw = (os.environ.get("LLM_CONTEXT_WINDOW_TOKENS") or "").strip()
    if not raw:
        return DEFAULT_CONTEXT_WINDOW_TOKENS
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("LLM_CONTEXT_WINDOW_TOKENS must be an integer") from exc
    if value <= 0:
        raise RuntimeError("LLM_CONTEXT_WINDOW_TOKENS must be positive")
    return value


def estimate_tokens(messages: Iterable[BaseMessage], llm: Optional[ChatOpenAI]) -> int:
    if llm and hasattr(llm, "get_num_tokens_from_messages"):
        try:
            return int(llm.get_num_tokens_from_messages(list(messages)))
        except Exception:  # noqa: BLE001
            pass
    text = "".join(f"{msg.type}:{getattr(msg, 'content', '')}\n" for msg in messages)
    return max(1, len(text) // 4)


def is_context_window_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    markers = (
        "maximum context length",
        "context length is",
        "context window",
        "context_length_exceeded",
        "requested tokens in the messages",
        "please reduce the length of the messages",
    )
    return any(marker in text for marker in markers)


def _estimate_tokens_for_text(text: str) -> int:
    return max(1, len(text) // 4)


def _truncate_text_by_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}\n...[truncated]"


def _collect_recent_user_messages(
    history: List[BaseMessage], max_tokens: int
) -> List[str]:
    selected: List[str] = []
    remaining = max_tokens
    for msg in reversed(history):
        if not isinstance(msg, HumanMessage):
            continue
        text = str(getattr(msg, "content", "") or "").strip()
        if not text:
            continue
        if "<state_snapshot>" in text or "<continuity_testament" in text:
            continue
        tokens = _estimate_tokens_for_text(text)
        if tokens <= remaining:
            selected.append(text)
            remaining -= tokens
        else:
            selected.append(_truncate_text_by_tokens(text, remaining))
            remaining = 0
            break
    selected.reverse()
    return selected


def _is_env_context_message(text: str) -> bool:
    return "Today's date is" in text and "I'm currently working in the directory:" in text


def _extract_initial_context(history: List[BaseMessage]) -> tuple[List[BaseMessage], List[BaseMessage]]:
    if not history:
        return [], history
    first = history[0]
    if isinstance(first, HumanMessage):
        text = str(getattr(first, "content", "") or "")
        if _is_env_context_message(text):
            remaining = history[1:]
            if (
                remaining
                and isinstance(remaining[0], AIMessage)
                and "Got it. Thanks for the context!" in str(remaining[0].content or "")
            ):
                remaining = remaining[1:]
            return [first], remaining
    return [], history


def _require_int_env(name: str) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required env var: {name}")
    return int(value)


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _extract_continuity_testament(
    text: str,
    *,
    require_operation: bool = False,
) -> tuple[str, tuple[str, ...], str]:
    raw = str(text or "").strip()
    start = raw.find(f"<{CONTINUITY_TESTAMENT_ROOT}")
    closing = f"</{CONTINUITY_TESTAMENT_ROOT}>"
    end = raw.rfind(closing)
    if start < 0 or end < start:
        return "", (), "missing continuity_testament root"
    candidate = raw[start : end + len(closing)].strip()
    try:
        root = ET.fromstring(candidate)
    except ET.ParseError as exc:
        return "", (), f"invalid continuity_testament XML: {exc}"
    if root.tag != CONTINUITY_TESTAMENT_ROOT:
        return "", (), f"unexpected testament root: {root.tag}"
    if root.attrib.get("version") != CONTINUITY_TESTAMENT_VERSION:
        return "", (), "continuity_testament version must be 1"
    present = tuple(child.tag for child in root)
    missing = [
        section
        for section in CONTINUITY_TESTAMENT_REQUIRED_SECTIONS
        if root.find(section) is None
    ]
    if missing:
        return "", present, "missing testament sections: " + ", ".join(missing)
    operations = root.findall("./operation_ledger/operation")
    if require_operation and not operations:
        return "", present, "operation_ledger must contain an operation"
    for index, operation in enumerate(operations, start=1):
        missing_fields = [
            field
            for field in CONTINUITY_OPERATION_REQUIRED_FIELDS
            if operation.find(field) is None
        ]
        if missing_fields:
            return (
                "",
                present,
                f"operation {index} missing fields: " + ", ".join(missing_fields),
            )
    return candidate, present, ""


def _history_has_tool_calls(history: List[BaseMessage]) -> bool:
    return any(bool(getattr(message, "tool_calls", None)) for message in history)


@dataclass
class CompressionResult:
    new_history: List[BaseMessage] | None
    original_tokens: int
    new_tokens: int
    status: str
    summary: str
    split_point: int
    original_len: int
    new_len: int
    trigger_tokens: int
    headroom_tokens: int
    effective_trigger_tokens: int
    reason: str
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    testament_valid: bool = False
    testament_sections: tuple[str, ...] = ()
    summary_attempts: int = 0
    validation_error: str = ""


@dataclass
class CompressionDecision:
    original_tokens: int
    trigger_tokens: int
    headroom_tokens: int
    effective_trigger_tokens: int
    should_compress: bool
    reason: str
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS


def compression_decision(
    history: List[BaseMessage],
    llm: ChatOpenAI,
    force: bool,
) -> CompressionDecision:
    original_tokens = estimate_tokens(history, llm)
    configured_trigger_tokens = _require_int_env("CONTEXT_COMPRESSION_TRIGGER_TOKENS")
    if configured_trigger_tokens <= 0:
        raise RuntimeError("CONTEXT_COMPRESSION_TRIGGER_TOKENS must be positive")
    model_name = str(getattr(llm, "model_name", "") or "")
    context_window_tokens = token_limit_for_model(model_name)
    trigger_tokens = min(configured_trigger_tokens, context_window_tokens)
    headroom_tokens = max(0, _int_env("CONTEXT_COMPRESSION_HEADROOM_TOKENS", 20_000))
    effective_trigger_tokens = max(1, trigger_tokens - headroom_tokens)
    if force:
        return CompressionDecision(
            original_tokens=original_tokens,
            trigger_tokens=trigger_tokens,
            headroom_tokens=headroom_tokens,
            effective_trigger_tokens=effective_trigger_tokens,
            should_compress=True,
            reason="force",
            context_window_tokens=context_window_tokens,
        )
    if original_tokens >= effective_trigger_tokens:
        return CompressionDecision(
            original_tokens=original_tokens,
            trigger_tokens=trigger_tokens,
            headroom_tokens=headroom_tokens,
            effective_trigger_tokens=effective_trigger_tokens,
            should_compress=True,
            reason="trigger_reached_with_headroom",
            context_window_tokens=context_window_tokens,
        )
    return CompressionDecision(
        original_tokens=original_tokens,
        trigger_tokens=trigger_tokens,
        headroom_tokens=headroom_tokens,
        effective_trigger_tokens=effective_trigger_tokens,
        should_compress=False,
        reason="below_trigger",
        context_window_tokens=context_window_tokens,
    )


def compress_history(
    llm: ChatOpenAI,
    history: List[BaseMessage],
    model: str,
    force: bool,
) -> CompressionResult:
    if not history:
        return CompressionResult(
            new_history=None,
            original_tokens=0,
            new_tokens=0,
            status="noop",
            summary="",
            split_point=0,
            original_len=0,
            new_len=0,
            trigger_tokens=0,
            headroom_tokens=0,
            effective_trigger_tokens=0,
            reason="empty_history",
            context_window_tokens=token_limit_for_model(model),
        )

    decision = compression_decision(history, llm, force)
    original_tokens = decision.original_tokens
    trigger_tokens = decision.trigger_tokens
    headroom_tokens = decision.headroom_tokens
    effective_trigger_tokens = decision.effective_trigger_tokens
    context_window_tokens = decision.context_window_tokens
    recent_user_tokens = _require_int_env("CONTEXT_COMPRESS_RECENT_USER_TOKENS")
    if not decision.should_compress:
        return CompressionResult(
            new_history=None,
            original_tokens=original_tokens,
            new_tokens=original_tokens,
            status="noop",
            summary="",
            split_point=0,
            original_len=len(history),
            new_len=len(history),
            trigger_tokens=trigger_tokens,
            headroom_tokens=headroom_tokens,
            effective_trigger_tokens=effective_trigger_tokens,
            reason=decision.reason,
            context_window_tokens=context_window_tokens,
        )

    retry_limit = max(0, _int_env("CONTEXT_COMPRESSION_TESTAMENT_RETRIES", 1))
    summary_text = ""
    raw_summary = ""
    testament_sections: tuple[str, ...] = ()
    validation_error = ""
    summary_attempts = 0
    require_operation = _history_has_tool_calls(history)
    for attempt in range(retry_limit + 1):
        summary_attempts = attempt + 1
        request = (
            "Generate the continuity testament now. Return only valid XML."
            if attempt == 0
            else "The prior testament did not satisfy the contract. Re-read the full "
            "history and return only a valid, complete continuity_testament XML document."
        )
        summary_messages = [
            SystemMessage(content=COMPRESSION_PROMPT),
            *history,
            HumanMessage(content=request),
        ]
        summary_response = llm.invoke(summary_messages)
        raw_summary = str(summary_response.content or "").strip()
        summary_text, testament_sections, validation_error = (
            _extract_continuity_testament(
                raw_summary,
                require_operation=require_operation,
            )
        )
        if summary_text:
            break

    if not summary_text:
        status = "failed_empty" if not raw_summary else "failed_invalid_testament"
        return CompressionResult(
            new_history=None,
            original_tokens=original_tokens,
            new_tokens=original_tokens,
            status=status,
            summary=_truncate_text_by_tokens(raw_summary, 4_000),
            split_point=0,
            original_len=len(history),
            new_len=len(history),
            trigger_tokens=trigger_tokens,
            headroom_tokens=headroom_tokens,
            effective_trigger_tokens=effective_trigger_tokens,
            reason=status,
            context_window_tokens=context_window_tokens,
            testament_valid=False,
            testament_sections=testament_sections,
            summary_attempts=summary_attempts,
            validation_error=validation_error or "empty testament response",
        )

    initial_context, history_for_recent = _extract_initial_context(history)
    recent_user_messages = _collect_recent_user_messages(history_for_recent, recent_user_tokens)
    new_history = [*initial_context, *[HumanMessage(content=msg) for msg in recent_user_messages]]
    new_history.append(
        HumanMessage(
            content=(
                "Authoritative continuity testament from context compression. "
                "Use it as the historical record; do not repeat completed operations "
                "unless the current state requires recovery.\n\n"
                + summary_text
            )
        )
    )
    new_tokens = estimate_tokens(new_history, llm)
    if new_tokens > original_tokens:
        return CompressionResult(
            new_history=None,
            original_tokens=original_tokens,
            new_tokens=new_tokens,
            status="failed_inflated",
            summary=summary_text,
            split_point=0,
            original_len=len(history),
            new_len=len(new_history),
            trigger_tokens=trigger_tokens,
            headroom_tokens=headroom_tokens,
            effective_trigger_tokens=effective_trigger_tokens,
            reason="failed_inflated",
            context_window_tokens=context_window_tokens,
            testament_valid=True,
            testament_sections=testament_sections,
            summary_attempts=summary_attempts,
        )
    return CompressionResult(
        new_history=new_history,
        original_tokens=original_tokens,
        new_tokens=new_tokens,
        status="compressed",
        summary=summary_text,
        split_point=0,
        original_len=len(history),
        new_len=len(new_history),
        trigger_tokens=trigger_tokens,
        headroom_tokens=headroom_tokens,
        effective_trigger_tokens=effective_trigger_tokens,
        reason=decision.reason,
        context_window_tokens=context_window_tokens,
        testament_valid=True,
        testament_sections=testament_sections,
        summary_attempts=summary_attempts,
    )


def build_env_context(cwd: str) -> str:
    today = dt.datetime.now().strftime("%A, %B %d, %Y")
    platform = system_platform.system()
    protocol_registry = protocol_registry_json(cwd)
    lines = [
        f"Today's date is {today} (formatted according to the user's locale).",
        f"My operating system is: {platform}",
        "My command shell is: " + ("Windows PowerShell 5.1 (no PTY; Bash templates require separate prerequisites)." if IS_WINDOWS else "Bash."),
        f"I'm currently working in the directory: {Path(cwd).resolve()}",
    ]
    if protocol_registry:
        lines.append("Protocol registry (level 1 metadata):")
        lines.append(protocol_registry)
        lines.append(
            "Supplement protocols may describe runtime software and environment setup; "
            "load them when command setup matters."
        )
    return "\n".join(lines).strip()


def build_initial_history(system_prompt: str, env_context: str) -> List[BaseMessage]:
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=env_context),
        AIMessage(content="Got it. Thanks for the context!"),
    ]
