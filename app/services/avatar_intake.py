"""Strict, URL-based photo intake for HeyGen Photo Avatars.

The provider only accepts JPEG and PNG source assets.  This module receives a
public HTTPS image URL, validates its safety and fidelity, then uploads a
lossless PNG normalisation to HeyGen before creating the Photo Avatar.  It
never resizes, crops, retouches, or alters facial geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

try:  # HEIC / HEIF camera originals
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - requirements install this in production
    pass

try:  # AVIF browser and mobile originals
    import pillow_avif  # noqa: F401
except ImportError:  # pragma: no cover - requirements install this in production
    pass

from ..config import settings
from .heygen import HeyGenAuthenticationError, HeyGenNotConfigured, HeyGenRequestError, _data, _headers, _raise_for_provider, _require_config


MAX_IMAGE_BYTES = 32 * 1024 * 1024
MIN_SHORT_EDGE = 1080
MIN_MEGAPIXELS = 2.0
MAX_REDIRECTS = 5
USER_AGENT = "Media-State-Engine/1.5 AvatarPhotoIntake"


class AvatarIntakeError(RuntimeError):
    """Base error for a URL-photo intake that is safe to show to an operator."""


class PhotoUrlRejected(AvatarIntakeError):
    """The URL is malformed, non-public, or unsuitable for a server-side fetch."""


class PhotoValidationError(AvatarIntakeError):
    """The fetched bytes are not a usable photo."""


class PhotoQualityBlocked(AvatarIntakeError):
    """The photo is valid but does not meet the strict fidelity floor."""


@dataclass(frozen=True)
class NormalizedPhoto:
    """Lossless PNG bytes and the source characteristics used for preflight."""

    content: bytes
    filename: str
    mime_type: str
    source_format: str
    width: int
    height: int
    source_bytes: int
    output_bytes: int
    orientation_corrected: bool

    @property
    def megapixels(self) -> float:
        return round((self.width * self.height) / 1_000_000, 2)

    @property
    def short_edge(self) -> int:
        return min(self.width, self.height)

    @property
    def aspect_ratio(self) -> float:
        return round(self.width / self.height, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_format": self.source_format.lower(),
            "normalized_format": "png",
            "width": self.width,
            "height": self.height,
            "short_edge": self.short_edge,
            "megapixels": self.megapixels,
            "aspect_ratio": self.aspect_ratio,
            "source_bytes": self.source_bytes,
            "normalized_bytes": self.output_bytes,
            "orientation_corrected": self.orientation_corrected,
            "preserved_geometry": True,
            "transformations": [
                "EXIF orientation correction" if self.orientation_corrected else "no geometric transformation",
                "lossless PNG normalization",
            ],
        }


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


def _validate_public_https_url(value: str) -> httpx.URL:
    """Reject obvious SSRF targets before requesting an operator-provided link."""
    try:
        url = httpx.URL(value)
    except Exception as exc:
        raise PhotoUrlRejected("Photo link is not a valid URL.") from exc
    if url.scheme != "https":
        raise PhotoUrlRejected("Photo links must use HTTPS.")
    if not url.host or url.userinfo:
        raise PhotoUrlRejected("Photo link must have a public HTTPS host and cannot include credentials.")
    if _is_public_ip(url.host):
        return url
    try:
        resolved = socket.getaddrinfo(url.host, url.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PhotoUrlRejected("Photo link host could not be resolved.") from exc
    addresses = {item[4][0] for item in resolved}
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise PhotoUrlRejected("Photo link must resolve only to public internet addresses.")
    return url


async def _fetch_public_photo(source_url: str) -> tuple[bytes, str]:
    """Fetch a small public image while validating every HTTPS redirect target."""
    current_url = str(_validate_public_https_url(source_url))
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*"}
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                async with client.stream("GET", current_url, headers=headers) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise PhotoValidationError("Photo host sent an invalid redirect.")
                        current_url = str(_validate_public_https_url(urljoin(current_url, location)))
                        continue
                    if response.status_code >= 400:
                        raise PhotoValidationError(f"Photo host returned HTTP {response.status_code}.")
                    declared_size = response.headers.get("content-length")
                    if declared_size and int(declared_size) > MAX_IMAGE_BYTES:
                        raise PhotoValidationError("Photo is larger than HeyGen's 32 MB image limit.")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise PhotoValidationError("Photo is larger than HeyGen's 32 MB image limit.")
                        chunks.append(chunk)
                    if not chunks:
                        raise PhotoValidationError("Photo link returned no image bytes.")
                    return b"".join(chunks), current_url
            except httpx.HTTPError as exc:
                raise PhotoValidationError("Photo could not be downloaded from the supplied link.") from exc
    raise PhotoValidationError(f"Photo link exceeded the maximum of {MAX_REDIRECTS} redirects.")


def _normalize_photo(content: bytes, source_url: str) -> NormalizedPhoto:
    """Decode common camera/web formats and produce PNG without resampling or crop."""
    try:
        with Image.open(BytesIO(content)) as source:
            source_format = (source.format or "unknown").upper()
            original_orientation = source.getexif().get(274, 1)
            source.load()
            image = ImageOps.exif_transpose(source)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            elif image.mode == "RGBA":
                image = image.copy()
            else:
                image = image.copy()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PhotoValidationError(
            "Photo could not be decoded. Use a valid JPG, PNG, WebP, HEIC/HEIF, AVIF, TIFF, BMP, or GIF image link."
        ) from exc

    if image.width < 1 or image.height < 1:
        raise PhotoValidationError("Photo has invalid dimensions.")

    output = BytesIO()
    try:
        # PNG is lossless and keeps source pixels intact. No resize, crop, face
        # enhancement, background removal, or perspective transformation occurs.
        image.save(output, format="PNG", optimize=True)
    except OSError as exc:
        raise PhotoValidationError("Photo could not be normalized safely.") from exc
    normalized = output.getvalue()
    if len(normalized) > MAX_IMAGE_BYTES:
        raise PhotoValidationError(
            "Lossless normalization exceeds HeyGen's 32 MB limit. Use a smaller original rather than a lossy resize."
        )

    return NormalizedPhoto(
        content=normalized,
        filename=f"avatar-source-{uuid4().hex}.png",
        mime_type="image/png",
        source_format=source_format,
        width=image.width,
        height=image.height,
        source_bytes=len(content),
        output_bytes=len(normalized),
        orientation_corrected=original_orientation not in (None, 1),
    )


def _quality_result(photo: NormalizedPhoto) -> dict[str, Any]:
    blockers: list[str] = []
    advisories: list[str] = []
    if photo.short_edge < MIN_SHORT_EDGE:
        blockers.append(
            f"Short edge is {photo.short_edge}px; use at least {MIN_SHORT_EDGE}px for high-fidelity facial detail."
        )
    if photo.megapixels < MIN_MEGAPIXELS:
        blockers.append(f"Image is {photo.megapixels} MP; use at least {MIN_MEGAPIXELS:.1f} MP.")
    if photo.aspect_ratio > 1.9 or photo.aspect_ratio < 0.5:
        advisories.append("Extreme aspect ratio may crop poorly in a portrait avatar; a vertical 4:5 to 9:16 frame is preferred.")
    if photo.width > photo.height:
        advisories.append("Landscape images work, but a vertical portrait preserves more face and torso depth for avatar video.")
    return {
        "passed": not blockers,
        "blockers": blockers,
        "advisories": advisories,
        "manual_checks": [
            "One person only; face is large, centered, and front-facing (or a deliberate clean 3/4 view).",
            "Eyes, eyebrows, nose, mouth, jawline, and hairline are unobstructed; no sunglasses, face mask, or forehead-covering hat.",
            "Even soft key light with natural skin tone; avoid hard under-lighting, colored club lighting, beauty filters, or heavy HDR.",
            "Use a neutral expression for the primary image; keep the camera at eye level and avoid wide-angle/edge distortion.",
            "Keep shoulders and upper torso visible for depth and believable gestures; do not use a close selfie crop.",
            "Confirm the image faithfully represents the person today and that you have their permission to create the avatar.",
        ],
    }


async def preflight_photo_url(source_url: str) -> tuple[NormalizedPhoto, dict[str, Any]]:
    """Download, normalize, and assess one source link without making a HeyGen mutation."""
    raw, final_url = await _fetch_public_photo(source_url)
    photo = _normalize_photo(raw, final_url)
    result = _quality_result(photo)
    result["photo"] = photo.as_dict()
    result["final_url_host"] = httpx.URL(final_url).host
    return photo, result


async def _upload_asset(photo: NormalizedPhoto, idempotency_key: str) -> dict[str, Any]:
    api_key, base_url = _require_config()
    files = {"file": (photo.filename, photo.content, photo.mime_type)}
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "Idempotency-Key": idempotency_key}
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(f"{base_url}/v3/assets", headers=headers, files=files)
    _raise_for_provider(response)
    data = _data(response.json())
    if not data.get("asset_id"):
        raise HeyGenRequestError("HeyGen upload response did not include asset_id", body=data)
    return data


async def create_photo_avatar(
    *,
    name: str,
    primary_photo: NormalizedPhoto,
    avatar_group_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Upload a normalized image and create one HeyGen Photo Avatar look."""
    api_key, base_url = _require_config()
    key = idempotency_key or f"avatar-photo-{uuid4()}"
    asset = await _upload_asset(primary_photo, f"{key}:asset")
    payload: dict[str, Any] = {
        "type": "photo",
        "name": name,
        "file": {"type": "asset_id", "asset_id": asset["asset_id"]},
    }
    if avatar_group_id:
        payload["avatar_group_id"] = avatar_group_id
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{base_url}/v3/avatars",
            headers=_headers(api_key, key),
            json=payload,
        )
    _raise_for_provider(response)
    data = _data(response.json())
    avatar_item = data.get("avatar_item") or data.get("avatar") or data
    avatar_id = avatar_item.get("id") if isinstance(avatar_item, dict) else None
    if not avatar_id:
        raise HeyGenRequestError("HeyGen avatar response did not include avatar_item.id", body=data)
    return {
        "avatar_id": str(avatar_id),
        "avatar_group_id": avatar_item.get("group_id") or data.get("avatar_group_id"),
        "status": avatar_item.get("status") or data.get("status") or "processing",
        "preview_image_url": avatar_item.get("preview_image_url"),
        "supported_api_engines": avatar_item.get("supported_api_engines", []),
        "provider_asset_id": asset["asset_id"],
        "provider_asset_url": asset.get("url"),
    }


__all__ = [
    "AvatarIntakeError",
    "HeyGenAuthenticationError",
    "HeyGenNotConfigured",
    "PhotoQualityBlocked",
    "PhotoUrlRejected",
    "PhotoValidationError",
    "NormalizedPhoto",
    "create_photo_avatar",
    "preflight_photo_url",
]
