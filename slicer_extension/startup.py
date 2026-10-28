"""Runtime bootstrap loaded by the dmri-agent-slicer launcher."""

from __future__ import annotations

import os
import runpy
import traceback

import qt
import slicer


def _log(message: str) -> None:
    print(f"[dMRI-Agent Slicer] {message}", flush=True)


def _start() -> None:
    try:
        slicer.util.selectModule("DMRIAgentSlicer")
        _log("DMRIAgentSlicer module selected")

        test_script = os.environ.get("DMRI_SLICER_TEST_SCRIPT", "").strip()
        if test_script:
            qt.QTimer.singleShot(1200, lambda: runpy.run_path(test_script, run_name="__main__"))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        test_script = os.environ.get("DMRI_SLICER_TEST_SCRIPT", "").strip()
        if test_script:
            slicer.util.exit(1)


qt.QTimer.singleShot(0, _start)
