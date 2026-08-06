"""
Luồng hoạt động:
  1. Gateway khởi động, lắng nghe trên cổng 8765 (LAN).
  2. Web App kết nối và gửi token để xác thực (handshake).
  3. Sau khi được xác thực, Web App có thể gửi lệnh JSON.
  4. Gateway định tuyến lệnh đến AgentOrchestrator.
  5. Kết quả (bao gồm ảnh base64) được gửi lại cho Web App.
  6. Các streaming (Screen/Webcam) chạy trong task riêng song song.

Chạy bằng: python gateway_server.py
Hoặc:       python gateway_server.py --host 0.0.0.0 --port 8765

Cấu hình qua biến môi trường:
  GATEWAY_HOST         Địa chỉ bind (mặc định: 0.0.0.0 – toàn bộ LAN)
  GATEWAY_PORT         Cổng lắng nghe (mặc định: 8765)
  GATEWAY_TOKEN        Token bí mật dùng để xác thực Web App
  SCREEN_FPS           Số frame/giây cho livestream màn hình (mặc định: 60)
  WEBCAM_FPS           Số frame/giây cho livestream webcam (mặc định: 60)
  SCREEN_JPEG_QUALITY  Chất lượng JPEG màn hình, 1-100 (mặc định: 65)
  SCREEN_MAX_WIDTH     Chiều rộng màn hình tối đa, 0 giữ nguyên (mặc định: 1280)
  WEBCAM_JPEG_QUALITY  Chất lượng JPEG webcam, 1-100 (mặc định: 70)
  WEBCAM_MAX_WIDTH     Chiều rộng webcam tối đa, 0 giữ nguyên (mặc định: 1280)
  LOG_LEVEL            Mức log: DEBUG / INFO / WARNING (mặc định: INFO)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import secrets
import time
import uuid
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

# Import AgentOrchestrator từ agent_main
from agent_main import AgentOrchestrator
from agent_security import verify_gateway_token
from desktop_capture_utils import (
    CameraStreamCapture,
    ScreenStreamCapture,
)
from local_permission_control import LocalPermissionControl

# ──────────────────────────────────────────────────────────────────────────────
# Cấu hình mặc định
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("GATEWAY_PORT", "8765"))

# Sinh một lần khi process gateway khởi động.
DEFAULT_TOKEN = secrets.token_hex(32)

SCREEN_FPS = float(os.getenv("SCREEN_FPS", "60"))
WEBCAM_FPS = float(os.getenv("WEBCAM_FPS", "60"))
SCREEN_JPEG_QUALITY = int(os.getenv("SCREEN_JPEG_QUALITY", "65"))
SCREEN_MAX_WIDTH = int(os.getenv("SCREEN_MAX_WIDTH", "1280"))
WEBCAM_JPEG_QUALITY = int(os.getenv("WEBCAM_JPEG_QUALITY", "70"))
WEBCAM_MAX_WIDTH = int(os.getenv("WEBCAM_MAX_WIDTH", "1280"))

logger = logging.getLogger("gateway")

# Ứng dụng được phép khởi chạy từ xa.
# Đặt biến môi trường GATEWAY_ALLOWED_APPS với JSON object, ví dụ:
#   GATEWAY_ALLOWED_APPS='{"notepad": "C:\\Windows\\System32\\notepad.exe"}'
# Nếu không đặt, mặc định dùng notepad và calc (chỉ hoạt động trên Windows).
_DEFAULT_ALLOWED_APPS_WINDOWS = {
    "notepad": r"C:\Windows\System32\notepad.exe",
    "calc": r"C:\Windows\System32\calc.exe",
}


def _load_allowed_applications() -> dict[str, str]:
    """Đọc allowed_applications từ biến môi trường GATEWAY_ALLOWED_APPS (JSON).
    Nếu không có, dùng giá trị mặc định cho Windows.
    Trên macOS/Linux (môi trường dev), trả về dict rỗng để tránh lỗi validate path.
    """
    raw = os.getenv("GATEWAY_ALLOWED_APPS", "")
    if raw:
        try:
            apps = json.loads(raw)
            if isinstance(apps, dict):
                return {str(k): str(v) for k, v in apps.items()}
        except json.JSONDecodeError:
            logger.warning("GATEWAY_ALLOWED_APPS is not valid JSON, using defaults.")
    # Trên Windows dùng default; trên macOS/Linux bỏ qua để không crash khi dev
    if os.name == "nt":
        return _DEFAULT_ALLOWED_APPS_WINDOWS
    return {}



# ──────────────────────────────────────────────────────────────────────────────
# Trạng thái toàn cục của Gateway
# ──────────────────────────────────────────────────────────────────────────────


class GatewayState:
    """Quản lý trạng thái toàn cục: client đã xác thực và các task streaming."""

    def __init__(self, token: str, orchestrator: AgentOrchestrator) -> None:
        self.token = token
        self.orchestrator = orchestrator

        # Lưu client đã xác thực: websocket → client_id
        self._authenticated_clients: dict[ServerConnection, str] = {}

        # Streaming tasks: websocket → asyncio.Task (để có thể cancel)
        self._screen_tasks: dict[ServerConnection, asyncio.Task[None]] = {}
        self._webcam_tasks: dict[ServerConnection, asyncio.Task[None]] = {}

        self._lock = asyncio.Lock()

    # ── Authentication ────────────────────────────────────────────────────────

    def is_authenticated(self, websocket: ServerConnection) -> bool:
        return websocket in self._authenticated_clients

    async def authenticate(self, websocket: ServerConnection, provided_token: str) -> bool:
        """Kiểm tra token và đăng ký client nếu hợp lệ. Trả về True nếu xác thực thành công."""
        async with self._lock:
            if verify_gateway_token(provided_token, self.token):
                client_id = str(uuid.uuid4())[:8]
                self._authenticated_clients[websocket] = client_id
                logger.info("Client authenticated: id=%s remote=%s", client_id, websocket.remote_address)
                return True
            return False

    async def remove_client(self, websocket: ServerConnection) -> None:
        """Hủy mọi streaming task và gỡ client khỏi danh sách."""
        async with self._lock:
            await self._cancel_stream_task(self._screen_tasks, websocket, "screen")
            await self._cancel_stream_task(self._webcam_tasks, websocket, "webcam")
            if not self._screen_tasks:
                self.orchestrator.stop_active_feature("screen")
            if not self._webcam_tasks:
                self.orchestrator.stop_active_feature("webcam")
            client_id = self._authenticated_clients.pop(websocket, "unknown")
            logger.info("Client disconnected: id=%s", client_id)

    def client_id(self, websocket: ServerConnection) -> str:
        return self._authenticated_clients.get(websocket, "unknown")

    # ── Streaming management ──────────────────────────────────────────────────

    async def start_screen_stream(self, websocket: ServerConnection) -> None:
        async with self._lock:
            await self._cancel_stream_task(self._screen_tasks, websocket, "screen")
            task = asyncio.create_task(
                _screen_stream_loop(websocket, SCREEN_FPS),
                name=f"screen-{self.client_id(websocket)}",
            )
            self._screen_tasks[websocket] = task
            self.orchestrator.screen_stream_indicator.start()

    async def stop_screen_stream(self, websocket: ServerConnection) -> None:
        async with self._lock:
            await self._cancel_stream_task(self._screen_tasks, websocket, "screen")
            if not self._screen_tasks:
                self.orchestrator.stop_active_feature("screen")

    async def start_webcam_stream(self, websocket: ServerConnection, camera_index: int = 0) -> None:
        async with self._lock:
            await self._cancel_stream_task(self._webcam_tasks, websocket, "webcam")
            task = asyncio.create_task(
                _webcam_stream_loop(websocket, camera_index, WEBCAM_FPS),
                name=f"webcam-{self.client_id(websocket)}",
            )
            self._webcam_tasks[websocket] = task
            self.orchestrator.camera_stream_indicator.start()

    async def stop_webcam_stream(self, websocket: ServerConnection) -> None:
        async with self._lock:
            await self._cancel_stream_task(self._webcam_tasks, websocket, "webcam")
            if not self._webcam_tasks:
                self.orchestrator.stop_active_feature("webcam")

    async def set_feature_permissions(self, updates: dict[str, bool]) -> dict[str, dict[str, Any]]:
        """Cập nhật policy cục bộ và dừng ngay các tài nguyên bị thu hồi."""
        snapshot = self.orchestrator.security.set_feature_permissions(updates)
        revoked = {feature for feature, enabled in updates.items() if not enabled}
        if revoked:
            async with self._lock:
                if "screen" in revoked:
                    for client in list(self._screen_tasks):
                        await self._cancel_stream_task(self._screen_tasks, client, "screen")
                if "webcam" in revoked:
                    for client in list(self._webcam_tasks):
                        await self._cancel_stream_task(self._webcam_tasks, client, "webcam")
                for feature in revoked:
                    self.orchestrator.stop_active_feature(feature)
        return snapshot

    async def apply_local_permission_update(self, updates: dict[str, bool]) -> dict[str, dict[str, Any]]:
        """Điểm vào dành riêng cho cửa sổ native trên máy Agent/Gateway."""
        snapshot = await self.set_feature_permissions(updates)
        await self.broadcast_permission_state(changed_features=sorted(updates))
        logger.warning("Local policy changed: %s", updates)
        return snapshot

    async def broadcast_permission_state(self, changed_features: list[str] | None = None) -> None:
        payload = _build_response(
            msg_type="permission_state",
            data={
                "permissions": self.orchestrator.security.permission_snapshot(),
                "changed_features": changed_features or [],
            },
        )
        clients = list(self._authenticated_clients)
        if clients:
            await asyncio.gather(
                *(client.send(payload) for client in clients),
                return_exceptions=True,
            )

    @staticmethod
    async def _cancel_stream_task(
        task_map: dict[ServerConnection, asyncio.Task[None]],
        websocket: ServerConnection,
        name: str,
    ) -> None:
        task = task_map.pop(websocket, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            logger.debug("Cancelled %s stream", name)


# ──────────────────────────────────────────────────────────────────────────────
# Streaming loops (chạy song song với vòng lặp nhận lệnh)
# ──────────────────────────────────────────────────────────────────────────────


async def _screen_stream_loop(websocket: ServerConnection, fps: float) -> None:
    """Capture JPEG frames using one persistent MSS session."""
    interval = 1.0 / max(fps, 0.5)
    logger.info("Screen stream started (%.1f fps)", fps)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-capture")
    capture: ScreenStreamCapture | None = None
    try:
        capture = await loop.run_in_executor(
            executor, ScreenStreamCapture, SCREEN_JPEG_QUALITY, SCREEN_MAX_WIDTH
        )
        while True:
            t_start = time.monotonic()
            try:
                jpeg_bytes = await loop.run_in_executor(executor, capture.read_jpeg)
                frame_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
                payload = json.dumps({
                    "type": "stream_frame",
                    "module": "screen",
                    "data": {"image_base64": frame_b64, "mime": "image/jpeg"},
                }, separators=(",", ":"))
                await websocket.send(payload)
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception as exc:
                logger.warning("Screen capture error: %s", exc)

            elapsed = time.monotonic() - t_start
            sleep_time = max(0.0, interval - elapsed)
            await asyncio.sleep(sleep_time)
    except asyncio.CancelledError:
        pass
    finally:
        if capture is not None:
            try:
                await loop.run_in_executor(executor, capture.close)
            except Exception:
                logger.debug("Failed to close screen capture", exc_info=True)
        executor.shutdown(wait=False, cancel_futures=True)
        logger.info("Screen stream stopped")


async def _webcam_stream_loop(
    websocket: ServerConnection, camera_index: int, fps: float
) -> None:
    """Capture JPEG frames using one persistent webcam handle."""
    interval = 1.0 / max(fps, 0.5)
    logger.info("Webcam stream started (index=%d, %.1f fps)", camera_index, fps)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="webcam-capture")
    capture: CameraStreamCapture | None = None
    try:
        capture = await loop.run_in_executor(
            executor,
            CameraStreamCapture,
            camera_index,
            fps,
            WEBCAM_JPEG_QUALITY,
            WEBCAM_MAX_WIDTH,
        )
        while True:
            t_start = time.monotonic()
            try:
                jpeg_bytes = await loop.run_in_executor(executor, capture.read_jpeg)
                frame_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
                payload = json.dumps({
                    "type": "stream_frame",
                    "module": "webcam",
                    "data": {"image_base64": frame_b64, "mime": "image/jpeg"},
                }, separators=(",", ":"))
                await websocket.send(payload)
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception as exc:
                logger.warning("Webcam capture error: %s", exc)

            elapsed = time.monotonic() - t_start
            sleep_time = max(0.0, interval - elapsed)
            await asyncio.sleep(sleep_time)
    except asyncio.CancelledError:
        pass
    finally:
        if capture is not None:
            try:
                await loop.run_in_executor(executor, capture.close)
            except Exception:
                logger.debug("Failed to close webcam capture", exc_info=True)
        executor.shutdown(wait=False, cancel_futures=True)
        logger.info("Webcam stream stopped")


# ──────────────────────────────────────────────────────────────────────────────
# Giao thức JSON cho messages từ Web App
# ──────────────────────────────────────────────────────────────────────────────


def _parse_message(raw: str | bytes) -> dict[str, Any]:
    """Parse JSON message, raise ValueError nếu không hợp lệ."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Message must be a JSON object")
    return data


def _build_response(
    *,
    msg_type: str,
    request_id: str | None = None,
    status: str = "ok",
    data: Any = None,
    message: str | None = None,
) -> str:
    """Tạo JSON response chuẩn để gửi về Web App."""
    payload: dict[str, Any] = {"type": msg_type, "status": status}
    if request_id:
        payload["request_id"] = request_id
    if data is not None:
        payload["data"] = data
    if message:
        payload["message"] = message
    return json.dumps(payload)


# ──────────────────────────────────────────────────────────────────────────────
# Handler chính cho mỗi WebSocket connection
# ──────────────────────────────────────────────────────────────────────────────


async def _handle_client(websocket: ServerConnection, state: GatewayState) -> None:
    """Xử lý toàn bộ vòng đời của một WebSocket connection từ Web App."""
    remote = websocket.remote_address
    logger.info("New connection from %s", remote)

    # ── Bước 1: Handshake xác thực ────────────────────────────────────────────
    # Bước đầu tiên PHẢI là gửi {"type": "auth", "token": "<token>"}
    try:
        raw_auth = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        auth_msg = _parse_message(raw_auth)
    except asyncio.TimeoutError:
        logger.warning("Auth timeout from %s", remote)
        await websocket.send(_build_response(msg_type="auth_result", status="error", message="Authentication timeout"))
        return
    except Exception as exc:
        logger.warning("Bad auth message from %s: %s", remote, exc)
        await websocket.send(_build_response(msg_type="auth_result", status="error", message="Invalid auth message"))
        return

    if auth_msg.get("type") != "auth":
        await websocket.send(_build_response(msg_type="auth_result", status="error", message="First message must be type=auth"))
        return

    provided_token = auth_msg.get("token", "")
    if not await state.authenticate(websocket, provided_token):
        logger.warning("Auth failed from %s (wrong token)", remote)
        await websocket.send(_build_response(msg_type="auth_result", status="error", message="Invalid token"))
        return  # Ngắt kết nối ngay lập tức

    # Xác thực thành công
    await websocket.send(_build_response(
        msg_type="auth_result",
        status="ok",
        message=f"Authenticated. Gateway ready. Client ID: {state.client_id(websocket)}",
    ))
    await websocket.send(_build_response(
        msg_type="permission_state",
        data={
            "permissions": state.orchestrator.security.permission_snapshot(),
            "managed_by": "agent_gateway_local_control",
        },
    ))
    logger.info("Client %s fully authenticated", state.client_id(websocket))

    # Lệnh hệ thống chạy tuần tự qua worker riêng. Vòng nhận message vẫn rảnh để
    # WebSocket vẫn nhận trạng thái quyền trong khi lệnh blocking đang chạy.
    command_queue: asyncio.Queue[str | bytes] = asyncio.Queue()

    async def command_worker() -> None:
        while True:
            queued_message = await command_queue.get()
            try:
                await _route_message(queued_message, websocket, state)
            finally:
                command_queue.task_done()

    worker_task = asyncio.create_task(
        command_worker(), name=f"commands-{state.client_id(websocket)}"
    )

    # ── Bước 2: Vòng lặp nhận và xử lý lệnh ─────────────────────────────────
    try:
        async for raw_message in websocket:
            try:
                incoming_type = _parse_message(raw_message).get("type")
            except ValueError:
                incoming_type = None
            if incoming_type == "command":
                await command_queue.put(raw_message)
            else:
                await _route_message(raw_message, websocket, state)
    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("Connection closed: %s (code=%s)", state.client_id(websocket), exc.code)
    except Exception:
        logger.exception("Unexpected error for client %s", state.client_id(websocket))
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await state.remove_client(websocket)


async def _route_message(
    raw_message: str | bytes,
    websocket: ServerConnection,
    state: GatewayState,
) -> None:
    """Phân loại message và định tuyến đến handler tương ứng."""
    try:
        msg = _parse_message(raw_message)
    except ValueError as exc:
        await websocket.send(_build_response(msg_type="error", status="error", message=str(exc)))
        return

    msg_type = msg.get("type", "")
    request_id = msg.get("request_id") or str(uuid.uuid4())[:8]

    if msg_type == "permission_get":
        await websocket.send(_build_response(
            msg_type="permission_state",
            request_id=request_id,
            data={
                "permissions": state.orchestrator.security.permission_snapshot(),
                "managed_by": "agent_gateway_local_control",
            },
        ))
        return

    # ── Streaming control ──────────────────────────────────────────────────────
    if msg_type == "stream_control":
        await _handle_stream_control(msg, websocket, state, request_id)
        return

    # ── Module command: định tuyến đến AgentOrchestrator ─────────────────────
    if msg_type == "command":
        await _handle_command(msg, websocket, state, request_id)
        return

    # ── Ping/Pong để giữ kết nối ─────────────────────────────────────────────
    if msg_type == "ping":
        await websocket.send(_build_response(msg_type="pong", request_id=request_id))
        return

    # Message không xác định
    await websocket.send(_build_response(
        msg_type="error",
        request_id=request_id,
        status="error",
        message=f"Unknown message type: {msg_type!r}. Valid types: command, stream_control, permission_get, ping",
    ))


async def _handle_stream_control(
    msg: dict[str, Any],
    websocket: ServerConnection,
    state: GatewayState,
    request_id: str,
) -> None:
    """Xử lý lệnh bắt đầu/dừng streaming màn hình hoặc webcam.

    Định dạng:
      {"type": "stream_control", "module": "screen"|"webcam", "action": "start"|"stop",
       "camera_index": 0}   ← camera_index chỉ cần cho webcam
    """
    module = msg.get("module", "")
    action = msg.get("action", "")

    if module == "screen":
        if action == "start":
            # Yêu cầu consent trước khi bắt đầu stream màn hình
            consent_request = {"request_id": request_id, "module": "screen_stream", "parameters": {}}
            approved = await asyncio.get_event_loop().run_in_executor(
                None, state.orchestrator.security.authorize_request, consent_request
            )
            if approved is None:
                await websocket.send(_build_response(
                    msg_type="stream_control_result", request_id=request_id,
                    status="denied", data={"module": "screen", "status": "denied"},
                    message="User denied screen stream permission",
                ))
                return
            if not state.orchestrator.security.is_module_allowed("screen_stream"):
                await websocket.send(_build_response(
                    msg_type="stream_control_result", request_id=request_id,
                    status="denied", data={"module": "screen", "status": "denied"},
                    message="Screen permission was revoked while consent was pending",
                ))
                return
            await state.start_screen_stream(websocket)
            await websocket.send(_build_response(msg_type="stream_control_result", request_id=request_id, data={"module": "screen", "status": "started"}))
        elif action == "stop":
            await state.stop_screen_stream(websocket)
            await websocket.send(_build_response(msg_type="stream_control_result", request_id=request_id, data={"module": "screen", "status": "stopped"}))
        else:
            await websocket.send(_build_response(msg_type="error", request_id=request_id, status="error", message="action must be start or stop"))

    elif module == "webcam":
        camera_index = int(msg.get("camera_index", 0))
        if action == "start":
            # Yêu cầu consent trước khi bắt đầu stream webcam
            consent_request = {"request_id": request_id, "module": "camera_stream", "parameters": {}}
            approved = await asyncio.get_event_loop().run_in_executor(
                None, state.orchestrator.security.authorize_request, consent_request
            )
            if approved is None:
                await websocket.send(_build_response(
                    msg_type="stream_control_result", request_id=request_id,
                    status="denied", data={"module": "webcam", "status": "denied"},
                    message="User denied webcam stream permission",
                ))
                return
            if not state.orchestrator.security.is_module_allowed("camera_stream"):
                await websocket.send(_build_response(
                    msg_type="stream_control_result", request_id=request_id,
                    status="denied", data={"module": "webcam", "status": "denied"},
                    message="Webcam permission was revoked while consent was pending",
                ))
                return
            await state.start_webcam_stream(websocket, camera_index)
            await websocket.send(_build_response(msg_type="stream_control_result", request_id=request_id, data={"module": "webcam", "status": "started", "camera_index": camera_index}))
        elif action == "stop":
            await state.stop_webcam_stream(websocket)
            await websocket.send(_build_response(msg_type="stream_control_result", request_id=request_id, data={"module": "webcam", "status": "stopped"}))
        else:
            await websocket.send(_build_response(msg_type="error", request_id=request_id, status="error", message="action must be start or stop"))

    else:
        await websocket.send(_build_response(
            msg_type="error", request_id=request_id, status="error",
            message=f"Unknown stream module: {module!r}. Valid: screen, webcam",
        ))


async def _handle_command(
    msg: dict[str, Any],
    websocket: ServerConnection,
    state: GatewayState,
    request_id: str,
) -> None:
    """Định tuyến lệnh module đến AgentOrchestrator và gửi kết quả về.

    Định dạng lệnh:
      {
        "type": "command",
        "request_id": "abc123",     ← tùy chọn, gateway tự tạo nếu thiếu
        "module": "list_processes",
        "parameters": {}
      }
    """
    module = msg.get("module", "")
    parameters = msg.get("parameters", {})

    if not isinstance(parameters, dict):
        await websocket.send(_build_response(
            msg_type="command_result", request_id=request_id,
            status="error", message="parameters must be a JSON object",
        ))
        return

    # Đóng gói lại theo định dạng AgentOrchestrator mong đợi
    agent_request = {
        "request_id": request_id,
        "module": module,
        "parameters": parameters,
    }

    logger.info("Dispatching command: module=%s request_id=%s", module, request_id)

    try:
        # AgentOrchestrator.dispatch_command là async nên await trực tiếp
        result = await state.orchestrator.dispatch_command(agent_request)
    except Exception as exc:
        logger.exception("Unexpected error dispatching command %s", module)
        await websocket.send(_build_response(
            msg_type="command_result", request_id=request_id,
            status="error", message=f"Gateway internal error: {exc}",
        ))
        return

    # Trả kết quả về Web App
    response_status = result.get("status", "ok")
    response_data = result.get("data")
    response_message = result.get("message")

    await websocket.send(_build_response(
        msg_type="command_result",
        request_id=request_id,
        status=response_status,
        data=response_data,
        message=response_message,
    ))
    logger.info("Command result sent: module=%s status=%s", module, response_status)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


async def run_gateway(host: str, port: int, token: str, show_local_control: bool = True) -> None:
    """Khởi động WebSocket Gateway Server."""
    allowed_apps = _load_allowed_applications()
    logger.info("Allowed applications: %s", list(allowed_apps.keys()) or "(none)")
    orchestrator = AgentOrchestrator(allowed_applications=allowed_apps)

    # Kiểm tra consent ban đầu (chỉ hiển thị dialog trên Windows)
    if os.name == "nt" and not orchestrator.security.ensure_initial_consent():
        logger.error("Initial consent denied by user. Gateway will not start.")
        return

    state = GatewayState(token=token, orchestrator=orchestrator)
    local_control: LocalPermissionControl | None = None
    if show_local_control:
        loop = asyncio.get_running_loop()

        def submit_local_update(updates: dict[str, bool]):
            return asyncio.run_coroutine_threadsafe(
                state.apply_local_permission_update(updates), loop
            )

        local_control = LocalPermissionControl(
            snapshot_provider=orchestrator.security.permission_snapshot,
            submit_update=submit_local_update,
        )
        local_control.start()

    logger.info("=" * 60)
    logger.info("  WebSocket Gateway Server starting")
    logger.info("  Listening on ws://%s:%d", host, port)
    logger.info("  Screen stream: %.1f fps | Webcam stream: %.1f fps", SCREEN_FPS, WEBCAM_FPS)
    logger.info("=" * 60)

    # Tạo partial handler để truyền state vào
    async def handler(websocket: ServerConnection) -> None:
        await _handle_client(websocket, state)

    try:
        async with serve(
            handler,
            host,
            port,
            # JPEG is already compressed; per-message deflate wastes CPU here.
            compression=None,
            # Tăng giới hạn kích thước message lên 10MB để xử lý ảnh base64
            max_size=10 * 1024 * 1024,
            # Ping mỗi 20s để phát hiện kết nối chết
            ping_interval=20,
            ping_timeout=20,
        ) as server:
            logger.info("Gateway is running. Press Ctrl+C to stop.")
            await server.serve_forever()
    finally:
        if local_control is not None:
            local_control.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Remote Administration Gateway Server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host để bind (mặc định: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Cổng lắng nghe (mặc định: 8765)")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Token xác thực bí mật")
    parser.add_argument(
        "--no-local-control",
        action="store_true",
        help="Không mở cửa sổ quản lý quyền cục bộ (dành cho máy headless)",
    )
    parser.add_argument(
        "--generate-token",
        action="store_true",
        help="Tạo một token ngẫu nhiên an toàn và in ra màn hình",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("\n" + "=" * 72)
    print("GATEWAY TOKEN FOR THIS RUN:")
    print(args.token)
    print("=" * 72 + "\n", flush=True)

    try:
        asyncio.run(
            run_gateway(
                host=args.host,
                port=args.port,
                token=args.token,
                show_local_control=not args.no_local_control,
            )
        )
    except KeyboardInterrupt:
        logger.info("Gateway stopped by user.")


if __name__ == "__main__":
    main()
