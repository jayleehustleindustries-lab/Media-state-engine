from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, Field

Status = Literal['pending','script_ready','audio_generating','audio_ready','rendering','rendered','delivered','failed']
AssetKind = Literal['audio','video','final']

class JobCreate(BaseModel):
    script_text: str = Field(min_length=1)
class AdvanceRequest(BaseModel):
    to_status: Status
    payload: dict[str, Any] = Field(default_factory=dict)
class AssetCreate(BaseModel):
    kind: AssetKind
    url: str | None = None
    storage_path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
class JobOut(BaseModel):
    id: UUID; script_text: str; status: Status; created_at: datetime; updated_at: datetime
class AudioWebhook(BaseModel):
    job_id: UUID
    url: str | None = None
    storage_path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
class RenderWebhook(AudioWebhook):
    pass
