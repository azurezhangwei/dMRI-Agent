"""User-level startup bootstrap for the installed dMRI-Agent Slicer module.

The installer may invoke this file from ``~/.slicerrc.py`` using ``runpy``.
Slicer executes that user startup file after its ``startupCompleted`` signal,
so this bootstrap queues registration for the next Qt event-loop turn.  It
deliberately does one thing only: register and load the managed scripted module
located next to this file in the installed application tree.

This bootstrap does not select the module, start a service, inspect
credentials, or write Slicer settings.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, NamedTuple


LOGGER = logging.getLogger("dmri_agent.slicer.user_startup_bootstrap")
MODULE_NAME = "DMRIAgentSlicer"
MINIMUM_SLICER_VERSION = (5, 6)
_STATE_ATTRIBUTE = "_dmri_agent_user_startup_bootstrap_state"
_VERSION_PATTERN = re.compile(r"^\s*(\d+)\.(\d+)(?:\D|$)")


class InstallLayout(NamedTuple):
    app_dir: Path
    module_file: Path


def _value(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    return value() if callable(value) else value


def _discover_install_layout(bootstrap_file: str | Path) -> InstallLayout:
    """Resolve the managed layout strictly upward from this bootstrap file."""

    bootstrap_path = Path(bootstrap_file).resolve(strict=True)
    if bootstrap_path.name != "user_startup_bootstrap.py":
        raise RuntimeError(f"Unexpected bootstrap filename: {bootstrap_path}")

    extension_dir = bootstrap_path.parent
    if extension_dir.name != "slicer_extension":
        raise RuntimeError(
            "The user startup bootstrap is not inside an installed "
            f"slicer_extension directory: {bootstrap_path}"
        )

    app_dir = extension_dir.parent.resolve(strict=True)
    module_file = (
        extension_dir / MODULE_NAME / f"{MODULE_NAME}.py"
    ).resolve(strict=True)
    try:
        module_file.relative_to(app_dir)
    except ValueError as exc:
        raise RuntimeError(
            f"Managed Slicer module resolves outside APP_DIR: {module_file}"
        ) from exc
    if not module_file.is_file():
        raise RuntimeError(f"Managed Slicer module is not a file: {module_file}")
    return InstallLayout(app_dir=app_dir, module_file=module_file)


def _slicer_version(app: Any) -> tuple[int, int]:
    raw_version = str(_value(app, "applicationVersion", "") or "")
    if not raw_version:
        major = _value(app, "majorVersion", None)
        minor = _value(app, "minorVersion", None)
        if major is not None and minor is not None:
            return int(major), int(minor)
    match = _VERSION_PATTERN.match(raw_version)
    if match is None:
        raise RuntimeError(f"Cannot determine the Slicer version: {raw_version!r}")
    return int(match.group(1)), int(match.group(2))


def _module_path(module: Any) -> Path:
    raw_path = _value(module, "path", "")
    if not raw_path:
        raise RuntimeError(f"Loaded {MODULE_NAME} module does not expose its path")
    return Path(str(raw_path)).resolve(strict=True)


def _register_and_load(slicer_module: Any, qt_module: Any, module_file: Path) -> None:
    module_manager = slicer_module.app.moduleManager()
    if module_manager is None:
        raise RuntimeError("Slicer module manager is unavailable after startup")

    loaded_module = module_manager.module(MODULE_NAME)
    if loaded_module is not None:
        loaded_path = _module_path(loaded_module)
        if loaded_path != module_file:
            raise RuntimeError(
                f"{MODULE_NAME} is already loaded from an unmanaged path: {loaded_path}"
            )
        LOGGER.info("dMRI-Agent Slicer module is already loaded from %s", module_file)
        return

    factory = module_manager.factoryManager()
    if factory is None:
        raise RuntimeError("Slicer module factory manager is unavailable after startup")
    if factory.isRegistered(MODULE_NAME):
        raise RuntimeError(
            f"{MODULE_NAME} was already registered by another source; refusing to load it"
        )

    factory.registerModule(qt_module.QFileInfo(str(module_file)))
    if not factory.isRegistered(MODULE_NAME):
        raise RuntimeError(f"Slicer did not register {module_file}")
    if factory.loadModules([MODULE_NAME]) is False:
        raise RuntimeError(f"Slicer did not load {MODULE_NAME}")

    loaded_module = module_manager.module(MODULE_NAME)
    if loaded_module is None:
        raise RuntimeError(f"Slicer reported success but {MODULE_NAME} is not loaded")
    loaded_path = _module_path(loaded_module)
    if loaded_path != module_file:
        raise RuntimeError(
            f"{MODULE_NAME} loaded from {loaded_path}, expected {module_file}"
        )
    LOGGER.info("Loaded dMRI-Agent Slicer module from %s", module_file)


def _install(slicer_module: Any, qt_module: Any, bootstrap_file: str | Path) -> bool:
    """Queue the bootstrap. Dependencies are explicit for pure unit tests."""

    try:
        app = slicer_module.app
        version = _slicer_version(app)
        if version < MINIMUM_SLICER_VERSION:
            LOGGER.error(
                "dMRI-Agent requires Slicer %d.%d or newer; found %d.%d",
                *MINIMUM_SLICER_VERSION,
                *version,
            )
            return False

        layout = _discover_install_layout(bootstrap_file)
        existing_state = getattr(slicer_module, _STATE_ATTRIBUTE, None)
        if isinstance(existing_state, dict) and existing_state.get("scheduled"):
            LOGGER.info("dMRI-Agent user startup bootstrap is already scheduled")
            return True

        state: dict[str, Any] = {
            "scheduled": False,
            "attempted": False,
            "succeeded": False,
            "app_dir": str(layout.app_dir),
            "module_file": str(layout.module_file),
        }

        def register_on_next_event_loop_turn() -> None:
            if state["attempted"]:
                return
            state["attempted"] = True
            try:
                _register_and_load(slicer_module, qt_module, layout.module_file)
                state["succeeded"] = True
            except Exception:  # noqa: BLE001 - startup must remain usable
                LOGGER.exception(
                    "Could not load the installed dMRI-Agent Slicer module; "
                    "Slicer will continue without it"
                )

        # Retain the callback in module-owned state until it has run.  The
        # .slicerrc.py file is executed after startupCompleted, therefore a
        # connection to that signal would never fire in a normal launch.
        state["callback"] = register_on_next_event_loop_turn
        setattr(slicer_module, _STATE_ATTRIBUTE, state)
        try:
            qt_module.QTimer.singleShot(0, register_on_next_event_loop_turn)
        except Exception:
            delattr(slicer_module, _STATE_ATTRIBUTE)
            raise
        state["scheduled"] = True
        LOGGER.info(
            "Queued dMRI-Agent module loading on the Qt event loop (%s)",
            layout.app_dir,
        )
        return True
    except Exception:  # noqa: BLE001 - never break the user's Slicer startup
        LOGGER.exception(
            "Could not schedule the installed dMRI-Agent Slicer module; "
            "Slicer will continue without it"
        )
        return False


def install() -> bool:
    """Schedule managed module loading without changing Slicer configuration."""

    try:
        import qt
        import slicer
    except Exception:  # noqa: BLE001 - keep a user startup file non-fatal
        LOGGER.exception(
            "dMRI-Agent user startup bootstrap requires the embedded Slicer runtime"
        )
        return False
    return _install(slicer, qt, Path(__file__))


if __name__ == "__main__":
    install()
