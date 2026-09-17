"""Simple local (or pluggable) object storage for generated media bytes."""
from __future__ import annotations

import os
from pathlib import Path

from ..config import settings


def storage_root() -> Path:
    root = Path(settings.asset_storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_bytes(relative_path: str, data: bytes) -> str:
    """Persist bytes under ASSET_STORAGE_DIR; return the relative storage_path."""
    relative_path = relative_path.lstrip("/").replace("..", "_")
    dest = storage_root() / relative_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return relative_path


def read_bytes(relative_path: str) -> bytes:
    path = storage_root() / relative_path.lstrip("/")
    return path.read_bytes()


def absolute_path(relative_path: str) -> str:
    return str((storage_root() / relative_path.lstrip("/")).resolve())


def exists(relative_path: str) -> bool:
    return (storage_root() / relative_path.lstrip("/")).is_file()
