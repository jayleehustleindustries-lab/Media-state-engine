"""YouTube Shorts distributor.

Uses YouTube Data API v3 resumable upload when OAuth credentials exist:
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN

When credentials are missing, returns staging_only (safe no-op — no network call).
Shorts: upload as private/unlisted/public with #Shorts in description; vertical video.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from ...config import settings
from .base import DistributeResult

log = logging.getLogger("media_state.distribute.youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


class YouTubeDistributor:
    name = "youtube"

    def configured(self) -> bool:
        return bool(
            (settings.youtube_client_id or "").strip()
            and (settings.youtube_client_secret or "").strip()
            and (settings.youtube_refresh_token or "").strip()
        )

    async def _access_token(self) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": settings.youtube_client_id,
                    "client_secret": settings.youtube_client_secret,
                    "refresh_token": settings.youtube_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise RuntimeError("YouTube OAuth refresh returned no access_token")
            return token

    async def publish(
        self,
        *,
        job_id: str,
        title: str,
        description: str,
        tags: list[str],
        video_path: str | None,
        video_url: str | None,
        caption_meta: dict[str, Any],
    ) -> DistributeResult:
        if not self.configured():
            return DistributeResult(
                platform=self.name,
                mode="staging_only",
                public_post=False,
                detail={
                    "reason": "missing YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN",
                    "next": "export staging files + manual Shorts upload, or set OAuth env vars",
                },
            )

        if not video_path or not Path(video_path).is_file():
            # Live API needs local bytes; URL-only assets stay staging
            return DistributeResult(
                platform=self.name,
                mode="staging_only",
                public_post=False,
                detail={
                    "reason": "no local video file for resumable upload",
                    "video_url": video_url,
                    "hint": "persist HeyGen MP4 bytes before live YouTube upload",
                },
            )

        privacy = (settings.youtube_privacy_status or "private").strip().lower()
        if privacy not in ("private", "unlisted", "public"):
            privacy = "private"

        # Always append #Shorts for Shorts shelf eligibility
        desc = description or ""
        if "#shorts" not in desc.lower():
            desc = (desc + "\n\n#Shorts").strip()

        started = time.perf_counter()
        try:
            token = await self._access_token()
            video_id = await self._resumable_upload(
                token=token,
                path=Path(video_path),
                title=(title or f"Short {job_id}")[:100],
                description=desc[:5000],
                tags=tags[:15],
                privacy=privacy,
            )
            latency = (time.perf_counter() - started) * 1000.0
            # public_post only when privacy is public AND upload succeeded
            public_post = privacy == "public"
            return DistributeResult(
                platform=self.name,
                mode="live",
                public_post=public_post,
                external_id=video_id,
                latency_ms=round(latency, 2),
                detail={
                    "privacy_status": privacy,
                    "watch_url": f"https://youtu.be/{video_id}",
                    "shorts_hint": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            latency = (time.perf_counter() - started) * 1000.0
            log.exception("YouTube upload failed job_id=%s", job_id)
            return DistributeResult(
                platform=self.name,
                mode="failed",
                public_post=False,
                latency_ms=round(latency, 2),
                error=str(exc)[:1000],
            )

    async def _resumable_upload(
        self,
        *,
        token: str,
        path: Path,
        title: str,
        description: str,
        tags: list[str],
        privacy: str,
    ) -> str:
        size = path.stat().st_size
        metadata = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": "22",  # People & Blogs — common for Shorts
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/*",
        }
        params = {
            "uploadType": "resumable",
            "part": "snippet,status",
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            init = await client.post(UPLOAD_URL, params=params, headers=headers, json=metadata)
            init.raise_for_status()
            upload_url = init.headers.get("location")
            if not upload_url:
                raise RuntimeError("YouTube resumable init missing Location header")

            data = path.read_bytes()
            put = await client.put(
                upload_url,
                content=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/*",
                    "Content-Length": str(size),
                },
            )
            put.raise_for_status()
            body = put.json()
            video_id = body.get("id")
            if not video_id:
                raise RuntimeError(f"YouTube upload response missing id: {body}")
            return video_id
