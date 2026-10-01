"""Process helpers: isolate child processes and reliably stop a whole process tree.

On Windows `os.kill(pid, sig)` calls TerminateProcess (even for signal 0), so liveness
checks and tree kills go through ctypes / taskkill there instead.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


def popen_isolation_kwargs() -> dict[str, object]:
    """Start a child in its own process group/session so its whole tree can be stopped."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    pid = int(pid)
    if os.name == "nt":
        return _windows_pid_alive(pid)
    # A finished child stays a zombie until its parent reaps it, and kill(pid, 0) still
    # succeeds for zombies. Reap our own children here; check /proc for anyone else's.
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except OSError:
        waited = 0  # not our child
    if waited == pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return False  # no /proc (e.g. macOS): fall back to "alive"
    # The state letter follows the last ")" because the command name may contain anything.
    return data[data.rfind(b")") + 2 : data.rfind(b")") + 3] == b"Z"


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


def _parent_map() -> dict[int, list[int]]:
    """parent pid -> child pids, from /proc where available, else `ps`."""
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                data = (entry / "stat").read_bytes()
                ppid = int(data[data.rfind(b")") + 2 :].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(ppid, []).append(int(entry.name))
        if children:
            return children
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=False).stdout
    except OSError:
        return children
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return children


def descendants(pid: int) -> list[int]:
    children = _parent_map()
    found: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def kill_tree(pid: int, grace: float = 3.0) -> None:
    """Stop `pid` and everything it started. Best effort; never raises.

    A child may run in its own session (agents do, so a timeout can stop them), so the
    process group alone is not enough: snapshot the descendants first, because they are
    re-parented once their parent dies.
    """
    if not pid or pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    victims = [pid, *descendants(pid)]
    groups: set[int] = set()
    for victim in victims:
        try:
            group = os.getpgid(victim)
        except OSError:
            continue
        if group != os.getpgrp():  # never signal our own group
            groups.add(group)
    _signal_all(victims, groups, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(pid_alive(victim) for victim in victims):
        time.sleep(0.05)
    survivors = [victim for victim in victims if pid_alive(victim)]
    if survivors:
        _signal_all(survivors, groups, signal.SIGKILL)


def _signal_all(pids: list[int], groups: set[int], sig: int) -> None:
    for group in groups:
        try:
            os.killpg(group, sig)
        except OSError:
            pass
    for victim in pids:
        if victim == os.getpid():
            continue
        try:
            os.kill(victim, sig)
        except OSError:
            pass
