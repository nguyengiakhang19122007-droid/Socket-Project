"""Windows system metrics and power-control helpers.
Power-changing operations require ``confirm=True`` so callers must explicitly
opt in before invoking a disruptive system command. """

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from datetime import timedelta


class WindowsSystemError(RuntimeError):
    """Raised when a Windows API or system command cannot be completed."""


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    """Current machine memory use and elapsed time since the last boot."""

    ram_usage_percent: float
    uptime: timedelta


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _require_windows() -> None:
    """Fail clearly rather than issuing platform-specific calls elsewhere."""
    if os.name != "nt":
        raise OSError("windows_system_control can only run on Windows")


def _require_confirmation(confirm: bool) -> None:
    if confirm is not True:
        raise PermissionError("Power operation cancelled: call with confirm=True to proceed")


def get_system_metrics() -> SystemMetrics:
    """Return current physical RAM usage percentage and Windows system uptime."""
    _require_windows()
    memory_status = _MemoryStatusEx()
    memory_status.dwLength = ctypes.sizeof(memory_status)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
        raise WindowsSystemError(f"GlobalMemoryStatusEx failed (WinError {ctypes.get_last_error()})")

    uptime_ms = kernel32.GetTickCount64()
    return SystemMetrics(
        ram_usage_percent=float(memory_status.dwMemoryLoad),
        uptime=timedelta(milliseconds=uptime_ms),
    )


def lock_workstation() -> None:
    """Immediately lock the current interactive Windows session."""
    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.LockWorkStation():
        raise WindowsSystemError(f"LockWorkStation failed (WinError {ctypes.get_last_error()})")


def sleep_system(*, confirm: bool = False) -> None:
    """Put the computer to sleep after explicit confirmation.

    ``confirm`` must be exactly ``True``. The call may fail when the system's
    power policy, hardware, or current privileges do not permit sleep.
    """
    _require_windows()
    _require_confirmation(confirm)
    powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    set_suspend_state = powrprof.SetSuspendState
    set_suspend_state.argtypes = [wintypes.BOOL, wintypes.BOOL, wintypes.BOOL]
    set_suspend_state.restype = wintypes.BOOL
    # hibernate=False, force=False, wakeup-events disabled.
    if not set_suspend_state(False, False, False):
        raise WindowsSystemError(f"SetSuspendState failed (WinError {ctypes.get_last_error()})")


def restart_system(*, delay_seconds: int = 0, force: bool = False, confirm: bool = False) -> None:

    """Schedule a Windows restart after explicit confirmation.
    ``delay_seconds`` must be a non-negative integer. Set ``force=True`` only
    when it is acceptable for Windows to close applications without asking. """
    _run_shutdown_command(restart=True, delay_seconds=delay_seconds, force=force, confirm=confirm)


def shutdown_system(*, delay_seconds: int = 0, force: bool = False, confirm: bool = False) -> None:

    """Schedule a Windows shutdown after explicit confirmation.
    ``delay_seconds`` must be a non-negative integer. Set ``force=True`` only
    when it is acceptable for Windows to close applications without asking. """

    _run_shutdown_command(restart=False, delay_seconds=delay_seconds, force=force, confirm=confirm)


def _run_shutdown_command(*, restart: bool, delay_seconds: int, force: bool, confirm: bool) -> None:
    """Validate and run Windows' shutdown utility without a command shell."""
    _require_windows()
    _require_confirmation(confirm)
    if not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool) or delay_seconds < 0:
        raise ValueError("delay_seconds must be a non-negative integer")

    command = ["shutdown.exe", "/r" if restart else "/s", "/t", str(delay_seconds)]
    if force:
        command.append("/f")
    try:
        subprocess.run(command, check=True, shell=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise WindowsSystemError("shutdown.exe was not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise WindowsSystemError(f"Windows power command failed: {detail}") from exc

