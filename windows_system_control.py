"""Windows system metrics and power-control helpers."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from datetime import timedelta

import psutil


class WindowsSystemError(RuntimeError):
    """Raised when a Windows API or system command cannot be completed."""


_POWER_SCHEME = "SCHEME_CURRENT"
_SLEEP_SUBGROUP = "SUB_SLEEP"
_HYBRID_SLEEP_SETTING = "HYBRIDSLEEP"
_POWER_INDEX_PATTERN = re.compile(r"0x([0-9a-fA-F]{8})")


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    """Current machine status including System Uptime (elapsed usage time), CPU temp, and RAM %."""

    uptime_str: str  # Thời gian máy tính đã sử dụng/hoạt động
    cpu_temperature: float | str
    ram_usage_percent: float
    cpu_usage_percent: float  # % CPU toàn hệ thống (tổng các lõi, trung bình)
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
    if os.name != "nt":
        raise OSError("windows_system_control can only run on Windows")


def _require_confirmation(confirm: bool) -> None:
    if confirm is not True:
        raise PermissionError("Power operation cancelled: call with confirm=True to proceed")


def _get_cpu_temperature() -> float | str:
    """Lấy nhiệt độ CPU trên Windows qua WMI / Sensor."""
    try:
        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for name, entries in temps.items():
                    if entries:
                        return round(entries[0].current, 1)
        
        # Fallback qua PowerShell WMI
        cmd = "powershell -NoProfile -Command \"(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue).CurrentTemperature\""
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2, shell=True)
        if res.returncode == 0 and res.stdout.strip().isdigit():
            val = float(res.stdout.strip())
            celsius = (val / 10.0) - 273.15
            if 0 <= celsius <= 120:
                return round(celsius, 1)
    except Exception:
        pass
    return "N/A"


def _format_uptime(td: timedelta) -> str:
    """Định dạng timedelta thành chuỗi thời gian sử dụng dễ đọc (ví dụ: '1 ngày, 04:15:20' hoặc '04:15:20')."""
    total_seconds = int(td.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if days > 0:
        return f"{days} ngày, {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def get_system_metrics() -> SystemMetrics:
    """Trả về chỉ số hệ thống: Thời gian máy đã sử dụng (Uptime), Nhiệt độ CPU, % RAM."""
    _require_windows()
    memory_status = _MemoryStatusEx()
    memory_status.dwLength = ctypes.sizeof(memory_status)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
        raise WindowsSystemError(f"GlobalMemoryStatusEx failed (WinError {ctypes.get_last_error()})")

    # Lấy số millisecond máy đã hoạt động kể từ lần khởi động gần nhất
    uptime_ms = kernel32.GetTickCount64()
    uptime_td = timedelta(milliseconds=uptime_ms)
    uptime_formatted = _format_uptime(uptime_td)
    cpu_temp = _get_cpu_temperature()

    # psutil.cpu_percent(interval=0.5) block 0.5s để đo CPU usage thực tế.
    # interval=None (non-blocking) luôn trả 0.0 ở lần gọi đầu tiên nên không dùng.
    cpu_usage = round(psutil.cpu_percent(interval=0.5), 1)

    return SystemMetrics(
        uptime_str=uptime_formatted,
        cpu_temperature=cpu_temp,
        ram_usage_percent=float(memory_status.dwMemoryLoad),
        cpu_usage_percent=cpu_usage,
        uptime=uptime_td,
    )


def lock_workstation() -> None:
    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.LockWorkStation():
        raise WindowsSystemError(f"LockWorkStation failed (WinError {ctypes.get_last_error()})")


def _run_power_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            shell=False,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        raise WindowsSystemError(f"Windows power command failed: {details}") from exc
    except OSError as exc:
        raise WindowsSystemError(f"Windows power command could not start: {exc}") from exc


def _read_hybrid_sleep_values() -> tuple[int, int]:
    result = _run_power_command([
        "powercfg.exe",
        "/query",
        _POWER_SCHEME,
        _SLEEP_SUBGROUP,
        _HYBRID_SLEEP_SETTING,
    ])
    # The labels printed by powercfg are localized, while its AC/DC setting
    # values retain the language-independent 0x00000000 format.
    matches = _POWER_INDEX_PATTERN.findall(result.stdout)
    if len(matches) < 2:
        raise WindowsSystemError(
            "Windows did not expose the AC/DC Hybrid Sleep values. "
            "Run 'powercfg /a' to verify that this computer supports Sleep."
        )
    return int(matches[-2], 16), int(matches[-1], 16)


def _set_hybrid_sleep_values(ac_value: int, dc_value: int) -> None:
    _run_power_command([
        "powercfg.exe", "/setacvalueindex", _POWER_SCHEME,
        _SLEEP_SUBGROUP, _HYBRID_SLEEP_SETTING, str(ac_value),
    ])
    _run_power_command([
        "powercfg.exe", "/setdcvalueindex", _POWER_SCHEME,
        _SLEEP_SUBGROUP, _HYBRID_SLEEP_SETTING, str(dc_value),
    ])
    _run_power_command(["powercfg.exe", "/setactive", _POWER_SCHEME])


def _request_windows_suspend() -> None:
    # PowerState.Suspend = 0. PowerState.Hibernate = 1 and is intentionally
    # never used here.
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$ok = [System.Windows.Forms.Application]::SetSuspendState("
        "[System.Windows.Forms.PowerState]::Suspend, $false, $false); "
        "if (-not $ok) { throw 'Windows rejected the Sleep request.' }"
    )
    _run_power_command([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script,
    ])


def sleep_system(*, confirm: bool = False) -> None:
    """Enter pure Sleep/Standby while preserving the user's power plan."""
    _require_windows()
    _require_confirmation(confirm)
    original_ac, original_dc = _read_hybrid_sleep_values()
    try:
        # Index 0 means Hybrid Sleep is disabled. Apply and read it back before
        # suspend so Windows cannot silently use the S3 + hiberfile hybrid path.
        _set_hybrid_sleep_values(0, 0)
        if _read_hybrid_sleep_values() != (0, 0):
            raise WindowsSystemError(
                "Windows policy kept Hybrid Sleep enabled; pure Sleep was cancelled."
            )
        _request_windows_suspend()
    finally:
        # SetSuspendState returns after resume; restore the exact AC/DC values
        # that were active before this request.
        _set_hybrid_sleep_values(original_ac, original_dc)


def restart_system(*, delay_seconds: int = 0, force: bool = False, confirm: bool = False) -> None:
    _run_shutdown_command(restart=True, delay_seconds=delay_seconds, force=force, confirm=confirm)


def shutdown_system(*, delay_seconds: int = 0, force: bool = False, confirm: bool = False) -> None:
    _run_shutdown_command(restart=False, delay_seconds=delay_seconds, force=force, confirm=confirm)


def _run_shutdown_command(*, restart: bool, delay_seconds: int, force: bool, confirm: bool) -> None:
    _require_windows()
    _require_confirmation(confirm)
    command = ["shutdown.exe", "/r" if restart else "/s", "/t", str(delay_seconds)]
    if force:
        command.append("/f")
    try:
        subprocess.run(command, check=True, shell=False, capture_output=True, text=True)
    except Exception as exc:
        raise WindowsSystemError(f"Windows power command failed: {exc}") from exc
