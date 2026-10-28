from __future__ import annotations

import re


SLICER_TASK_PATTERN = re.compile(
    r"\bslicer\b|slicerdmri|run_slicer_module|mcp_slicer_|"
    r"diffusionweightedvolumemasking|dwitodtiestimation|"
    r"diffusiontensorscalarmeasurements|dwiconvert|"
    r"ukftractography|fibertractmeasurements",
    re.IGNORECASE,
)


def is_slicer_instruction(instruction: str) -> bool:
    """Return true only when the instruction explicitly targets Slicer tooling."""
    return bool(SLICER_TASK_PATTERN.search(str(instruction or "")))
