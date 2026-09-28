from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.services import gemini_transcribe, voice_router


@pytest.fixture
def client():
    with patch("app.main.connect", new_callable=AsyncMock), patch("app.main.close", new_callable=AsyncMock):
        from app.main import app
        with TestClient(app) as test_client:
            yield test_client


JOB_COMMAND_TEST_KEY = "jc-test-key-distinct-from-mse"


@pytest.fixture
def job_command_on(monkeypatch):
    """PR21-F2: Job Command routes are flag-gated and use their own API key."""
    from app.config import settings

    monkeypatch.setattr(settings, "job_command_voice_enabled", True)
    monkeypatch.setattr(settings, "job_command_api_key", JOB_COMMAND_TEST_KEY)
    return JOB_COMMAND_TEST_KEY


def test_router_uses_only_configured_language_profile():
    route = voice_router.route_job_command(
        requested_language="es-MX",
        default_language="en",
        supported_languages=["en", "es", "pt-BR"],
    )
    assert route.profile_code == "job-command"
    assert route.language == "es"
    assert "public_figure likeness or voice imitation" in route.presentation_profile["exclude"]


def test_router_rejects_unsupported_language():
    with pytest.raises(voice_router.VoiceRouteError):
        voice_router.route_job_command(
            requested_language="fr",
            default_language="en",
            supported_languages=["en", "es"],
        )


def test_gemini_modes_remain_provider_valid():
    diarized = gemini_transcribe.build_transcription_config(mode="diarized", custom_vocabulary=["Job Command"])
    assert diarized["transcription_config"]["mode"]["diarization_mode"] == "speaker"
    assert "custom_vocabulary" not in diarized["transcription_config"]

    smart = gemini_transcribe.build_transcription_config(mode="smart", custom_vocabulary=["Job Command"])
    assert smart["transcription_config"]["mode"]["type"] == "smart"
    assert smart["transcription_config"]["custom_vocabulary"] == ["Job Command"]


@pytest.fixture
async def voice_pg_pool(require_database_url):
    import asyncpg
    from app import db as dbmod
    from tests.conftest import apply_schema, truncate_app_tables

    await dbmod.close()
    pool = await asyncpg.create_pool(require_database_url, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await apply_schema(conn)
        await truncate_app_tables(conn)
    dbmod._pool = pool
    yield pool
    await pool.close()
    dbmod._pool = None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_session_issues_ephemeral_provider_url_without_persisting_it(voice_pg_pool, monkeypatch):
    from app.config import settings
    from app.services import voice_sessions

    monkeypatch.setattr(settings, "job_command_operator_authorized", True)
    monkeypatch.setattr(settings, "job_command_owner_voice_ref", "owner-voice-v1")
    monkeypatch.setattr(settings, "job_command_owner_avatar_ref", "owner-avatar-v1")
    monkeypatch.setattr(settings, "job_command_default_language", "en")
    monkeypatch.setattr(settings, "job_command_supported_languages", "en,es,pt-BR")
    monkeypatch.setattr(settings, "job_command_asr_keywords", "Job Command,acme")
    monkeypatch.setattr(settings, "elevenlabs_agent_id", "agent_private_123")
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    monkeypatch.setattr(
        voice_sessions.elevenlabs_agents,
        "create_signed_url",
        AsyncMock(return_value={
            "signed_url": "wss://api.elevenlabs.test/conversation?token=opaque",
            "agent_id": "agent_private_123",
            "expires_at": expires,
        }),
    )

    issued = await voice_sessions.create_session(
        visitor_ref="user:42", requested_language="es-MX", surface="web", archive_consent=True
    )

    assert issued["session"]["resolved_language"] == "es"
    assert issued["connection"]["signed_url"].startswith("wss://")
    assert issued["connection"]["conversation_init"]["conversation_config_override"]["agent"]["language"] == "es"
    assert issued["campaign"]["profile"] == "job-command"

    async with voice_pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM voice_sessions WHERE id=$1", uuid4() if False else issued["session"]["id"])
        profile = await conn.fetchrow("SELECT * FROM voice_agent_profiles WHERE code='job-command'")
    assert row is not None
    assert "signed_url" not in str(row["metadata"])
    assert row["archive_consent"] is True
    assert profile["owner_voice_ref"] == "owner-voice-v1"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_verified_transcription_event_is_idempotent_and_stores_turns(voice_pg_pool, monkeypatch):
    from app.config import settings
    from app.services import voice_sessions

    monkeypatch.setattr(settings, "job_command_operator_authorized", True)
    monkeypatch.setattr(settings, "elevenlabs_agent_id", "agent_private_123")
    monkeypatch.setattr(settings, "job_command_supported_languages", "en,es")
    monkeypatch.setattr(settings, "job_command_default_language", "en")
    monkeypatch.setattr(settings, "job_command_owner_voice_ref", "owner-voice-v1")
    monkeypatch.setattr(settings, "job_command_owner_avatar_ref", "owner-avatar-v1")
    monkeypatch.setattr(
        voice_sessions.elevenlabs_agents,
        "create_signed_url",
        AsyncMock(return_value={
            "signed_url": "wss://api.elevenlabs.test/conversation?token=opaque",
            "agent_id": "agent_private_123",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        }),
    )
    issued = await voice_sessions.create_session(
        visitor_ref="user:42", requested_language="en", surface="web", archive_consent=False
    )
    session_id = issued["session"]["id"]
    event = {
        "type": "post_call_transcription",
        "event_timestamp": 1739537319,
        "data": {
            "agent_id": "agent_private_123",
            "conversation_id": "conv_456",
            "conversation_initiation_client_data": {
                "dynamic_variables": {"job_command_session_id": session_id}
            },
            "transcript": [
                {"role": "user", "message": "I need help in English.", "language": "en"},
                {"role": "agent", "message": "I can help with that.", "language": "en"},
            ],
        },
    }

    first = await voice_sessions.ingest_postcall_event(event)
    second = await voice_sessions.ingest_postcall_event(event)
    assert first["accepted"] is True
    assert first["duplicate"] is False
    assert second["duplicate"] is True

    async with voice_pg_pool.acquire() as conn:
        turns = await conn.fetch("SELECT * FROM voice_transcript_turns WHERE session_id=$1 ORDER BY ordinal", session_id)
        session = await conn.fetchrow("SELECT * FROM voice_sessions WHERE id=$1", session_id)
    assert len(turns) == 2
    assert turns[0]["speaker_role"] == "user"
    assert session["provider_conversation_id"] == "conv_456"
    assert session["state"] == "completed"


def test_voice_session_endpoint_requires_engine_key(client, job_command_on):
    import os

    response = client.post("/voice/sessions", json={})
    assert response.status_code == 401
    # PR21-F2: the shared MSE key is no longer accepted for Job Command routes.
    mse = client.post("/voice/sessions", headers={"X-API-Key": os.environ["MEDIA_ENGINE_API_KEY"]}, json={})
    assert mse.status_code == 401


def test_voice_session_endpoint_maps_mocked_provider_response(client, job_command_on):
    key = job_command_on
    response_payload = {
        "session": {"id": "00000000-0000-0000-0000-000000000123", "resolved_language": "en", "state": "issued"},
        "connection": {"signed_url": "wss://api.elevenlabs.test/token", "expires_at": "2026-01-01T00:00:00+00:00", "conversation_init": {}},
        "campaign": {"profile": "job-command", "language": "en"},
    }
    with patch("app.api.voice.voice_sessions.create_session", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = response_payload
        response = client.post(
            "/voice/sessions",
            headers={"X-API-Key": key},
            json={"visitor_ref": "user:42", "language": "en", "archive_consent": False},
        )
    assert response.status_code == 201
    assert response.json()["connection"]["signed_url"].startswith("wss://")
    mock_create.assert_awaited_once()


def test_voice_webhook_rejects_invalid_provider_signature(client, job_command_on):
    with patch(
        "app.api.voice.elevenlabs_agents.verify_postcall_webhook",
        side_effect=__import__("app.services.elevenlabs_agents", fromlist=["ElevenLabsAgentError"]).ElevenLabsAgentError("Invalid ElevenLabs webhook signature or payload."),
    ):
        response = client.post("/webhooks/elevenlabs/voice", content=b"{}")
    assert response.status_code == 401
