"""Secure, sandbox-restricted local file management utilities for Windows."""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from typing import Union


PathInput = Union[str, os.PathLike[str]]
SANDBOX_ROOT = Path(r"C:\RemoteWorkspace").resolve()


def safe_path_resolve(requested_path: PathInput) -> Path:

    """Resolve *requested_path* and ensure it remains inside ``SANDBOX_ROOT``.
    Relative paths are interpreted from the sandbox root. Absolute paths are
    allowed only if they point into it. Resolving first means existing symbolic
    links cannot be used to escape the sandbox. A ``PermissionError`` is raised
    for any path outside the boundary. """

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
    """Return the currently available Windows drive roots, such as ``C:\\``."""
    if os.name != "nt":
        raise OSError("Drive enumeration is only supported on Windows")
    if hasattr(os, "listdrives"):
        return sorted(os.listdrives())
    return [f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:\\").exists()]


def list_directory(requested_path: PathInput = ".") -> list[Path]:
    """List direct children of a validated sandbox directory, sorted by name.

    Returned paths are absolute resolved paths within the sandbox.
    """
    directory = safe_path_resolve(requested_path)
    if not directory.exists():
        raise FileNotFoundError(f"Directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")
    try:
        entries: list[Path] = []
        for entry in directory.iterdir():
            try:
                entries.append(safe_path_resolve(entry))
            except PermissionError:
                # Do not expose a symlink whose target leaves the sandbox.
                continue
        return sorted(entries, key=lambda item: item.name.lower())
    except OSError as exc:
        raise OSError(f"Unable to list directory {directory}: {exc}") from exc


def read_file(requested_path: PathInput) -> bytes:
    """Read and return bytes from a file inside the sandbox."""
    file_path = safe_path_resolve(requested_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File does not exist: {file_path}")
    try:
        return file_path.read_bytes()
    except OSError as exc:
        raise OSError(f"Unable to read file {file_path}: {exc}") from exc


def write_file(requested_path: PathInput, data: bytes, *, overwrite: bool = False) -> Path:
    """Save bytes within the sandbox and return the resolved destination path.

    Parent directories must already exist, avoiding an unexpected directory tree
    creation. Existing files are protected unless ``overwrite=True`` is passed.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    destination = safe_path_resolve(requested_path)
    if destination == SANDBOX_ROOT:
        raise IsADirectoryError("Cannot write to the sandbox root")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"Parent directory does not exist: {destination.parent}")
    if destination.exists() and not destination.is_file():
        raise IsADirectoryError(f"Destination is not a regular file: {destination}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"File already exists: {destination}")

    try:
        # copyfileobj keeps all write handling in binary mode and avoids a shell.
        with destination.open("wb") as output:
            shutil.copyfileobj(io.BytesIO(data), output)
    except OSError as exc:
        raise OSError(f"Unable to write file {destination}: {exc}") from exc
    return destination


def delete_file(requested_path: PathInput) -> None:
    """Delete one regular file inside the sandbox; directories are never removed."""
    file_path = safe_path_resolve(requested_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File does not exist: {file_path}")
    if not file_path.is_file():
        raise IsADirectoryError(f"Refusing to delete non-file path: {file_path}")
    try:
        file_path.unlink()
    except OSError as exc:
        raise OSError(f"Unable to delete file {file_path}: {exc}") from exc
