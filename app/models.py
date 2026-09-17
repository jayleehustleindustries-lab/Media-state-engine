from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, Field, model_validator

Status = Literal[
    'pending', 'script_ready', 'audio_generating', 'audio_ready',
    'rendering', 'rendered', 'staged', 'approved', 'delivered', 'failed',
]
AssetKind = Literal['audio', 'video', 'final', 'video_h', 'final_h']


class JobCreate(BaseModel):
    """Create a job with raw script_text and/or a topic for structured generation."""
    script_text: str | None = Field(default=None, min_length=1)
    topic: str | None = Field(default=None, min_length=1)
    duration_target_seconds: int = Field(default=30, ge=15, le=60)
    cta: str | None = None
    include_horizontal: bool = False
    platforms: list[str] = Field(default_factory=lambda: ['tiktok', 'reels', 'shorts'])
    # Advance to script_ready immediately after structured script is stored
    auto_script_ready: bool = True

    @model_validator(mode='after')
    def _require_text_or_topic(self):
        if not (self.script_text or self.topic):
            raise ValueError('provide script_text or topic')
        return self


class AdvanceRequest(BaseModel):
    to_status: Status
    payload: dict[str, Any] = Field(default_factory=dict)


class AssetCreate(BaseModel):
    kind: AssetKind
    url: str | None = None
    storage_path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    id: UUID
    script_text: str
    status: Status
    created_at: datetime
    updated_at: datetime
    script: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    approved_at: datetime | None = None
    approved_by: str | None = None


class ApproveRequest(BaseModel):
    """Explicit human approval signal — required before any distribution."""
    approved_by: str | None = Field(default=None, description='Operator id or name')
    enqueue_distribute: bool = Field(
        default=True,
        description='Enqueue Phase-3 distribute stub after approve (does not public-post)',
    )
    note: str | None = None


class AudioWebhook(BaseModel):
    job_id: UUID
    url: str | None = None
    storage_path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class RenderWebhook(AudioWebhook):
    pass
