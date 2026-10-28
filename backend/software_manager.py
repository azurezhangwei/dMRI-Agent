from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.project_paths import PROJECT_ROOT
from backend.runtime_state import atomic_write_text, managed_protocols_home, state_home


class SoftwareManagerError(RuntimeError):
    pass


class SoftwareManager:
    def __init__(
        self,
        *,
        catalog_path: Path | None = None,
        state_root: Path | None = None,
        protocols_root: Path | None = None,
    ) -> None:
        self.catalog_path = (catalog_path or PROJECT_ROOT / "config" / "software_catalog.json").resolve()
        self.state_root = (state_root or state_home()).expanduser().resolve()
        self.protocols_root = (protocols_root or managed_protocols_home()).expanduser().resolve()

    def catalog(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SoftwareManagerError(f"cannot load software catalog: {exc}") from exc
        if not isinstance(payload.get("software"), dict):
            raise SoftwareManagerError("software catalog must contain a software object")
        return payload

    def inventory(self, *, refresh_protocol: bool = True) -> dict[str, Any]:
        catalog = self.catalog()
        configured_python = (os.environ.get("DMRI_AGENT_PYTHON") or "").strip()
        runtime_python = configured_python or sys.executable
        if not Path(runtime_python).is_absolute():
            runtime_python = shutil.which(runtime_python) or runtime_python
        # Preserve the virtual-environment entrypoint instead of resolving its
        # symlink to the base interpreter, which would bypass venv site-packages.
        runtime_python_path = str(Path(runtime_python).expanduser().absolute())
        software: list[dict[str, Any]] = []
        for software_id, entry in catalog["software"].items():
            commands = [
                self._probe_command(spec, software_id=software_id)
                for spec in entry.get("commands", [])
            ]
            modules = [
                {"name": name, "available": importlib.util.find_spec(name) is not None}
                for name in entry.get("python_modules", [])
            ]
            software.append(
                {
                    "id": software_id,
                    "label": entry.get("label", software_id),
                    "category": entry.get("category", "other"),
                    "available": any(item["available"] for item in commands + modules),
                    "commands": commands,
                    "python_modules": modules,
                    "recommended_strategy": self._strategy_summary(entry),
                }
            )
        payload = {
            "schema_version": 1,
            "generated_at": _utc_now(),
            "platform": {
                "system": platform.system(),
                "platform_key": self.platform_key(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": sys.version.split()[0],
            },
            "agent_runtime": {
                "python": runtime_python_path,
                "python_version": sys.version.split()[0],
                "shell": "Windows PowerShell 5.1" if os.name == "nt" else "Bash",
                "pty_supported": os.name != "nt",
            },
            "software": software,
        }
        atomic_write_text(
            self.state_root / "software_inventory.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        if refresh_protocol:
            self._write_runtime_protocol(payload)
        return payload

    def plan(
        self,
        software_id: str,
        *,
        action: str,
        strategy: str = "auto",
    ) -> dict[str, Any]:
        if action not in {"install", "remove"}:
            raise SoftwareManagerError("action must be install or remove")
        catalog = self.catalog()
        try:
            entry = catalog["software"][software_id]
        except KeyError as exc:
            raise SoftwareManagerError(f"unknown software id: {software_id}") from exc
        strategies = entry.get("strategies", {}).get(self.platform_key(), [])
        if not strategies:
            raise SoftwareManagerError(
                f"{software_id} has no catalogued strategy for {self.platform_key()}"
            )
        selected = strategies[0] if strategy == "auto" else next(
            (item for item in strategies if item.get("type") == strategy),
            None,
        )
        if not selected:
            raise SoftwareManagerError(f"strategy not available: {strategy}")
        strategy_type = str(selected.get("type") or "")
        commands: list[list[str]] = []
        invocation = ""
        automated = strategy_type in {"homebrew", "conda", "container", "python_venv"}
        cleanup_path = ""

        if strategy_type == "homebrew":
            brew = shutil.which("brew")
            if not brew:
                automated = False
            elif action == "install":
                if selected.get("tap"):
                    commands.append([brew, "tap", str(selected["tap"])])
                commands.append([brew, "install", str(selected["formula"])])
            else:
                commands.append([brew, "uninstall", str(selected["formula"])])
        elif strategy_type == "conda":
            manager = next(
                (
                    executable
                    for name in ("micromamba", "mamba", "conda")
                    if (executable := shutil.which(name))
                ),
                None,
            )
            if not manager:
                automated = False
            else:
                env_name = str(catalog.get("managed_conda_env") or "dmri-agent-tools")
                packages = [str(item) for item in selected.get("packages", [])]
                channel_args = ["-c", str(selected["channel"])] if selected.get("channel") else []
                if action == "install":
                    verb = "install" if self._conda_env_exists(manager, env_name) else "create"
                    commands.append([manager, verb, "-y", "-n", env_name, *channel_args, *packages])
                else:
                    commands.append([manager, "remove", "-y", "-n", env_name, *packages])
                primary_command = str(entry.get("commands", [{}])[0].get("candidates", [""])[0])
                invocation = f"{Path(manager).name} run -n {env_name} {primary_command}".strip()
        elif strategy_type == "container":
            runtime = shutil.which("docker") or shutil.which("podman")
            image = str(selected.get("image") or "")
            if not runtime or not image:
                automated = False
            elif action == "install":
                commands.append([runtime, "pull", image])
                invocation = (
                    f"{Path(runtime).name} run --rm -v \"$PWD:/work\" -w /work {image} <command>"
                )
            else:
                commands.append([runtime, "image", "rm", image])
        elif strategy_type == "python_venv":
            packages = [str(item) for item in selected.get("packages", [])]
            if not packages:
                automated = False
            else:
                environment = self._managed_venv(software_id)
                environment_python = self._venv_executable(environment, "python")
                primary_command = str(entry.get("commands", [{}])[0].get("candidates", [""])[0])
                invocation = str(self._venv_executable(environment, primary_command))
                if action == "install":
                    commands.extend(
                        [
                            [sys.executable, "-m", "venv", str(environment)],
                            [
                                str(environment_python),
                                "-m",
                                "pip",
                                "install",
                                "--disable-pip-version-check",
                                "--no-input",
                                *packages,
                            ],
                        ]
                    )
                else:
                    cleanup_path = str(environment)

        return {
            "software_id": software_id,
            "label": entry.get("label", software_id),
            "action": action,
            "platform": self.platform_key(),
            "strategy": strategy_type,
            "automated": automated,
            "commands": commands,
            "cleanup_path": cleanup_path,
            "invocation": invocation,
            "summary": selected.get("summary", ""),
            "source_url": selected.get("source_url", ""),
            "isolation": (
                "dedicated Python virtual environment"
                if strategy_type == "python_venv"
                else "dedicated conda environment"
                if strategy_type == "conda"
                else "container image"
                if strategy_type == "container"
                else "native host installation"
            ),
        }

    def manage(
        self,
        software_id: str,
        *,
        action: str,
        strategy: str = "auto",
        execute: bool = False,
    ) -> dict[str, Any]:
        plan = self.plan(software_id, action=action, strategy=strategy)
        if not execute:
            return {"ok": True, "executed": False, "plan": plan}
        if not plan["automated"] or (not plan["commands"] and not plan["cleanup_path"]):
            raise SoftwareManagerError(
                "this strategy requires manual installation; follow source_url and summary"
            )
        results: list[dict[str, Any]] = []
        command_environment = os.environ.copy()
        if plan["strategy"] == "homebrew":
            command_environment.update(
                {
                    "HOMEBREW_NO_AUTO_UPDATE": "1",
                    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
                    "HOMEBREW_NO_ENV_HINTS": "1",
                }
            )
        for command in plan["commands"]:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("DMRI_SOFTWARE_TIMEOUT", "3600")),
                check=False,
                env=command_environment,
            )
            result = {
                "command": command,
                "return_code": completed.returncode,
                "stdout": completed.stdout[-12000:],
                "stderr": completed.stderr[-12000:],
            }
            results.append(result)
            if completed.returncode != 0:
                if plan["strategy"] == "python_venv" and plan["action"] == "install":
                    self._remove_managed_venv(Path(plan["invocation"]).parent.parent)
                raise SoftwareManagerError(
                    json.dumps({"message": "software command failed", "result": result})
                )
        if plan["cleanup_path"]:
            self._remove_managed_venv(Path(plan["cleanup_path"]))
            results.append(
                {
                    "command": ["managed-remove", plan["cleanup_path"]],
                    "return_code": 0,
                    "stdout": "managed tool environment removed",
                    "stderr": "",
                }
            )
        inventory = self.inventory(refresh_protocol=True)
        return {"ok": True, "executed": True, "plan": plan, "results": results, "inventory": inventory}

    @staticmethod
    def platform_key() -> str:
        if sys.platform == "darwin":
            return "darwin"
        if sys.platform.startswith("linux"):
            return "linux"
        if sys.platform.startswith("win"):
            return "win32"
        return sys.platform

    def _probe_command(self, spec: dict[str, Any], *, software_id: str) -> dict[str, Any]:
        candidates = [str(item) for item in spec.get("candidates", [])]
        executable = next(
            (
                resolved
                for item in candidates
                if (resolved := self._resolve_executable(item, software_id=software_id))
            ),
            None,
        )
        if not executable:
            return {"name": candidates[0] if candidates else "", "available": False}
        version = ""
        version_args = [str(item) for item in spec.get("version_args", [])]
        if version_args:
            try:
                completed = subprocess.run(
                    [executable, *version_args],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                output = "\n".join(
                    part.strip()
                    for part in (completed.stdout, completed.stderr)
                    if part and part.strip()
                )
                version = self._extract_version(
                    output,
                    str(spec.get("version_pattern") or ""),
                )
            except (OSError, subprocess.TimeoutExpired):
                version = ""
        return {
            "name": Path(executable).name,
            "available": True,
            "path": executable,
            "version": version,
        }

    @staticmethod
    def _extract_version(output: str, pattern: str = "") -> str:
        if pattern:
            try:
                match = re.search(pattern, output)
            except re.error:
                match = None
            if match:
                value = match.group(1) if match.lastindex else match.group(0)
                return value.strip()[:300]
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return lines[0][:300] if lines else ""

    def _resolve_executable(self, candidate: str, *, software_id: str) -> str | None:
        if not candidate:
            return None
        if Path(candidate).is_absolute():
            return candidate if os.access(candidate, os.X_OK) else None
        managed = self._venv_executable(self._managed_venv(software_id), candidate)
        if os.access(managed, os.X_OK):
            return str(managed)
        return shutil.which(candidate)

    def _managed_venv(self, software_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,63}", software_id):
            raise SoftwareManagerError(f"invalid software id: {software_id}")
        return (self.state_root / "tools" / "venvs" / software_id).resolve()

    @staticmethod
    def _venv_executable(environment: Path, command: str) -> Path:
        directory = "Scripts" if sys.platform.startswith("win") else "bin"
        suffix = ".exe" if sys.platform.startswith("win") and not command.endswith(".exe") else ""
        return environment / directory / f"{command}{suffix}"

    def _remove_managed_venv(self, environment: Path) -> None:
        managed_root = (self.state_root / "tools" / "venvs").resolve()
        candidate = environment.expanduser().resolve()
        try:
            relative = candidate.relative_to(managed_root)
        except ValueError as exc:
            raise SoftwareManagerError("refusing to remove path outside managed tool root") from exc
        if len(relative.parts) != 1 or candidate.is_symlink():
            raise SoftwareManagerError("refusing to remove an invalid managed tool path")
        if candidate.exists():
            shutil.rmtree(candidate)

    def _strategy_summary(self, entry: dict[str, Any]) -> dict[str, str] | None:
        strategies = entry.get("strategies", {}).get(self.platform_key(), [])
        if not strategies:
            return None
        selected = strategies[0]
        return {
            "type": str(selected.get("type") or ""),
            "source_url": str(selected.get("source_url") or ""),
        }

    @staticmethod
    def _conda_env_exists(manager: str, env_name: str) -> bool:
        try:
            completed = subprocess.run(
                [manager, "env", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            payload = json.loads(completed.stdout or "{}")
            return any(Path(item).name == env_name for item in payload.get("envs", []))
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return False

    def _write_runtime_protocol(self, inventory: dict[str, Any]) -> None:
        available = [item for item in inventory["software"] if item["available"]]
        unavailable = [item for item in inventory["software"] if not item["available"]]
        lines = [
            "---",
            "name: Local Runtime Software Inventory",
            "description: Generated inventory of software detected in the active dMRI-Agent host runtime.",
            "version: 1.0.0",
            "type: supplement",
            "---",
            "",
            "# Local Runtime Software Inventory",
            "",
            f"Generated: `{inventory['generated_at']}`",
            f"Platform: `{inventory['platform']['system']} {inventory['platform']['release']} {inventory['platform']['machine']}`",
            "",
            "## Agent Runtime",
            "",
            f"- Python templates: `{inventory['agent_runtime']['python']}`",
            f"- Python version: `{inventory['agent_runtime']['python_version']}`",
            "- Run bundled Python Protocol resources with this exact interpreter; do not substitute a host Conda or system Python.",
            "",
            "## Available",
            "",
        ]
        if not available:
            lines.append("- No catalogued neuroimaging commands were detected on PATH.")
        for item in available:
            commands = [
                f"`{command['name']}` at `{command.get('path', '')}`"
                for command in item["commands"]
                if command["available"]
            ]
            modules = [
                f"Python module `{module['name']}`"
                for module in item["python_modules"]
                if module["available"]
            ]
            lines.append(f"- **{item['label']}**: " + ", ".join(commands + modules))
        lines.extend(["", "## Not Detected", ""])
        lines.extend(f"- {item['label']}" for item in unavailable)
        lines.extend(
            [
                "",
                "## Use",
                "",
                "- Prefer the detected absolute command path or the current PATH.",
                "- Refresh this inventory after software installation or removal.",
                "- This generated inventory records availability only; algorithm and QC rules remain in the pipeline protocol.",
            ]
        )
        target = self.protocols_root / "RUNTIME_LOCAL" / "PROTOCOL.md"
        atomic_write_text(target, "\n".join(lines) + "\n")


def software_inventory(refresh: bool = True) -> str:
    """Probe the current host and refresh the generated RUNTIME_LOCAL protocol."""
    try:
        payload = SoftwareManager().inventory(refresh_protocol=refresh)
        return json.dumps(payload, ensure_ascii=False)
    except SoftwareManagerError as exc:
        return f"[error] {exc}"


def software_manage(
    software_id: str,
    action: str,
    strategy: str = "auto",
    execute: bool = False,
) -> str:
    """Plan or execute a catalogued software install/remove action without arbitrary shell input."""
    try:
        payload = SoftwareManager().manage(
            software_id,
            action=action,
            strategy=strategy,
            execute=execute,
        )
        return json.dumps(payload, ensure_ascii=False)
    except SoftwareManagerError as exc:
        return f"[error] {exc}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
