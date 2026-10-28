"""Standard-library platform services shared by installer, Slicer and backend."""
from __future__ import annotations

import contextlib
import base64
from functools import lru_cache
import ctypes
import os
from pathlib import Path
import signal
import stat
import subprocess
import time
from typing import Mapping

IS_WINDOWS = os.name == 'nt'


def config_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get('XDG_CONFIG_HOME'):
        return Path(env['XDG_CONFIG_HOME']).expanduser() / 'dmri-agent'
    if IS_WINDOWS:
        return Path(env.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'dMRI-Agent/config'
    return Path(env.get('HOME') or Path.home()).expanduser() / '.config/dmri-agent'


def state_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get('DMRI_STATE_HOME'):
        return Path(env['DMRI_STATE_HOME']).expanduser()
    if env.get('XDG_DATA_HOME'):
        return Path(env['XDG_DATA_HOME']).expanduser() / 'dmri-agent'
    if IS_WINDOWS:
        return Path(env.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'dMRI-Agent/state'
    return Path(env.get('HOME') or Path.home()).expanduser() / '.local/share/dmri-agent'


def venv_python(app: Path, *, windows: bool | None = None) -> Path:
    windows = IS_WINDOWS if windows is None else windows
    return app / '.venv' / ('Scripts/python.exe' if windows else 'bin/python')


def filesystem_path(path: Path) -> Path:
    """Use extended DOS paths on Windows without changing recorded locations."""
    if not IS_WINDOWS:
        return path
    raw = str(path.absolute())
    return Path(raw if raw.startswith('\\\\?\\') else ('\\\\?\\UNC\\' + raw[2:] if raw.startswith('\\\\') else '\\\\?\\' + raw))


def python_launch_path(path: Path) -> Path:
    """Keep pip's relative wheel paths valid while shortening Windows roots."""
    if not IS_WINDOWS:
        return path
    _, kernel = _api()
    buffer = ctypes.create_unicode_buffer(32768)
    size = kernel.GetShortPathNameW(str(path), buffer, len(buffer))
    return Path(buffer.value) if 0 < size < len(buffer) else path


def is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


@lru_cache(maxsize=1)
def _api():
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    def bind(lib, name, args, result=w.BOOL):
        fn = getattr(lib, name); fn.argtypes = args; fn.restype = result; return fn
    bind(kernel, 'GetCurrentProcess', [], w.HANDLE)
    bind(kernel, 'CloseHandle', [w.HANDLE])
    bind(kernel, 'LocalFree', [ctypes.c_void_p], ctypes.c_void_p)
    bind(kernel, 'OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
    bind(kernel, 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)
    bind(kernel, 'GetProcessTimes', [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)])
    bind(kernel, 'GetSystemDirectoryW', [w.LPWSTR, w.UINT], w.UINT)
    bind(kernel, 'GetShortPathNameW', [w.LPCWSTR, w.LPWSTR, w.DWORD], w.DWORD)
    bind(kernel, 'CreateToolhelp32Snapshot', [w.DWORD, w.DWORD], w.HANDLE)
    bind(kernel, 'Process32FirstW', [w.HANDLE, ctypes.c_void_p])
    bind(kernel, 'Process32NextW', [w.HANDLE, ctypes.c_void_p])
    bind(adv, 'OpenProcessToken', [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)])
    bind(adv, 'GetTokenInformation', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)])
    bind(adv, 'ConvertSidToStringSidW', [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)])
    bind(adv, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD)])
    bind(adv, 'GetSecurityDescriptorDacl', [ctypes.c_void_p, ctypes.POINTER(w.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL)])
    bind(adv, 'GetSecurityDescriptorOwner', [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL)])
    bind(adv, 'SetNamedSecurityInfoW', [w.LPWSTR, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], w.DWORD)
    bind(adv, 'GetNamedSecurityInfoW', [w.LPWSTR, ctypes.c_int, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], w.DWORD)
    bind(adv, 'GetAce', [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)])
    bind(adv, 'CheckTokenMembership', [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(w.BOOL)])
    return adv, kernel


def _sid_text(sid, api=None) -> str:
    from ctypes import wintypes as w
    adv, kernel = api or _api()
    text = w.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def current_windows_sid() -> str:
    from ctypes import wintypes as w
    adv, kernel = _api(); token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        return _sid_text(sid, (adv, kernel))
    finally:
        kernel.CloseHandle(token)


def _windows_access_safe(path: Path, *, private: bool) -> bool:
    adv, kernel = _api()
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    error = adv.GetNamedSecurityInfoW(str(path), 1, 0x00000005, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        sid = current_windows_sid()
        if not owner or not dacl:
            return False
        owner_sid = _sid_text(owner, (adv, kernel))
        # SYSTEM may own a legitimate Windows profile directory. Accept it only
        # with the same trusted DACL and explicit current-user control below.
        if owner_sid not in {sid, 'S-1-5-18', 'S-1-5-32-544'}:
            return False
        # ACL header: revision/unused (2 bytes), size (2), AceCount (2), unused (2).
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        trusted = {sid, 'S-1-5-18', 'S-1-5-32-544'}
        user_controls_file = False
        admin_controls_file = False
        for index in range(count):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            if flags & 0x08 or kind == 1:  # inherit-only or deny ACE grants no access here
                continue
            if kind != 0:
                return False  # refuse an ACL form that this validator cannot inspect
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            principal = _sid_text(ctypes.c_void_p(ace.value + 8), (adv, kernel))
            # OWNER RIGHTS refers to this object's validated owner, not another
            # account. Python's mode-0700 ACL uses this well-known SID.
            if principal == 'S-1-3-4':
                principal = owner_sid
            if principal == sid and (mask & 0x10000000 or mask & 0x001f01ff == 0x001f01ff):
                user_controls_file = True
            if principal == 'S-1-5-32-544' and (mask & 0x10000000 or mask & 0x001f01ff == 0x001f01ff):
                admin_controls_file = True
            forbidden = 0xffffffff if private else 0x500d0116
            if principal not in trusted and mask & forbidden:
                return False
        # Elevated Windows processes may create Administrators-owned objects.
        # Require the individual user or verified effective owner membership.
        if owner_sid == sid or user_controls_file:
            return True
        # Python's mode-0700 directories may grant only the default token owner
        # (Administrators for an elevated process). Verify the *effective* token
        # membership before normalizing such an object to the individual user.
        if admin_controls_file:
            from ctypes import wintypes as w
            member = w.BOOL()
            if not adv.CheckTokenMembership(None, owner, ctypes.byref(member)):
                raise ctypes.WinError(ctypes.get_last_error())
            return bool(member.value)
        return False
    finally:
        kernel.LocalFree(descriptor)


def access_safe(path: Path, *, private: bool = False) -> bool:
    try:
        info = path.lstat()
        if is_reparse_point(path):
            return False
        if IS_WINDOWS:
            return _windows_access_safe(path, private=private)
        if hasattr(os, 'getuid') and info.st_uid != os.getuid():
            return False
        return not bool(stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022))
    except OSError:
        return False


def secure_path(path: Path, mode: int = 0o600) -> None:
    """Use POSIX modes or an explicit current-user/SYSTEM protected Windows DACL."""
    if not IS_WINDOWS:
        os.chmod(path, mode)
        return
    from ctypes import wintypes as w
    if is_reparse_point(path):
        raise PermissionError(f'Refusing a reparse point: {path}')
    if not _windows_access_safe(path, private=False):
        raise PermissionError(f'Path is not safely owned by the current user: {path}')
    adv, kernel = _api(); descriptor = ctypes.c_void_p()
    sid = current_windows_sid(); inherit = 'OICI' if path.is_dir() else ''
    sddl = f'O:{sid}D:P(A;{inherit};FA;;;{sid})(A;{inherit};FA;;;SY)'
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted, dacl = w.BOOL(), w.BOOL(), ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        owner = ctypes.c_void_p()
        if not adv.GetSecurityDescriptorOwner(descriptor, ctypes.byref(owner), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        error = adv.SetNamedSecurityInfoW(str(path), 1, 0x80000005, owner, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)


def fd_permissions(fd: int, mode: int, path: Path) -> None:
    if IS_WINDOWS:
        secure_path(path, mode)
    else:
        os.fchmod(fd, mode)


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 15):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not access_safe(path):
        raise PermissionError(f'Unsafe lock file: {path}')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        secure_path(path)
        if IS_WINDOWS:
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            end = time.monotonic() + timeout
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1); break
                except OSError:
                    if time.monotonic() >= end:
                        raise TimeoutError(f'Another dMRI-Agent operation holds {path}')
                    time.sleep(.05)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if IS_WINDOWS:
                os.lseek(fd, 0, os.SEEK_SET); msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def system_directory() -> Path:
    _, kernel = _api(); buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


def shell_command(command: str, *, login: bool = True) -> list[str]:
    if not IS_WINDOWS:
        return ['bash', '-lc' if login else '-c', command]
    shell = system_directory() / 'WindowsPowerShell/v1.0/powershell.exe'
    script = ("$ErrorActionPreference='Stop'; $OutputEncoding=New-Object System.Text.UTF8Encoding; "
              "[Console]::OutputEncoding=$OutputEncoding; $global:LASTEXITCODE=0; & {\n" + command +
              "\n}; if (-not $?) { if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; exit 1 }; exit $LASTEXITCODE")
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return [str(shell), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]


def spawn_options() -> dict:
    return {'creationflags': 0x08000200} if IS_WINDOWS else {'start_new_session': True}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    _, kernel = _api(); handle = kernel.OpenProcess(0x00101000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def process_creation_time(pid: int) -> float | None:
    if not IS_WINDOWS:
        return None
    from ctypes import wintypes as w
    _, kernel = _api()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, exited, cpu_kernel, cpu_user = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
            return None
        return ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 10_000_000 - 11_644_473_600
    finally:
        kernel.CloseHandle(handle)


def process_descends_from(pid: int, ancestor_pid: int) -> bool:
    """Verify a live Windows launcher/worker chain, including PID reuse checks."""
    if not IS_WINDOWS or pid <= 0 or ancestor_pid <= 0 or pid == ancestor_pid:
        return False
    from ctypes import wintypes as w
    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', w.DWORD),
                    ('cntThreads', w.DWORD), ('th32ParentProcessID', w.DWORD),
                    ('pcPriClassBase', w.LONG), ('dwFlags', w.DWORD), ('szExeFile', w.WCHAR * 260)]
    _, kernel = _api()
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        entry = ProcessEntry(); entry.dwSize = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    child_started = process_creation_time(pid)
    if child_started is None:
        return False
    for _ in range(16):
        parent = parents.get(pid, 0)
        if parent <= 0 or parent == pid:
            return False
        parent_started = process_creation_time(parent)
        if parent_started is None or parent_started > child_started:
            return False
        if parent == ancestor_pid:
            return True
        pid, child_started = parent, parent_started
    return False


def terminate_tree(process, *, force: bool = False) -> None:
    """Terminate a tree only while its owned parent process is still running."""
    code = process.returncode if not hasattr(process, 'poll') else process.poll()
    if code is not None:
        return
    if IS_WINDOWS:
        result = subprocess.run([str(system_directory() / 'taskkill.exe'), '/PID', str(process.pid), '/T', '/F'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=0x08000000, timeout=15)
        if result.returncode and pid_alive(process.pid):
            raise OSError(f'Could not stop the owned process tree (PID {process.pid})')
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.kill() if force else process.terminate()
