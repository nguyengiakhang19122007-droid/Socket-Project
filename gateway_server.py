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
  SCREEN_FPS           Số frame/giây cho livestream màn hình (mặc định: 5)
  WEBCAM_FPS           Số frame/giây cho livestream webcam (mặc định: 10)
  LOG_LEVEL            Mức log: DEBUG / INFO / WARNING (mặc định: INFO)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import secrets
import time
import uuid
from typing import Any

import cv2
import websockets
from websockets.asyncio.server import ServerConnection, serve

# Import AgentOrchestrator từ agent_main
from agent_main import AgentOrchestrator
from agent_security import verify_gateway_token
from desktop_capture_utils import capture_primary_screen_png, capture_camera_frame

# ──────────────────────────────────────────────────────────────────────────────
# Cấu hình mặc định
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("GATEWAY_PORT", "8765"))
DEFAULT_TOKEN = os.getenv(
    "GATEWAY_TOKEN",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)
SCREEN_FPS = float(os.getenv("SCREEN_FPS", "5"))
WEBCAM_FPS = float(os.getenv("WEBCAM_FPS", "10"))

logger = logging.getLogger("gateway")

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

    async def stop_screen_stream(self, websocket: ServerConnection) -> None:
        async with self._lock:
            await self._cancel_stream_task(self._screen_tasks, websocket, "screen")

    async def start_webcam_stream(self, websocket: ServerConnection, camera_index: int = 0) -> None:
        async with self._lock:
            await self._cancel_stream_task(self._webcam_tasks, websocket, "webcam")
            task = asyncio.create_task(
                _webcam_stream_loop(websocket, camera_index, WEBCAM_FPS),
                name=f"webcam-{self.client_id(websocket)}",
            )
            self._webcam_tasks[websocket] = task

    async def stop_webcam_stream(self, websocket: ServerConnection) -> None:
        async with self._lock:
            await self._cancel_stream_task(self._webcam_tasks, websocket, "webcam")

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
    """Liên tục chụp màn hình và gửi frame qua WebSocket."""
    interval = 1.0 / max(fps, 0.5)
    logger.info("Screen stream started (%.1f fps)", fps)
    try:
        while True:
            t_start = time.monotonic()
            try:
                # Chạy trong executor để không chặn event loop
                png_bytes = await asyncio.get_event_loop().run_in_executor(
                    None, capture_primary_screen_png
                )
                frame_b64 = base64.b64encode(png_bytes).decode("utf-8")
                payload = json.dumps({
                    "type": "stream_frame",
                    "module": "screen",
                    "data": {"image_base64": frame_b64},
                })
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
        logger.info("Screen stream stopped")


async def _webcam_stream_loop(
    websocket: ServerConnection, camera_index: int, fps: float
) -> None:
    """Liên tục chụp frame webcam và gửi qua WebSocket."""
    interval = 1.0 / max(fps, 0.5)
    logger.info("Webcam stream started (index=%d, %.1f fps)", camera_index, fps)
    try:
        while True:
            t_start = time.monotonic()
            try:
                frame = await asyncio.get_event_loop().run_in_executor(
                    None, capture_camera_frame, camera_index
                )
                success, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not success:
                    raise RuntimeError("Failed to encode webcam frame")
                frame_b64 = base64.b64encode(encoded.tobytes()).decode("utf-8")
                payload = json.dumps({
                    "type": "stream_frame",
                    "module": "webcam",
                    "data": {"image_base64": frame_b64},
                })
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
    logger.info("Client %s fully authenticated", state.client_id(websocket))

    # ── Bước 2: Vòng lặp nhận và xử lý lệnh ─────────────────────────────────
    try:
        async for raw_message in websocket:
            await _route_message(raw_message, websocket, state)
    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("Connection closed: %s (code=%s)", state.client_id(websocket), exc.code)
    except Exception:
        logger.exception("Unexpected error for client %s", state.client_id(websocket))
    finally:
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
        message=f"Unknown message type: {msg_type!r}. Valid types: command, stream_control, ping",
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


async def run_gateway(host: str, port: int, token: str) -> None:
    """Khởi động WebSocket Gateway Server."""
    orchestrator = AgentOrchestrator()

    # Kiểm tra consent ban đầu (chạy một lần, lưu vào file)
    if not orchestrator.security.ensure_initial_consent():
        logger.error("Initial consent denied by user. Gateway will not start.")
        return

    state = GatewayState(token=token, orchestrator=orchestrator)

    logger.info("=" * 60)
    logger.info("  WebSocket Gateway Server starting")
    logger.info("  Listening on ws://%s:%d", host, port)
    logger.info("  Screen stream: %.1f fps | Webcam stream: %.1f fps", SCREEN_FPS, WEBCAM_FPS)
    logger.info("=" * 60)

    # Tạo partial handler để truyền state vào
    async def handler(websocket: ServerConnection) -> None:
        await _handle_client(websocket, state)

    async with serve(
        handler,
        host,
        port,
        # Tăng giới hạn kích thước message lên 10MB để xử lý ảnh base64
        max_size=10 * 1024 * 1024,
        # Ping mỗi 20s để phát hiện kết nối chết
        ping_interval=20,
        ping_timeout=20,
    ) as server:
        logger.info("Gateway is running. Press Ctrl+C to stop.")
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Remote Administration Gateway Server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host để bind (mặc định: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Cổng lắng nghe (mặc định: 8765)")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Token xác thực bí mật")
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

    if args.generate_token:
        print(f"Generated token: {secrets.token_hex(32)}")
        print("Lưu token này vào biến môi trường GATEWAY_TOKEN trên cả hai máy.")
        return

    if len(args.token) < 32:
        logger.warning(
            "Token quá ngắn (< 32 ký tự). Hãy dùng --generate-token để tạo token an toàn."
        )

    try:
        asyncio.run(run_gateway(host=args.host, port=args.port, token=args.token))
    except KeyboardInterrupt:
        logger.info("Gateway stopped by user.")


if __name__ == "__main__":
    main()
