"""Local WebSocket client agent.
Listens for messages from a local WebSocket server and shows a Windows toast
notification when a remote action is received.
Install dependencies with: pip install websockets plyer
Run with: python client_agent.py """

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from plyer import notification
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException


SERVER_URL = os.getenv("LOCAL_AGENT_SERVER_URL", "ws://127.0.0.1:8765")
RETRY_INITIAL_SECONDS = 1
RETRY_MAX_SECONDS = 30


def show_notification(title: str, message: str) -> None:
    """Display a native desktop notification without crashing the agent."""
    try:
        notification.notify(
            title=title,
            message=message,
            app_name="Local Client Agent",
            timeout=10,
        )
    except Exception:
        logging.exception("Unable to display local notification")


def notification_from_message(payload: str | bytes) -> tuple[str, str] | None:
    """Extract a notification from a JSON remote-action message.

    Expected shape: {"type": "remote_action", "title": "...", "message": "..."}
    A plain-text message is also treated as a remote action for simple servers.
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")

    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError:
        return ("Remote action", payload.strip()) if payload.strip() else None

    if not isinstance(data, Mapping) or data.get("type") != "remote_action":
        return None

    title = str(data.get("title", "Remote action"))
    message = str(data.get("message", data.get("action", "A remote action was triggered.")))
    return title, message


async def handle_connection(websocket: Any) -> None:
    """Receive and process messages until the current connection closes."""
    async for payload in websocket:
        event = notification_from_message(payload)
        if event is None:
            logging.debug("Ignoring unsupported message: %r", payload)
            continue

        title, message = event
        logging.info("Remote action received: %s", message)
        show_notification(title, message)


async def run_agent(server_url: str) -> None:
    """Maintain a WebSocket connection with capped exponential backoff."""
    retry_delay = RETRY_INITIAL_SECONDS

    while True:
        try:
            logging.info("Connecting to %s", server_url)
            async with connect(server_url, ping_interval=20, ping_timeout=20) as websocket:
                logging.info("Connected to local server")
                retry_delay = RETRY_INITIAL_SECONDS
                await handle_connection(websocket)
        except asyncio.CancelledError:
            raise
        except (OSError, WebSocketException, ConnectionClosed) as exc:
            logging.warning("Connection lost (%s); retrying in %s seconds", exc, retry_delay)
        except Exception:
            logging.exception("Unexpected agent error; retrying in %s seconds", retry_delay)

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, RETRY_MAX_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(run_agent(SERVER_URL))
    except KeyboardInterrupt:
        logging.info("Client agent stopped")


if __name__ == "__main__":
    main()
