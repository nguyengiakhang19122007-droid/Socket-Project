"""Utilities for listing, launching, and safely stopping Windows processes and Whitelisted Applications.
Install the dependency with: pip install psutil
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, Mapping

import psutil


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """A snapshot of a running process and its resource consumption."""

    pid: int
    name: str
    cpu_percent: float
    ram_mb: float  
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
    """Return all accessible running processes with CPU percentage and RAM usage in MB."""
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
            mem_info = process.memory_info()
            ram_mb = round(mem_info.rss / (1024 * 1024), 2) 
            
            results.append(
                ProcessInfo(
                    pid=pid,
                    name=process.name() or "<unnamed>",
                    cpu_percent=round(process.cpu_percent(interval=None), 2),
                    ram_mb=ram_mb,
                    is_gui_application=pid in gui_pids,
                )
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
            
    return sorted(results, key=lambda item: (not item.is_gui_application, item.name.lower(), item.pid))


def list_applications(allowed_applications: Mapping[str, Path]) -> list[dict]:
    """Quản lý Module Application: Liệt kê trạng thái (đang chạy/không) và % CPU của các app Whitelist."""
    running_processes = list(psutil.process_iter(["pid", "name", "exe"]))
    for proc in running_processes:
        try:
            proc.cpu_percent(interval=None)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
            
    time.sleep(0.05)
    app_list = []

    for app_name, app_path in allowed_applications.items():
        app_path_resolved = str(Path(app_path).resolve()).lower()
        matched_pids = []
        total_cpu = 0.0

        for proc in running_processes:
            try:
                p_exe = proc.info.get("exe")
                if p_exe and str(Path(p_exe).resolve()).lower() == app_path_resolved:
                    matched_pids.append(proc.pid)
                    total_cpu += proc.cpu_percent(interval=None)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue

        app_list.append({
            "name": app_name,
            "path": str(app_path),
            "is_running": len(matched_pids) > 0,
            "pids": matched_pids,
            "cpu_percent": round(total_cpu, 2)
        })

    return app_list


def launch_application(command: str | os.PathLike[str] | Sequence[str]) -> subprocess.Popen[str]:
    """Khởi chạy ứng dụng nằm trong Whitelist."""
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


def stop_application_by_name(app_name: str, allowed_applications: Mapping[str, Path]) -> int:
    """Tắt ứng dụng nằm trong Whitelist theo tên cấu hình."""
    if app_name not in allowed_applications:
        raise PermissionError(f"Application '{app_name}' is not in the whitelist.")

    target_path = str(Path(allowed_applications[app_name]).resolve()).lower()
    stopped_count = 0

    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            p_exe = proc.info.get("exe")
            if p_exe and str(Path(p_exe).resolve()).lower() == target_path:
                proc.terminate()
                stopped_count += 1
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue

    return stopped_count


def stop_process(pid: int, timeout: float = 5.0) -> bool:
    """Kill một tiến trình bất kỳ dựa trên PID."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if pid == os.getpid():
        raise ValueError("refusing to stop the current process")

    try:
        process = psutil.Process(pid)
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
        return True
    except psutil.NoSuchProcess:
        return False