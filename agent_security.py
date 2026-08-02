"""Consent, authentication, validation, and audit controls for a Windows Agent.
Set ``WINDOWS_AGENT_GATEWAY_TOKEN`` to a random 32+ byte secret before starting
the agent. The gateway must verify the same value in the ``X-Agent-Token``
WebSocket handshake header using ``verify_gateway_token``.
This module authorizes requests only; callers must invoke the protected action
only after ``SecurityController.authorize_request`` returns ``True``. """

from __future__ import annotations

import ctypes
import hmac
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from websockets.asyncio.client import ClientConnection, connect


if os.name == "nt":
    # Windows: dùng %LOCALAPPDATA%\WindowsAgent (ví dụ: C:\Users\user\AppData\Local\WindowsAgent)
    APP_DIRECTORY = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "WindowsAgent"
else:
    # macOS/Linux (môi trường dev): lưu trong thư mục project hoặc theo biến môi trường
    _agent_data_dir = os.environ.get("AGENT_DATA_DIR", "")
    APP_DIRECTORY = Path(_agent_data_dir) if _agent_data_dir else Path(__file__).parent / ".agent_data"


CONSENT_FILE = APP_DIRECTORY / "authorization.json"
AUDIT_LOG = APP_DIRECTORY / "agent_audit.log"
TOKEN_HEADER = "X-Agent-Token"

SENSITIVE_MODULES = frozenset({"screen_capture", "camera_capture", "process_terminate", "power_control", "keylogger"})
SUPPORTED_MODULES = SENSITIVE_MODULES | frozenset({"application_launch"})
CONSENT_REQUIRED_MODULES = SUPPORTED_MODULES

_MB_YESNO = 0x00000004
_MB_ICONWARNING = 0x00000030
_MB_TOPMOST = 0x00040000
_IDYES = 6


class SecurityError(RuntimeError):
    """Raised when a request fails an authentication, consent, or policy check."""


@dataclass(frozen=True, slots=True)
class RemoteRequest:
    """A validated request that an agent integration may dispatch."""

    request_id: str
    module: str
    parameters: dict[str, Any]


def _prepare_storage() -> None:
    """Create the per-user state directory and make it private where possible."""
    APP_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["icacls.exe", str(APP_DIRECTORY), "/inheritance:r", "/grant:r", f"{os.getlogin()}:(OI)(CI)F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _message_box(message: str, title: str) -> bool:
    """Show a blocking Windows Yes/No dialog; fail closed if unavailable."""
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
    """Compare gateway handshake tokens in constant time."""
    return bool(provided_token and expected_token) and hmac.compare_digest(provided_token, expected_token)


def validate_gateway_url(gateway_url: str) -> str:
    """Allow only an unencrypted loopback WebSocket gateway."""
    parsed = urlparse(gateway_url)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise SecurityError("Gateway must use ws:// on a loopback host")
    if parsed.username or parsed.password or parsed.fragment:
        raise SecurityError("Gateway URL contains unsupported components")
    return gateway_url


async def connect_trusted_gateway(gateway_url: str, token: str) -> ClientConnection:
    """Open an authenticated connection to the trusted local gateway."""
    validate_gateway_url(gateway_url)
    if not isinstance(token, str) or len(token) < 32:
        raise SecurityError("Gateway token must be a secret of at least 32 characters")
    return await connect(gateway_url, additional_headers={TOKEN_HEADER: token}, open_timeout=10)


class SecurityController:
    """Central authorization gate to call before dispatching a remote action."""

    def __init__(self, allowed_applications: Mapping[str, str | os.PathLike[str]] | None = None) -> None:
        self._allowed_applications = {
            name: Path(path).resolve() for name, path in (allowed_applications or {}).items()
        }
        # Only validate executable paths on Windows – dev machines (macOS/Linux) skip this check
        if os.name == "nt" and any(not name or not path.is_file() for name, path in self._allowed_applications.items()):
            raise ValueError("Each allowed application needs a non-empty name and existing executable path")
        _prepare_storage()

    def ensure_initial_consent(self) -> bool:
        stored_consent = self._read_consent_state()
        if stored_consent is not None:
            return stored_consent
        approved = _message_box(
            "Allow this Windows Agent to operate as a managed terminal?\n\n"
            "It will request approval before sensitive actions.",
            "Windows Agent setup",
        )
        self._write_consent_state(approved)
        self._audit("setup", "initial-consent", "approved" if approved else "denied")
        return approved

    def authorize_request(self, raw_request: Mapping[str, Any]) -> RemoteRequest | None:
        request_id = str(raw_request.get("request_id", "unknown"))[:128]
        module = str(raw_request.get("module", "unknown"))[:64]
        try:
            if not self.ensure_initial_consent():
                self._audit(request_id, module, "denied: setup consent missing")
                return None
            request = self._validate_request(raw_request)
            if request.module in CONSENT_REQUIRED_MODULES:
                approved = _message_box(
                    f"A remote request wants to access {request.module.replace('_', ' ').title()}. Do you allow this?",
                    "Windows Agent permission request",
                )
                decision = "approved" if approved else "denied"
                self._audit(request.request_id, request.module, decision)
                return request if approved else None
        except (TypeError, ValueError, SecurityError) as exc:
            self._audit(request_id, module, f"rejected: {type(exc).__name__}")
            raise SecurityError("Remote request failed security validation") from exc

    def _validate_request(self, raw_request: Mapping[str, Any]) -> RemoteRequest:
        if not isinstance(raw_request, Mapping):
            raise SecurityError("Request must be an object")
        request_id, module, parameters = raw_request.get("request_id"), raw_request.get("module"), raw_request.get("parameters", {})
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise SecurityError("Invalid request_id")
        
        if module not in SUPPORTED_MODULES or not isinstance(parameters, Mapping):
            raise SecurityError("Unsupported module or invalid parameters")
            
        clean_parameters = dict(parameters)
        if module == "screen_capture" and clean_parameters:
            raise SecurityError("Screen capture accepts no parameters")
        elif module == "camera_capture":
            index = clean_parameters.get("camera_index", 0)
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 9:
                raise SecurityError("Invalid camera index")
            clean_parameters = {"camera_index": index}
        elif module == "keylogger":
            # Cho phép cấu hình action tùy chọn (start/stop/get)
            action = clean_parameters.get("action", "start")
            if action not in {"start", "stop", "get"}:
                raise SecurityError("Invalid keylogger action")
            clean_parameters = {"action": action}
        elif module == "process_terminate":
            pid = clean_parameters.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or set(clean_parameters) != {"pid"}:
                raise SecurityError("Process termination requires one positive PID")
        elif module == "power_control":
            action = clean_parameters.get("action")
            if action not in {"lock", "sleep", "restart", "shutdown"} or set(clean_parameters) != {"action"}:
                raise SecurityError("Invalid power action")
        elif module == "application_launch":
            application = clean_parameters.get("application")
            if not isinstance(application, str) or application not in self._allowed_applications or set(clean_parameters) != {"application"}:
                raise SecurityError("Application is not allowlisted")
            clean_parameters = {"executable": str(self._allowed_applications[application])}
            
        return RemoteRequest(request_id=request_id, module=module, parameters=clean_parameters)

    def _read_consent_state(self) -> bool | None:
        try:
            content = json.loads(CONSENT_FILE.read_text(encoding="utf-8"))
            return content.get("managed_terminal_consent") if isinstance(content.get("managed_terminal_consent"), bool) else None
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None

    def _write_consent_state(self, approved: bool) -> None:
        temporary = CONSENT_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"managed_terminal_consent": approved, "updated_at": _utc_timestamp()}), encoding="utf-8")
        temporary.replace(CONSENT_FILE)

    def _audit(self, request_id: str, module: str, decision: str) -> None:
        entry = {"timestamp": _utc_timestamp(), "request_id": request_id, "module": module, "decision": decision}
        with AUDIT_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, separators=(",", ":")) + "\n")