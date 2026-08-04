"""Local WebSocket client agent for desktop notifications."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from plyer import notification

SERVER_URL = os.getenv("LOCAL_AGENT_SERVER_URL", "ws://127.0.0.1:8765")


def show_notification(title: str, message: str) -> None:
    """Hiển thị thông báo trên màn hình Windows."""
    try:
        notification.notify(
            title=title,
            message=message,
            app_name="Windows Agent Client",
            timeout=5,
        )
    except Exception:
        pass


def notification_from_message(payload: str | bytes) -> tuple[str, str] | None:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")

    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError:
        return ("Remote action", payload.strip()) if payload.strip() else None

    if not isinstance(data, Mapping) or data.get("type") != "remote_action":
        return None

    title = str(data.get("title", "Remote Action"))
    message = str(data.get("message", "A remote action was triggered."))
    return title, message