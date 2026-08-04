"""Secure, sandbox-restricted local file management utilities for Windows."""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from typing import Union

PathInput = Union[str, os.PathLike[str]]

# Định nghĩa thư mục Sandbox tuyệt đối
SANDBOX_ROOT = Path(r"C:\RemoteWorkspace").resolve()


def safe_path_resolve(requested_path: PathInput) -> Path:
    """Đảm bảo mọi đường dẫn truy cập đều nằm nghiêm ngặt trong Sandbox."""
    if not isinstance(requested_path, (str, os.PathLike)):
        raise TypeError("requested_path must be a string or path-like object")

    root = SANDBOX_ROOT.resolve()
    candidate = Path(requested_path)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (ValueError, OSError) as exc:
        raise PermissionError(f"Path is outside the sandbox: {requested_path!s}") from exc
    return resolved


def list_drives() -> list[str]:
    if os.name != "nt":
        raise OSError("Drive enumeration is only supported on Windows")
    return [f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:\\").exists()]


def list_directory(requested_path: PathInput = ".") -> list[Path]:
    """Liệt kê danh sách file/thư mục trong Sandbox."""
    directory = safe_path_resolve(requested_path)
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    entries: list[Path] = []
    for entry in directory.iterdir():
        try:
            entries.append(safe_path_resolve(entry))
        except PermissionError:
            continue
    return sorted(entries, key=lambda item: item.name.lower())


def read_file(requested_path: PathInput) -> bytes:
    """Tải file từ Sandbox về (Download)."""
    file_path = safe_path_resolve(requested_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File does not exist: {file_path}")
    return file_path.read_bytes()


def write_file(requested_path: PathInput, data: bytes, *, overwrite: bool = True) -> Path:
    """Lưu file tải lên (Upload) vào thư mục Sandbox."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    destination = safe_path_resolve(requested_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("wb") as output:
        shutil.copyfileobj(io.BytesIO(data), output)
    return destination


def delete_file(requested_path: PathInput) -> None:
    """Xóa file trong Sandbox."""
    file_path = safe_path_resolve(requested_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File does not exist: {file_path}")
    if not file_path.is_file():
        raise IsADirectoryError(f"Refusing to delete non-file path: {file_path}")
    file_path.unlink()