#!/usr/bin/env python3
"""Native installer orchestration; shares transaction primitives with install.sh."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.platform import (IS_WINDOWS, access_safe, config_home, state_home, file_lock,
                           is_reparse_point, secure_path, venv_python, python_launch_path)
from scripts import installer_support as support
from scripts import slicer_rc_registration as registration
from scripts.slicer_registration import discover_slicers


@dataclass(frozen=True)
class Layout:
    app: Path
    config: Path
    state: Path
    workspace: Path
    rc: Path

    @property
    def metadata(self):
        return self.config / 'install.json'

    @property
    def registry(self):
        return self.config / 'slicer-startup-registration.json'

    @property
    def marker(self):
        return self.app / '.dmri-agent-install'

    def environment(self) -> dict[str, str]:
        result = dict(os.environ)
        for key in ('PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE', 'VIRTUAL_ENV'):
            result.pop(key, None)
        result.update(PYTHONPATH=str(self.app), PYTHONNOUSERSITE='1', PYTHONUTF8='1',
                      DMRI_AGENT_INSTALL_CONFIG=str(self.metadata), DMRI_SECRETS_FILE=str(self.config / 'secrets.json'),
                      DMRI_STATE_HOME=str(self.state), DMRI_PROTOCOLS_HOME=str(self.state / 'protocols'),
                      DMRI_AGENT_PYTHON=str(venv_python(self.app)))
        return result


def _canonical(path: Path) -> Path:
    if '\n' in str(path) or '\r' in str(path):
        raise ValueError('Installation paths must not contain line breaks')
    return path.expanduser().resolve()


def _outside(app: Path, name: str, path: Path) -> None:
    if path.is_relative_to(app):
        raise ValueError(f'{name} must be outside the application directory: {path}')


def _private_directory(path: Path):
    if path.exists() and is_reparse_point(path):
        raise ValueError(f'Refusing a reparse point: {path}')
    path.mkdir(parents=True, exist_ok=True)
    secure_path(path, 0o700)


def _read_record(path: Path) -> dict:
    if not path.exists():
        return {}
    if not path.is_file() or not access_safe(path, private=True):
        raise ValueError(f'Installation record is not private: {path}')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'Invalid installation record: {path}')
    return payload


def _snapshot(path: Path):
    return path.read_bytes() if path.is_file() else None


def _restore(path: Path, previous):
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        from backend.runtime_state import atomic_write_text
        atomic_write_text(path, previous.decode('utf-8'))


def _require_quiet(layout: Layout) -> None:
    support.require_backend_stopped(layout.state)


def check(app: Path, env: dict[str, str]) -> None:
    python = python_launch_path(venv_python(app))
    if not python.is_file():
        raise ValueError('The installed Python environment is missing')
    subprocess.run([str(python), '-c', 'import fastapi,uvicorn,langchain_core; import backend.service.api; import backend.tools'],
                   cwd=app, env=env, check=True)
    print('dMRI-Agent installation self-check passed.', flush=True)


def install(source: Path, layout: Layout, *, slicers: list[str], register: bool = True,
            backend_port: int = 8765, workspace_explicit: bool = False,
            prepare_runtime=None, validate_runtime=None) -> None:
    """Activate a staged payload, keep old app until validation, then commit."""
    source = _canonical(source)
    app = layout.app
    if source == app or source.is_relative_to(app):
        raise ValueError('Install source must be outside the managed application')
    if Path(sys.executable).resolve().is_relative_to(app):
        raise ValueError('Run installation with Python outside the application environment')
    for name, path in [('configuration', layout.config), ('state', layout.state),
                       ('workspace', layout.workspace), ('Slicer startup', layout.rc)]:
        _outside(app, name, path)
    if layout.workspace == Path(layout.workspace.anchor):
        raise ValueError('A filesystem root cannot be the workspace')
    if not 1 <= backend_port <= 65535:
        raise ValueError('Backend port must be between 1 and 65535')
    found = discover_slicers(slicers)
    if register and not found:
        raise ValueError('3D Slicer was not found. Pass -Slicer C:\\path\\to\\Slicer.exe')
    for path in (layout.config, layout.state, app.parent):
        if path.exists() and is_reparse_point(path):
            raise ValueError(f'Refusing a reparse point: {path}')
    _private_directory(layout.config)
    _private_directory(layout.state)
    app.parent.mkdir(parents=True, exist_ok=True)
    journal = app.parent / f'.{app.name}.dmri-agent-upgrade.json'
    rc_journal = layout.config / '.slicer-startup-transaction.json'
    with file_lock(layout.config / 'installer.lock'):
        _require_quiet(layout)
        if journal.exists():
            support.recover_managed_upgrade(journal)
        if rc_journal.exists():
            registration.rollback(rc_journal)
        previous = _read_record(layout.metadata)
        if previous and _canonical(Path(previous.get('app_dir', ''))) != app:
            raise ValueError('Existing configuration belongs to another application')
        if previous:
            for field, expected in [('state_home', layout.state), ('config_home', layout.config)]:
                if previous.get(field) and _canonical(Path(previous[field])) != expected:
                    raise ValueError(f'Existing {field} differs from this installation')
        selected = layout.workspace
        if not workspace_explicit and previous.get('workspace'):
            selected = _canonical(Path(previous['workspace']))
        _outside(app, 'workspace', selected)
        if selected == Path(selected.anchor):
            raise ValueError('A filesystem root cannot be the workspace')
        action = support.managed_target_action(source, app)
        if action == 'recover':
            # The shared helper validated this same-version, owner-only sentinel.
            support._remove_transaction_tree(app, app.parent)
            action = 'install'
        if app.exists() and action == 'install' and any(app.iterdir()):
            raise ValueError('The application directory is not empty')
        old_metadata, old_registry = _snapshot(layout.metadata), _snapshot(layout.registry)
        token = secrets.token_hex(24)
        stage = app.parent / f'.{app.name}.dmri-agent-stage-{token}'
        activated = False
        upgraded = False
        completed = False
        committing = False
        try:
            support.managed_copy(source, stage)
            secure_path(stage, 0o700)
            pending = stage / '.dmri-agent-install-pending'
            pending.write_text(token, encoding='ascii')
            secure_path(pending)
            if action in {'upgrade', 'repair'}:
                support.begin_managed_upgrade(source, app, stage, journal, token)
                upgraded = True
            else:
                if app.exists():
                    app.rmdir()
                stage.rename(app)
            activated = True
            selected.mkdir(parents=True, exist_ok=True)
            env = layout.environment()
            if prepare_runtime is None:
                subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(app / '.venv')], env=env, check=True)
                python = python_launch_path(venv_python(app))
                # Run ensurepip directly so an initialization failure retains its
                # real output instead of venv hiding it in check_output().
                subprocess.run([str(python), '-m', 'ensurepip', '--upgrade', '--default-pip'], env=env, cwd=app, check=True)
                subprocess.run([str(python), '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
                                '--require-hashes', '--only-binary=:all:', '-r', str(app / 'backend/requirements.lock.txt')],
                               env=env, cwd=app, check=True)
            else:
                prepare_runtime(app, env)
            (validate_runtime or check)(app, env)
            registered = bool(old_registry)
            if register:
                result = registration.register(layout.rc, app / 'slicer_extension/user_startup_bootstrap.py', str(app),
                         slicer_home_rc=[p.parent / '.slicerrc.py' for p in found],
                         slicerrc_override=os.environ.get('SLICERRC'), journal_path=rc_journal)
                support._atomic_json(layout.registry, result)
                registered = True
            version = (app / 'VERSION').read_text().strip()
            support.write_metadata(layout.metadata, app, version, backend_port, workspace=selected)
            payload = _read_record(layout.metadata)
            payload.update(config_home=str(layout.config), state_home=str(layout.state),
                           bootstrap_python=sys.executable, slicer_rc=str(layout.rc), native_platform='windows' if IS_WINDOWS else sys.platform)
            support._atomic_json(layout.metadata, payload)
            values = dict(mode='managed', app_dir=str(app), bin_dir=str(layout.config.parent / 'bin'),
                          state_home=str(layout.state), config_home=str(layout.config), protocols_home=str(layout.state / 'protocols'),
                          install_json=str(layout.metadata), slicer_registry=str(layout.registry), slicer_rc=str(layout.rc),
                          slicer_registered='1' if registered else '0', web='0', cli='0',
                          installed_at=datetime.now(timezone.utc).isoformat(), workspace=str(selected))
            support.write_marker(layout.marker, values)
            support.finalize_pending_install(app, app)
            if upgraded:
                # After commit starts, recovery must finish cleanup. Rolling
                # back would restore metadata while leaving the new app active.
                committing = True
                support.commit_managed_upgrade(journal)
            completed = True
            registration.commit(rc_journal)
        except Exception as failure:
            if committing:
                # The validated new payload and its metadata are already live.
                # Preserve the original cleanup error and recovery journals.
                if rc_journal.exists():
                    try:
                        registration.commit(rc_journal)
                    except Exception as rc_error:
                        failure.add_note(f'Slicer registration cleanup also failed: {rc_error}')
                raise
            if not completed:
                if rc_journal.exists():
                    registration.rollback(rc_journal)
                _restore(layout.metadata, old_metadata)
                _restore(layout.registry, old_registry)
                if upgraded and journal.exists():
                    support.rollback_managed_upgrade(journal)
                elif activated and app.is_dir():
                    if support._pending_token(app) != token:
                        raise ValueError('Interrupted application changed ownership; preserving it')
                    support._remove_transaction_tree(app, app.parent)
                elif stage.is_dir():
                    support._remove_transaction_tree(stage, app.parent)
            raise
    print(f'dMRI-Agent installed. Open Slicer normally. Workspace: {selected}', flush=True)


def uninstall(layout: Layout, *, purge: bool = False) -> None:
    app = layout.app
    if Path(sys.executable).resolve().is_relative_to(app):
        raise ValueError('Uninstall requires the bootstrap Python outside the application')
    with file_lock(layout.config / 'installer.lock'):
        _require_quiet(layout)
        values = support._validate_managed_app(app, app)
        record = _read_record(layout.metadata)
        if record and _canonical(Path(record.get('app_dir', ''))) != app:
            raise ValueError('Installation metadata belongs to another application')
        for field, expected in [('config_home',layout.config),('state_home',layout.state),('slicer_rc',layout.rc)]:
            if values.get(field) and _canonical(Path(values[field])) != expected:
                raise ValueError(f'Refusing mismatched {field}')
        support.validate_managed_layout(app, layout.marker, layout.metadata)
        workspaces = [_canonical(Path(v)) for v in (values.get('workspace'), record.get('workspace')) if v]
        targets = [layout.config, layout.state, _canonical(Path(values['protocols_home'])), app]
        if purge:
            support.validate_purge_paths(targets, workspaces)
        if values.get('slicer_registered') == '1':
            if layout.registry.is_file():
                registration.unregister_from_record(layout.registry, str(app))
                layout.registry.unlink()
            else:
                registration.unregister(layout.rc, str(app))
        support.remove_metadata(layout.metadata, app)
        support._remove_transaction_tree(app, app.parent)
    # Windows cannot delete the open installer lock. Cleanup starts after releasing it.
    if purge:
        for target in targets[:-1]:
            if target.exists():
                support._remove_transaction_tree(target, target.parent)
    print('dMRI-Agent removed. Workspaces were preserved.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=ROOT)
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--slicer', action='append', default=[])
    parser.add_argument('--no-slicer-register', action='store_true')
    parser.add_argument('--backend-port', type=int, default=8765)
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--purge', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    if args.purge and not args.uninstall:
        parser.error('--purge is only valid with --uninstall')
    if not IS_WINDOWS:
        parser.error('Use install.sh on macOS/Linux; this entry point targets native Windows')
    config = _canonical(config_home())
    # The existing config directory can resolve through MSIX redirection while
    # a not-yet-visible sibling state directory does not. Anchor default state
    # to that canonical directory, preserving explicit state overrides.
    state = _canonical(state_home()) if (os.environ.get('DMRI_STATE_HOME') or os.environ.get('XDG_DATA_HOME')) else config.parent / 'state'
    record = _read_record(config / 'install.json')
    app = _canonical(args.install_dir or Path(record.get('app_dir') or config.parent / 'app'))
    workspace = _canonical(args.workspace or Path(record.get('workspace') or Path.home() / 'Documents/dmri-agent-workspace'))
    rc = _canonical(Path(record.get('slicer_rc') or Path.home() / '.slicerrc.py'))
    layout = Layout(app, config, state, workspace, rc)
    try:
        if args.check:
            check(app, layout.environment())
        elif args.uninstall:
            if args.purge and not args.yes:
                raise ValueError('Use --yes with --purge after reviewing which state will be removed')
            uninstall(layout, purge=args.purge)
        else:
            install(args.source_dir, layout, slicers=args.slicer, register=not args.no_slicer_register,
                    backend_port=args.backend_port, workspace_explicit=args.workspace is not None)
    except (OSError, ValueError, subprocess.SubprocessError, registration.SlicerRCError) as exc:
        traceback.print_exc()
        print(f'Installation stopped: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
