"""Distribution interfaces — no platform posts without approved status."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DistributeResult:
    platform: str
    mode: str  # "live" | "staging_only" | "skipped" | "failed"
    public_post: bool
    external_id: str | None = None
    staging_path: str | None = None
    latency_ms: float | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "mode": self.mode,
            "public_post": self.public_post,
            "external_id": self.external_id,
            "staging_path": self.staging_path,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "detail": self.detail,
        }


class Distributor(Protocol):
    name: str

    def configured(self) -> bool:
        """True when env credentials are present for a live API call."""
        ...

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
        """Upload / publish one vertical video. Must no-op safely when not configured."""
        ...
