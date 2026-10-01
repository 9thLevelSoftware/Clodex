"""Process helpers: isolate child processes and reliably stop a whole process tree.

On Windows `os.kill(pid, sig)` calls TerminateProcess (even for signal 0), so liveness
checks and tree kills go through ctypes / taskkill there instead.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time


def popen_isolation_kwargs() -> dict[str, object]:
    """Start a child in its own process group/session so its whole tree can be stopped."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(int(pid))
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means the process exists but belongs to someone else.
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def kill_tree(pid: int, grace: float = 3.0) -> None:
    """Stop `pid` and everything it started. Best effort; never raises."""
    if not pid or pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        group = os.getpgid(pid)
    except OSError:
        return
    if group == os.getpgrp():
        # Not isolated (never put it in its own group): only stop that one process.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.05)
    if pid_alive(pid):
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
