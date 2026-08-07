"""Consent, authentication, validation, and audit controls for a Windows Agent."""

from __future__ import annotations

import ctypes
import hmac
import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from websockets.asyncio.client import ClientConnection, connect


APP_DIRECTORY = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WindowsAgent"
CONSENT_FILE = APP_DIRECTORY / "authorization.json"
AUDIT_LOG = APP_DIRECTORY / "agent_audit.log"
PERMISSIONS_FILE = APP_DIRECTORY / "feature_permissions.json"
TOKEN_HEADER = "X-Agent-Token"

# Module CHỈ ĐỌC: Được duyệt tự động để phục vụ cơ chế Refresh 5s không gây phiền
READONLY_MODULES = frozenset({"system_metrics", "list_processes", "application_list", "file_list"})

# Module NHẠY CẢM: Phải hiện Popup xin quyền (Consent Dialog)
# file_read được xếp vào nhóm nhạy cảm vì có thể dùng để tải file về máy điều khiển
SENSITIVE_MODULES = frozenset({
    "screen_capture", "screen_stream", "camera_capture", "camera_stream",
    "process_terminate", "power_control", "keylogger",
    "file_read", "file_write", "file_delete", "application_launch", "application_stop"
})

SUPPORTED_MODULES = READONLY_MODULES | SENSITIVE_MODULES
CONSENT_REQUIRED_MODULES = SENSITIVE_MODULES

# Quyền được quản lý theo nhóm tính năng để người dùng không phải bật/tắt
# từng lệnh nội bộ. Mặc định giữ tương thích ngược: mọi nhóm đều được bật.
FEATURE_MODULES: dict[str, frozenset[str]] = {
    "applications": frozenset({"application_list", "application_launch", "application_stop"}),
    "processes": frozenset({"list_processes", "process_terminate"}),
    "screen": frozenset({"screen_capture", "screen_stream"}),
    "webcam": frozenset({"camera_capture", "camera_stream"}),
    "keylogger": frozenset({"keylogger"}),
    "files": frozenset({"file_list", "file_read", "file_write", "file_delete"}),
    "power_system": frozenset({"system_metrics", "power_control"}),
}
FEATURE_LABELS = {
    "applications": "Applications",
    "processes": "Processes",
    "screen": "Screen capture",
    "webcam": "Webcam",
    "keylogger": "Keyboard capture",
    "files": "Sandbox files",
    "power_system": "Power & system",
}
MODULE_FEATURE = {
    module: feature
    for feature, modules in FEATURE_MODULES.items()
    for module in modules
}

_MB_YESNO = 0x00000004
_MB_ICONWARNING = 0x00000030
_MB_TOPMOST = 0x00040000
_IDYES = 6


class SecurityError(RuntimeError):
    """Raised when a request fails an authentication, consent, or policy check."""


@dataclass(frozen=True, slots=True)
class RemoteRequest:
    request_id: str
    module: str
    parameters: dict[str, Any]


def _prepare_storage() -> None:
    APP_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)


def _atomic_write_json(destination: Path, payload: Mapping[str, Any], *, indent: int | None = None) -> None:
    """Atomically write JSON without sharing one predictable temporary path."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{destination.stem}-",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            descriptor = None
            json.dump(payload, temporary_file, indent=indent)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except PermissionError as exc:
        raise SecurityError(
            f"Cannot save Agent state in {destination.parent}. "
            "Close duplicate Agent/Gateway processes and check that this folder is writable."
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _message_box(message: str, title: str) -> bool:
    if os.name != "nt":
        return False
    try:
        result = ctypes.windll.user32.MessageBoxW(None, message, title, _MB_YESNO | _MB_ICONWARNING | _MB_TOPMOST)
        return result == _IDYES
    except (AttributeError, OSError):
        return False


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def verify_gateway_token(provided_token: str | None, expected_token: str) -> bool:
    return bool(provided_token and expected_token) and hmac.compare_digest(provided_token, expected_token)


def validate_gateway_url(gateway_url: str) -> str:
    parsed = urlparse(gateway_url)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise SecurityError("Gateway must use ws:// on a loopback host")
    return gateway_url


class SecurityController:
    """Central authorization gate."""

    def __init__(self, allowed_applications: Mapping[str, str | os.PathLike[str]] | None = None) -> None:
        self._allowed_applications = {
            name: Path(path).resolve() for name, path in (allowed_applications or {}).items()
        }
        self._permission_lock = threading.RLock()
        _prepare_storage()
        self._permissions = self._read_permissions()

    @property
    def allowed_applications(self) -> dict[str, Path]:
        return self._allowed_applications

    def ensure_initial_consent(self) -> bool:
        stored_consent = self._read_consent_state()
        if stored_consent is not None:
            return stored_consent
        approved = _message_box(
            "Allow this Windows Agent to operate as a managed terminal?\n\nIt will request approval before sensitive actions.",
            "Windows Agent setup",
        )
        self._write_consent_state(approved)
        return approved

    def authorize_request(self, raw_request: Mapping[str, Any]) -> RemoteRequest | None:
        request_id = str(raw_request.get("request_id", "unknown"))[:128]
        module = str(raw_request.get("module", "unknown"))[:64]
        try:
            if not self.ensure_initial_consent():
                return None
            request = self._validate_request(raw_request)

            # Lệnh dừng luôn được phép để một tính năng có thể được cleanup sau
            # khi quyền đã bị thu hồi. Mọi thao tác khác phải qua policy động.
            is_stop = request.parameters.get("action") == "stop"
            if not is_stop and not self.is_module_allowed(request.module):
                self._audit(request.request_id, request.module, "denied: feature disabled")
                return None
            
            # Nếu thuộc nhóm Sensitive -> Hiện Popup xin quyền Consent Dialog
            if request.module in CONSENT_REQUIRED_MODULES:
                approved = _message_box(
                    f"A remote request wants to access {request.module.replace('_', ' ').title()}. Do you allow this?",
                    "Windows Agent Permission Request",
                )
                decision = "approved" if approved else "denied"
                self._audit(request.request_id, request.module, decision)
                return request if approved else None
            
            # Module Read-only tự động chấp thuận phục vụ Polling 5s
            self._audit(request.request_id, request.module, "approved: read-only")
            return request

        except Exception as exc:
            self._audit(request_id, module, f"rejected: {type(exc).__name__}")
            raise SecurityError("Remote request failed security validation") from exc

    def permission_snapshot(self) -> dict[str, dict[str, Any]]:
        """Trả về bản sao policy an toàn để gửi cho Web App."""
        with self._permission_lock:
            return {
                feature: {
                    "enabled": bool(self._permissions.get(feature, True)),
                    "label": FEATURE_LABELS[feature],
                    "modules": sorted(FEATURE_MODULES[feature]),
                }
                for feature in FEATURE_MODULES
            }

    def is_module_allowed(self, module: str) -> bool:
        feature = MODULE_FEATURE.get(module)
        if feature is None:
            return False
        with self._permission_lock:
            return bool(self._permissions.get(feature, True))

    def set_feature_permission(self, feature: str, enabled: bool) -> dict[str, dict[str, Any]]:
        return self.set_feature_permissions({feature: enabled})

    def set_feature_permissions(self, updates: Mapping[str, bool]) -> dict[str, dict[str, Any]]:
        """Cập nhật một hoặc nhiều quyền trong một lần ghi file nguyên tử."""
        if not updates:
            raise SecurityError("At least one permission update is required")
        for feature, enabled in updates.items():
            if feature not in FEATURE_MODULES:
                raise SecurityError(f"Unknown feature: {feature}")
            if type(enabled) is not bool:
                raise SecurityError("enabled must be a boolean")
        with self._permission_lock:
            self._permissions.update(updates)
            self._write_permissions()
        for feature, enabled in updates.items():
            self._audit("local-policy", feature, "enabled" if enabled else "revoked")
        return self.permission_snapshot()

    def _validate_request(self, raw_request: Mapping[str, Any]) -> RemoteRequest:
        request_id = raw_request.get("request_id")
        module = raw_request.get("module")
        parameters = raw_request.get("parameters", {})

        if module not in SUPPORTED_MODULES:
            raise SecurityError("Unsupported module")

        clean_params = dict(parameters)
        if module == "application_launch":
            app = clean_params.get("application")
            if app not in self._allowed_applications:
                raise SecurityError("Application not in whitelist")
            clean_params = {"executable": str(self._allowed_applications[app])}
        elif module == "application_stop":
            app = clean_params.get("application")
            if app not in self._allowed_applications:
                raise SecurityError("Application not in whitelist")
            clean_params = {"application": app}

        return RemoteRequest(request_id=str(request_id), module=str(module), parameters=clean_params)

    def _read_consent_state(self) -> bool | None:
        try:
            content = json.loads(CONSENT_FILE.read_text(encoding="utf-8"))
            return content.get("managed_terminal_consent")
        except Exception:
            return None

    def _write_consent_state(self, approved: bool) -> None:
        _atomic_write_json(
            CONSENT_FILE,
            {"managed_terminal_consent": approved, "updated_at": _utc_timestamp()},
        )

    def _read_permissions(self) -> dict[str, bool]:
        defaults = {feature: True for feature in FEATURE_MODULES}
        try:
            content = json.loads(PERMISSIONS_FILE.read_text(encoding="utf-8"))
            stored = content.get("permissions", {})
            if isinstance(stored, dict):
                for feature in defaults:
                    if type(stored.get(feature)) is bool:
                        defaults[feature] = stored[feature]
        except Exception:
            pass
        return defaults

    def _write_permissions(self) -> None:
        payload = {
            "version": 1,
            "permissions": self._permissions,
            "updated_at": _utc_timestamp(),
        }
        _atomic_write_json(PERMISSIONS_FILE, payload, indent=2)

    def _audit(self, request_id: str, module: str, decision: str) -> None:
        entry = {"timestamp": _utc_timestamp(), "request_id": request_id, "module": module, "decision": decision}
        with AUDIT_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, separators=(",", ":")) + "\n")
