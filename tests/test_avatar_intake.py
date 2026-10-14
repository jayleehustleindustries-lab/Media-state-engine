from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from app.services import avatar_intake


def image_bytes(width: int = 1600, height: int = 2000, image_format: str = "PNG") -> bytes:
    image = Image.new("RGB", (width, height), color=(96, 122, 151))
    stream = BytesIO()
    image.save(stream, format=image_format)
    return stream.getvalue()


def test_normalization_preserves_pixels_and_geometry():
    source = image_bytes()
    photo = avatar_intake._normalize_photo(source, "https://example.com/person.webp")

    assert photo.mime_type == "image/png"
    assert photo.width == 1600
    assert photo.height == 2000
    assert photo.short_edge == 1600
    assert photo.megapixels == 3.2
    assert photo.as_dict()["preserved_geometry"] is True
    with Image.open(BytesIO(photo.content)) as normalized:
        assert normalized.format == "PNG"
        assert normalized.size == (1600, 2000)


def test_quality_blocks_a_low_resolution_photo():
    photo = avatar_intake._normalize_photo(image_bytes(600, 800), "https://example.com/small.jpg")
    result = avatar_intake._quality_result(photo)

    assert result["passed"] is False
    assert any("Short edge" in blocker for blocker in result["blockers"])


def test_private_and_non_https_photo_targets_are_rejected():
    with pytest.raises(avatar_intake.PhotoUrlRejected):
        avatar_intake._validate_public_https_url("http://example.com/avatar.jpg")
    with pytest.raises(avatar_intake.PhotoUrlRejected):
        avatar_intake._validate_public_https_url("https://127.0.0.1/avatar.jpg")
    with pytest.raises(avatar_intake.PhotoUrlRejected):
        avatar_intake._validate_public_https_url("https://169.254.169.254/latest/meta-data")


@pytest.mark.asyncio
async def test_create_photo_avatar_uses_uploaded_asset_and_primary_only(monkeypatch):
    photo = avatar_intake._normalize_photo(image_bytes(), "https://example.com/portrait.png")
    monkeypatch.setattr(avatar_intake, "_require_config", lambda: ("test-key", "https://api.heygen.test"))
    monkeypatch.setattr(
        avatar_intake,
        "_upload_asset",
        AsyncMock(return_value={"asset_id": "asset_123", "url": "https://files.heygen.test/asset_123.png"}),
    )

    class Response:
        is_success = True
        status_code = 200

        def json(self):
            return {"data": {"avatar_item": {"id": "look_123", "group_id": "group_123", "status": "processing"}}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            assert url == "https://api.heygen.test/v3/avatars"
            assert kwargs["json"] == {
                "type": "photo",
                "name": "Jordan — Studio Anchor",
                "file": {"type": "asset_id", "asset_id": "asset_123"},
                "avatar_group_id": "group_123",
            }
            return Response()

    with patch("app.services.avatar_intake.httpx.AsyncClient", return_value=Client()):
        result = await avatar_intake.create_photo_avatar(
            name="Jordan — Studio Anchor",
            primary_photo=photo,
            avatar_group_id="group_123",
            idempotency_key="test-avatar-key",
        )

    assert result["avatar_id"] == "look_123"
    assert result["provider_asset_id"] == "asset_123"
