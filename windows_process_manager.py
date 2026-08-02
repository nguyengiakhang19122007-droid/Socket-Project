"""Utilities for listing, launching, and safely stopping Windows processes.

Install the dependency with: pip install psutil
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import psutil


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """A snapshot of a running process and its resource consumption."""

    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    is_gui_application: bool


def _visible_window_pids() -> set[int]:
    """Return PIDs owning a main window, or an empty set if querying fails."""
    script = "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -ExpandProperty Id"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return set()
        return {int(value) for value in result.stdout.split() if value.isdigit()}
    except (OSError, subprocess.SubprocessError):
        return set()


def list_processes(include_gui_status: bool = True) -> list[ProcessInfo]:

    """Return all accessible running processes with CPU and memory percentages.
    CPU usage is sampled across a short interval so it is meaningful rather than
    the initial always-zero psutil value.  ``is_gui_application`` identifies
    processes that own a main window when ``include_gui_status`` is true.
    Access-denied and already-exited processes are skipped. """

    processes = list(psutil.process_iter(["pid", "name"]))
    for process in processes:
        try:
            process.cpu_percent(interval=None)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue

    time.sleep(0.1)
    gui_pids = _visible_window_pids() if include_gui_status else set()
    results: list[ProcessInfo] = []
    for process in processes:
        try:
            pid = process.pid
            results.append(
                ProcessInfo(
                    pid=pid,
                    name=process.name() or "<unnamed>",
                    cpu_percent=round(process.cpu_percent(interval=None), 2),
                    memory_percent=round(process.memory_percent(), 2),
                    is_gui_application=pid in gui_pids,
                )
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return sorted(results, key=lambda item: (not item.is_gui_application, item.name.lower(), item.pid))


def launch_application(command: str | os.PathLike[str] | Sequence[str]) -> subprocess.Popen[str]:
    """Launch an executable or command and return its ``Popen`` handle.

    Pass an executable path, a Windows command string, or a sequence such as
    ``[r"C:\\Program Files\\App\\app.exe", "--flag"]``. ``shell=False`` is
    always used to avoid command-shell injection.

    Raises:
        FileNotFoundError: The requested executable cannot be found.
        PermissionError: Windows refused permission to launch it.
        OSError: The operating system could not create the process.
    """
    if isinstance(command, os.PathLike):
        executable = Path(command)
        if not executable.is_file():
            raise FileNotFoundError(f"Executable does not exist: {executable}")
        args: str | list[str] = [str(executable)]
    elif isinstance(command, str):
        if not command.strip():
            raise ValueError("command must not be empty")
        args = command
    else:
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("command must be a non-empty executable path or sequence of non-empty strings")
        args = list(command)

    return subprocess.Popen(args, shell=False, text=True)


def stop_process(pid: int, timeout: float = 5.0) -> bool:
    """Stop a process gracefully, then kill it if it does not exit in time.

    Returns ``True`` when a process was stopped. Returns ``False`` if the PID was
    already absent. Raises ``ValueError`` for unsafe PIDs and ``psutil.AccessDenied``
    when the current user lacks permission to control the process.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if pid == os.getpid():
        raise ValueError("refusing to stop the current process")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False

    try:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
        return True
    except psutil.NoSuchProcess:
        return True
