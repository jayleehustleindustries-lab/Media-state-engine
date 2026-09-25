"""Authenticated Avatar Intake API for HeyGen Photo Avatar creation."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator
from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_api_key
from ..services import avatar_intake
from ..services.heygen import HeyGenError

router = APIRouter(tags=["avatar-intake"])


class AvatarPhotoIntakeRequest(BaseModel):
    """A primary photo plus optional supporting references for an avatar operator."""

    name: str = Field(min_length=2, max_length=120, description="Display name for the new HeyGen Photo Avatar")
    primary_photo_url: str = Field(min_length=8, max_length=4096, description="Public HTTPS URL for the single primary photo")
    reference_photo_urls: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Optional supporting URLs for human review and future Looks; they do not replace the primary photo.",
    )
    avatar_group_id: str | None = Field(default=None, max_length=255)
    confirmed_likeness_rights: bool = Field(
        description="Must be true: operator has the person's permission and rights to create this avatar."
    )

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("primary_photo_url", "reference_photo_urls", mode="after")
    @classmethod
    def reject_blank_urls(cls, value: str | list[str]):
        if isinstance(value, list):
            cleaned = [entry.strip() for entry in value if entry.strip()]
            if len(cleaned) != len(value):
                raise ValueError("reference_photo_urls cannot contain blank entries")
            return cleaned
        if not value.strip():
            raise ValueError("primary_photo_url cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def unique_urls_and_consent(self):
        all_urls = [self.primary_photo_url, *self.reference_photo_urls]
        if len({entry.casefold() for entry in all_urls}) != len(all_urls):
            raise ValueError("each photo URL may be supplied only once")
        if not self.confirmed_likeness_rights:
            raise ValueError("confirmed_likeness_rights must be true")
        return self


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, avatar_intake.PhotoQualityBlocked):
        return HTTPException(422, str(exc))
    if isinstance(exc, avatar_intake.AvatarIntakeError):
        return HTTPException(422, str(exc))
    if isinstance(exc, HeyGenError):
        return HTTPException(502, str(exc))
    return HTTPException(500, "Avatar intake failed unexpectedly.")


async def _preflight_all(request: AvatarPhotoIntakeRequest):
    primary_photo, primary = await avatar_intake.preflight_photo_url(request.primary_photo_url)
    references = []
    for source_url in request.reference_photo_urls:
        _, result = await avatar_intake.preflight_photo_url(source_url)
        references.append(result)
    return primary_photo, primary, references


@router.post("/avatars/photo/preflight", dependencies=[Depends(require_api_key)])
async def preflight(request: AvatarPhotoIntakeRequest):
    """Run a non-mutating photo quality and format gate before creating an avatar."""
    try:
        _, primary, references = await _preflight_all(request)
        return {
            "primary": primary,
            "references": references,
            "can_create": primary["passed"],
            "provider_action": "none",
            "note": "Preflight validates pixels and source quality only. It does not create or upload an avatar.",
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/avatars/photo", status_code=201, dependencies=[Depends(require_api_key)])
async def create_photo_avatar_endpoint(request: AvatarPhotoIntakeRequest):
    """Create one Photo Avatar using only the primary image after strict preflight passes."""
    try:
        primary_photo, primary, references = await _preflight_all(request)
        if not primary["passed"]:
            raise avatar_intake.PhotoQualityBlocked(
                "Primary photo did not pass the strict fidelity gate: " + "; ".join(primary["blockers"])
            )
        provider = await avatar_intake.create_photo_avatar(
            name=request.name,
            primary_photo=primary_photo,
            avatar_group_id=request.avatar_group_id,
        )
        return {
            "avatar": provider,
            "primary_preflight": primary,
            "reference_preflight": references,
            "manual_review_required": True,
            "next_step": "Wait for HeyGen avatar status to become completed, then use avatar.avatar_id with /jobs/{job_id}/generate-avatar.",
            "privacy": "Source URLs and original image bytes are not persisted by this API; HeyGen retains the normalized source asset under its workspace policy.",
        }
    except Exception as exc:
        raise _translate_error(exc) from exc
