"""Export staging packages for manual upload when live credentials are missing."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ...config import settings


def staging_root() -> Path:
    root = Path(settings.asset_storage_dir).resolve().parent / "staging"
    # Prefer explicit SCHEDULE/DISTRIBUTE staging dir when set
    explicit = (settings.distribute_staging_dir or "").strip()
    if explicit:
        root = Path(explicit)
    root.mkdir(parents=True, exist_ok=True)
    return root


def export_package(
    *,
    job_id: str,
    platforms: list[str],
    caption_bundle: dict[str, Any],
    video_url: str | None,
    video_storage_path: str | None,
    script: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a review/upload package under data/staging/{job_id}/.

    Never posts publicly. Suitable for manual TikTok/Reels/Shorts upload.
    """
    dest = staging_root() / str(job_id)
    dest.mkdir(parents=True, exist_ok=True)

    # Copy local video bytes when we have a storage path under asset dir
    video_copied = None
    if video_storage_path:
        src = Path(video_storage_path)
        if not src.is_absolute():
            src = Path(settings.asset_storage_dir) / video_storage_path
        if src.exists() and src.is_file():
            video_copied = dest / f"video{src.suffix or '.mp4'}"
            shutil.copy2(src, video_copied)

    manifest = {
        "job_id": job_id,
        "platforms": platforms,
        "video_url": video_url,
        "video_file": str(video_copied.name) if video_copied else None,
        "video_storage_path": video_storage_path,
        "script": script or {},
        "captions": caption_bundle,
        "public_post": False,
        "note": (
            "Staging export only — not published. Approve gate already passed; "
            "live distribute runs when platform credentials are set."
        ),
        **(extra or {}),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))

    # Per-platform caption files for manual paste
    for plat, cap in (caption_bundle or {}).items():
        if not isinstance(cap, dict):
            continue
        lines = [cap.get("caption") or ""]
        hashtags = cap.get("hashtags") or []
        if hashtags:
            lines.append("")
            lines.append(" ".join(f"#{h.lstrip('#')}" for h in hashtags))
        (dest / f"{plat}_caption.txt").write_text("\n".join(lines).strip() + "\n")

    return dest
