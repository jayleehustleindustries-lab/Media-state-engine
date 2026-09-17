"""Outbound distribution — approval-gated, credential-safe.

Wired platform: YouTube Shorts (live when OAuth env present, else staging export).
Documented-only next: TikTok, Instagram Reels, YouTube Shorts manual, generic Shorts.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from ...config import settings
from .base import DistributeResult, Distributor
from .staging import export_package
from .youtube import YouTubeDistributor

# Platforms we stage captions for but do not auto-post in Phase 4
DOCUMENTED_ONLY_PLATFORMS = ("tiktok", "reels", "shorts")

_YOUTUBE = YouTubeDistributor()


def get_distributors() -> list[Distributor]:
    return [_YOUTUBE]


def _parse(v):
    if isinstance(v, str):
        return json.loads(v)
    return v


def _resolve_local_video(asset: dict | None) -> tuple[str | None, str | None]:
    """Return (absolute_or_relative_path, url)."""
    if not asset:
        return None, None
    url = asset.get("url")
    storage = asset.get("storage_path")
    path = None
    if storage:
        from pathlib import Path
        p = Path(storage)
        if not p.is_absolute():
            p = Path(settings.asset_storage_dir) / storage
        if p.exists():
            path = str(p)
        else:
            # Still pass storage path for staging copy attempt
            path = str(p) if p.suffix else None
            if not (Path(path).exists() if path else False):
                path = None
    return path, url


async def run_distribution(
    *,
    job_id: UUID,
    job_row: dict,
    final_asset: dict | None,
) -> dict[str, Any]:
    """Run staging export + configured distributors.

    Caller MUST ensure job status is ``approved``.
    Sets public_post True only if at least one live distributor succeeded with public visibility.
    Marks platform_captions[*].published carefully (only platforms that actually went live).
    """
    meta = _parse(job_row.get("meta")) or {}
    script = _parse(job_row.get("script")) or {}
    captions = dict(meta.get("platform_captions") or {})
    platforms = list(captions.keys()) or list(settings.schedule_platforms_list)

    video_path, video_url = _resolve_local_video(final_asset)
    staging_path = export_package(
        job_id=str(job_id),
        platforms=platforms,
        caption_bundle=captions,
        video_url=video_url,
        video_storage_path=(final_asset or {}).get("storage_path"),
        script=script,
        extra={"status_at_distribute": job_row.get("status")},
    )

    # Prefer shorts/youtube caption text for YouTube title/description
    yt_cap = captions.get("shorts") or captions.get("youtube") or captions.get("tiktok") or {}
    title = (script.get("hook") or yt_cap.get("caption") or f"Short {job_id}")[:100]
    description = yt_cap.get("caption") or script.get("full_text") or job_row.get("script_text") or ""
    tags = list(yt_cap.get("hashtags") or ["shorts", "vertical"])

    results: list[DistributeResult] = []
    for dist in get_distributors():
        # Map distributor → caption key for published flags
        result = await dist.publish(
            job_id=str(job_id),
            title=title,
            description=description,
            tags=[str(t).lstrip("#") for t in tags],
            video_path=video_path,
            video_url=video_url,
            caption_meta=yt_cap if isinstance(yt_cap, dict) else {},
        )
        results.append(result)

    any_public = any(r.public_post and r.mode == "live" for r in results)
    any_live = any(r.mode == "live" for r in results)
    any_failed = any(r.mode == "failed" for r in results)

    # Update published flags carefully
    published_flags: dict[str, bool] = {}
    for r in results:
        if r.platform == "youtube" and r.mode == "live" and not r.error:
            # YouTube Shorts maps to captions key "shorts" and optional "youtube"
            published_flags["shorts"] = True
            published_flags["youtube"] = True
    for plat in platforms:
        if plat in published_flags:
            continue
        # Documented-only platforms stay unpublished
        published_flags[plat] = False

    updated_captions = {}
    for plat, cap in captions.items():
        cap = dict(cap) if isinstance(cap, dict) else {"caption": str(cap)}
        cap["published"] = bool(published_flags.get(plat, False))
        cap["staged"] = True
        if plat in ("shorts", "youtube") and published_flags.get(plat):
            yt_res = next((r for r in results if r.platform == "youtube"), None)
            if yt_res and yt_res.external_id:
                cap["external_id"] = yt_res.external_id
                cap["published_via"] = "youtube_data_api"
        updated_captions[plat] = cap

    mode = "live" if any_live else ("failed" if any_failed and not any_live else "staging_only")
    return {
        "job_id": str(job_id),
        "mode": mode,
        "public_post": any_public,
        "staging_path": str(staging_path),
        "results": [r.to_dict() for r in results],
        "platform_captions": updated_captions,
        "documented_only": list(DOCUMENTED_ONLY_PLATFORMS),
        "note": (
            "Live YouTube publish when OAuth env set and local video present; "
            "otherwise staging export only. TikTok/Reels remain documented-only."
        ),
    }


__all__ = [
    "DistributeResult",
    "Distributor",
    "YouTubeDistributor",
    "DOCUMENTED_ONLY_PLATFORMS",
    "export_package",
    "run_distribution",
    "get_distributors",
]
