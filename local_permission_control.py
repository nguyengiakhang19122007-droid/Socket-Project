"""Native permission panel that runs only on the Agent/Gateway machine."""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable, Mapping


Snapshot = Mapping[str, Mapping[str, Any]]
SubmitUpdate = Callable[[dict[str, bool]], Future[Any]]


class LocalPermissionControl:
    """Tkinter control panel; it never communicates through the Web App socket."""

    def __init__(
        self,
        snapshot_provider: Callable[[], Snapshot],
        submit_update: SubmitUpdate,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._submit_update = submit_update
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root: Any = None
        self._buttons: dict[str, Any] = {}
        self._status_labels: dict[str, Any] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="agent-permission-control",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._events.put(("close", None))

    def _run(self) -> None:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            self._root = root
            root.title("Agent Permission Control")
            root.geometry("610x540")
            root.minsize(560, 480)
            root.configure(bg="#10141d")

            title = tk.Label(
                root,
                text="AGENT / GATEWAY PERMISSION CONTROL",
                bg="#10141d",
                fg="#00d4ff",
                font=("Segoe UI", 14, "bold"),
            )
            title.pack(anchor="w", padx=20, pady=(18, 4))
            tk.Label(
                root,
                text="Cửa sổ này chạy cục bộ trên máy Agent. Web App chỉ có quyền xem trạng thái.",
                bg="#10141d",
                fg="#a7b4ca",
                font=("Segoe UI", 9),
                wraplength=560,
                justify="left",
            ).pack(anchor="w", padx=20, pady=(0, 14))

            rows = tk.Frame(root, bg="#10141d")
            rows.pack(fill="both", expand=True, padx=20)
            snapshot = self._snapshot_provider()
            for feature, item in snapshot.items():
                row = tk.Frame(rows, bg="#181e2a", highlightbackground="#293249", highlightthickness=1)
                row.pack(fill="x", pady=4)
                copy = tk.Frame(row, bg="#181e2a")
                copy.pack(side="left", fill="both", expand=True, padx=12, pady=9)
                tk.Label(
                    copy,
                    text=str(item.get("label", feature)),
                    bg="#181e2a",
                    fg="#d8e2f2",
                    font=("Segoe UI", 10, "bold"),
                ).pack(anchor="w")
                modules = " · ".join(str(value) for value in item.get("modules", []))
                tk.Label(
                    copy,
                    text=modules,
                    bg="#181e2a",
                    fg="#66738e",
                    font=("Consolas", 7),
                    wraplength=390,
                    justify="left",
                ).pack(anchor="w", pady=(2, 0))
                status = tk.Label(row, width=10, bg="#181e2a", font=("Consolas", 8, "bold"))
                status.pack(side="left", padx=4)
                button = tk.Button(row, width=9, font=("Segoe UI", 8, "bold"))
                button.pack(side="right", padx=10)
                self._status_labels[feature] = status
                self._buttons[feature] = button

            actions = tk.Frame(root, bg="#10141d")
            actions.pack(fill="x", padx=20, pady=14)
            tk.Button(
                actions,
                text="THU HỒI TOÀN BỘ QUYỀN",
                bg="#b42332",
                fg="white",
                activebackground="#d03646",
                activeforeground="white",
                font=("Segoe UI", 9, "bold"),
                command=lambda: self._confirm_revoke_all(messagebox),
            ).pack(side="left")
            tk.Button(
                actions,
                text="Làm mới",
                command=self._refresh,
                font=("Segoe UI", 9),
            ).pack(side="right")

            # Nút X chỉ thu nhỏ xuống taskbar để chủ máy luôn có thể mở lại
            # bảng thu hồi quyền mà không phải restart Gateway.
            root.protocol("WM_DELETE_WINDOW", root.iconify)
            self._refresh()
            root.after(100, self._poll_events)
            root.mainloop()
        except Exception:
            logging.getLogger("gateway").exception("Local permission panel could not start")
        finally:
            self._root = None

    def _refresh(self) -> None:
        snapshot = self._snapshot_provider()
        for feature, item in snapshot.items():
            enabled = bool(item.get("enabled", True))
            status = self._status_labels.get(feature)
            button = self._buttons.get(feature)
            if status is not None:
                status.configure(
                    text="● ALLOWED" if enabled else "● REVOKED",
                    fg="#20c987" if enabled else "#ff5b69",
                )
            if button is not None:
                button.configure(
                    text="Disable" if enabled else "Enable",
                    bg="#3b2029" if enabled else "#17382e",
                    fg="#ff8290" if enabled else "#54d6a7",
                    command=lambda name=feature, value=not enabled: self._submit({name: value}),
                    state="normal",
                )

    def _submit(self, updates: dict[str, bool]) -> None:
        for button in self._buttons.values():
            button.configure(state="disabled")
        try:
            future = self._submit_update(updates)
            future.add_done_callback(lambda completed: self._events.put(("complete", completed)))
        except Exception as exc:
            self._events.put(("error", exc))

    def _confirm_revoke_all(self, messagebox: Any) -> None:
        if not messagebox.askyesno(
            "Thu hồi toàn bộ quyền",
            "Dừng mọi tính năng đang chạy và chặn toàn bộ quyền của Web App?",
            parent=self._root,
        ):
            return
        snapshot = self._snapshot_provider()
        self._submit({feature: False for feature in snapshot})

    def _poll_events(self) -> None:
        if self._root is None:
            return
        try:
            while True:
                event, value = self._events.get_nowait()
                if event == "close":
                    self._root.destroy()
                    return
                if event == "complete":
                    try:
                        value.result()
                    except Exception as exc:
                        logging.getLogger("gateway").error("Permission update failed: %s", exc)
                    self._refresh()
                elif event == "error":
                    logging.getLogger("gateway").error("Permission update failed: %s", value)
                    self._refresh()
        except queue.Empty:
            pass
        self._root.after(100, self._poll_events)
