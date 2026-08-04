"""Windows Agent Main Orchestrator binding all 7 modules together.
Prerequisites: pip install websockets psutil mss opencv-python pynput plyer
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from typing import Any

import cv2 

from agent_security import SecurityController

from client_agent import show_notification

from desktop_capture_utils import (
    capture_primary_screen_png, 
    capture_camera_frame, 
    RemoteKeylogger, 
    VisualIndicator
)

from sandbox_file_manager import list_directory, read_file, write_file, delete_file

from windows_process_manager import (
    list_processes, 
    list_applications, 
    launch_application, 
    stop_application_by_name, 
    stop_process
)

from windows_system_control import (
    get_system_metrics, 
    lock_workstation, 
    sleep_system, 
    restart_system, 
    shutdown_system
)

GATEWAY_URL = os.getenv("WINDOWS_AGENT_GATEWAY_URL", "ws://127.0.0.1:8765")
AGENT_TOKEN = os.getenv("WINDOWS_AGENT_GATEWAY_TOKEN", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")


_DEFAULT_ALLOWED_APPS: dict[str, str] = {
    "notepad": r"C:\Windows\System32\notepad.exe",
    "calc": r"C:\Windows\System32\calc.exe",
}


class AgentOrchestrator:
    """Điều phối thực thi toàn bộ 7 Module."""

    def __init__(self, allowed_applications: dict[str, str] | None = None) -> None:
        self.security = SecurityController(
            allowed_applications=allowed_applications if allowed_applications is not None else _DEFAULT_ALLOWED_APPS
        )
        self.keylogger = RemoteKeylogger()

        self.screen_stream_indicator = VisualIndicator(color="green", label_text="SCREEN LIVESTREAM ACTIVE")
        self.camera_stream_indicator = VisualIndicator(color="red", label_text="WEBCAM STREAM ACTIVE")

    async def dispatch_command(self, raw_message: str | dict[str, Any]) -> dict[str, Any]:
        try:
            request_data = json.loads(raw_message) if isinstance(raw_message, str) else raw_message
            validated_request = self.security.authorize_request(request_data)
            
            if validated_request is None:
                return {"status": "denied", "message": "User or policy rejected the request."}

            module = validated_request.module
            params = validated_request.parameters
            req_id = validated_request.request_id

            logging.info("Executing module: %s (Request ID: %s)", module, req_id)
            show_notification("Windows Agent Active", f"Module: {module.replace('_', ' ').title()}")

            result_data = self._execute_module(module, params)
            return {"status": "success", "request_id": req_id, "module": module, "data": result_data}

        except Exception as exc:
            logging.exception("Error executing command")
            return {"status": "error", "message": str(exc)}

    def _execute_module(self, module: str, params: dict[str, Any]) -> Any:
        # 1. MODULE APPLICATION
        if module == "application_list":
            return list_applications(self.security.allowed_applications)
        elif module == "application_launch":
            proc = launch_application(params.get("executable"))
            return {"pid": proc.pid, "status": "launched"}
        elif module == "application_stop":
            stopped_count = stop_application_by_name(params.get("application"), self.security.allowed_applications)
            return {"stopped_count": stopped_count, "status": "stopped"}

        # 2. MODULE PROCESSES
        elif module == "list_processes":
            procs = list_processes(include_gui_status=True)
            return [{"pid": p.pid, "name": p.name, "cpu": p.cpu_percent, "ram_mb": p.ram_mb, "gui": p.is_gui_application} for p in procs]
        elif module == "process_terminate":
            stopped = stop_process(params.get("pid"))
            return {"pid": params.get("pid"), "terminated": stopped}

        # 3. MODULE SCREENSHOT - LIVESTREAM
        elif module == "screen_capture":
            png_bytes = capture_primary_screen_png()
            return {"image_base64": base64.b64encode(png_bytes).decode("utf-8")}
        elif module == "screen_stream":
            action = params.get("action", "start")
            if action == "start":
                self.screen_stream_indicator.start()
                png_bytes = capture_primary_screen_png()
                return {"status": "streaming", "image_base64": base64.b64encode(png_bytes).decode("utf-8")}
            else:
                self.screen_stream_indicator.stop()
                return {"status": "stopped"}

        # 4. MODULE KEYLOGGER
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
                return {"keystrokes": self.keylogger.get_and_clear()}

        # 5. MODULE FILES
        elif module == "file_list":
            items = list_directory(params.get("path", "."))
            return {"items": [str(p) for p in items]}
        elif module == "file_read":
            content_bytes = read_file(params.get("path"))
            return {"content_base64": base64.b64encode(content_bytes).decode("utf-8")}
        elif module == "file_write":
            data_bytes = base64.b64decode(params.get("content_base64", ""))
            saved_path = write_file(params.get("path"), data_bytes)
            return {"written_path": str(saved_path), "status": "success"}
        elif module == "file_delete":
            delete_file(params.get("path"))
            return {"status": "success"}

        # 6. MODULE WEBCAM
        elif module == "camera_capture":
            frame = capture_camera_frame(params.get("camera_index", 0))
            _, encoded_img = cv2.imencode(".jpg", frame)
            return {"image_base64": base64.b64encode(encoded_img.tobytes()).decode("utf-8")}
        elif module == "camera_stream":
            action = params.get("action", "start")
            if action == "start":
                self.camera_stream_indicator.start()
                frame = capture_camera_frame(params.get("camera_index", 0))
                _, encoded_img = cv2.imencode(".jpg", frame)
                return {"status": "streaming", "image_base64": base64.b64encode(encoded_img.tobytes()).decode("utf-8")}
            else:
                self.camera_stream_indicator.stop()
                return {"status": "stopped"}

        # 7. MODULE POWER CONTROL & METRICS
        elif module == "system_metrics":
            m = get_system_metrics()
            return {
                "uptime": m.uptime_str,  # Chuỗi thời gian máy tính đã hoạt động
                "cpu_temperature": m.cpu_temperature,
                "ram_usage_percent": m.ram_usage_percent,
                "uptime_seconds": m.uptime.total_seconds()
            }
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

        raise ValueError(f"Unknown module execution: {module}")


async def run_agent_daemon() -> None:
    orchestrator = AgentOrchestrator()
    if not orchestrator.security.ensure_initial_consent():
        sys.exit(1)

    from websockets.asyncio.client import connect

    while True:
        try:
            async with connect(GATEWAY_URL, additional_headers={"X-Agent-Token": AGENT_TOKEN}, open_timeout=10) as websocket:
                async for message in websocket:
                    response = await orchestrator.dispatch_command(message)
                    await websocket.send(json.dumps(response))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            await asyncio.sleep(3)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        asyncio.run(run_agent_daemon())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()