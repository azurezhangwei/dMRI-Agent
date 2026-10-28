from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from backend.protocol_registry import iter_protocol_entries
from backend.protocol_manager import PROTOCOL_ID_RE


TurnMode = Literal["execute", "plan"]
MAX_SELECTED_PROTOCOLS = 8


def normalize_turn_mode(value: object) -> TurnMode:
    return "plan" if str(value).strip().lower() == "plan" else "execute"


def normalize_protocol_ids(values: Iterable[object] | None) -> list[str]:
    if values is None:
        return []
    requested = [str(value).strip() for value in values if str(value).strip()]
    if len(requested) > MAX_SELECTED_PROTOCOLS:
        raise ValueError(f"at most {MAX_SELECTED_PROTOCOLS} Protocols may be selected")

    available = {entry.protocol_id.lower(): entry.protocol_id for entry in iter_protocol_entries()}
    normalized: list[str] = []
    for value in requested:
        if not PROTOCOL_ID_RE.fullmatch(value):
            raise ValueError(f"invalid Protocol id: {value}")
        canonical = available.get(value.lower())
        if canonical is None:
            raise ValueError(f"unknown Protocol id: {value}")
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def controlled_user_prompt(text: str, mode: TurnMode, protocol_ids: list[str]) -> str:
    controls = [f"Turn mode: {mode}."]
    if protocol_ids:
        controls.append(
            "User-selected Protocols: " + ", ".join(protocol_ids) + ". Load and follow each selected Protocol."
        )
    else:
        controls.append("User-selected Protocols: automatic selection.")
    if mode == "plan":
        controls.append(
            "This is a planning-only turn. You may inspect with the available read-only tools, "
            "but you must not execute processing, install or remove software, write files, change "
            "memory or Protocols, request approval, or delegate to an Executor. Return a concrete plan "
            "with inputs, outputs, stages, QC, failure boundaries, and approval points. Load each "
            "selected Protocol at most once and do not inspect unrelated Protocols. If the user did not "
            "provide a concrete data path, list it as a required input; do not scan the workspace, "
            "parent directories, or guessed locations. Stop inspecting as soon as the plan can be written."
        )
    return text.rstrip() + "\n\n[Agent request controls]\n" + "\n".join(controls)
