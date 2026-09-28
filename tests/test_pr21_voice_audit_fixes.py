"""PR #21 audit blockers — Job Command voice (F1 consent audio, F2 isolation,
F4 mint limits) plus trivial mediums (F7 stale reclaim, F8/F11 bind guards)."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.services import voice_sessions
from app.services.voice_mint_limits import VoiceMintLimited, fingerprint

JC_KEY = "jc-test-key-distinct-from-mse"
# Recognizable audio bytes: base64 of these must never appear in Postgres.
AUDIO_BYTES = (b"ID3\x04JOBCOMMAND-AUDIO-MARKER" + bytes(range(256))) * 16
AUDIO_B64 = base64.b64encode(AUDIO_BYTES).decode("ascii")


# ---------------------------------------------------------------- fixtures --

@pytest.fixture
def client():
    with patch("app.main.connect", new_callable=AsyncMock), patch("app.main.close", new_callable=AsyncMock):
        from app.main import app
        with TestClient(app) as test_client:
            yield test_client


@pytest.fixture
def jc_on(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_command_voice_enabled", True)
    monkeypatch.setattr(settings, "job_command_api_key", JC_KEY)
    return JC_KEY


@pytest.fixture
async def voice_pg_pool(require_database_url):
    import asyncpg
    from app import db as dbmod
    from tests.conftest import apply_schema, truncate_app_tables

    await dbmod.close()
    pool = await asyncpg.create_pool(require_database_url, min_size=1, max_size=8)
    async with pool.acquire() as conn:
        await apply_schema(conn)
        await truncate_app_tables(conn)
    dbmod._pool = pool
    yield pool
    await pool.close()
    dbmod._pool = None


@pytest.fixture
def voice_env(monkeypatch, tmp_path):
    """Authorized profile, mocked provider (no ElevenLabs call), tmp storage."""
    from app.config import settings

    monkeypatch.setattr(settings, "job_command_operator_authorized", True)
    monkeypatch.setattr(settings, "elevenlabs_agent_id", "agent_private_123")
    monkeypatch.setattr(settings, "job_command_supported_languages", "en,es")
    monkeypatch.setattr(settings, "job_command_default_language", "en")
    monkeypatch.setattr(settings, "job_command_owner_voice_ref", "owner-voice-v1")
    monkeypatch.setattr(settings, "job_command_owner_avatar_ref", "owner-avatar-v1")
    monkeypatch.setattr(settings, "asset_storage_dir", str(tmp_path / "assets"))
    mint = AsyncMock(side_effect=lambda: {
        "signed_url": "wss://api.elevenlabs.test/conversation?token=opaque",
        "agent_id": "agent_private_123",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
    })
    monkeypatch.setattr(voice_sessions.elevenlabs_agents, "create_signed_url", mint)
    return {"mint": mint, "storage": tmp_path / "assets", "settings": settings}


async def _session(consent: bool, visitor: str = "user:42", **kw) -> str:
    issued = await voice_sessions.create_session(
        visitor_ref=visitor, requested_language="en", surface="web", archive_consent=consent, **kw
    )
    return issued["session"]["id"]


def _audio_event(session_id: str | None, conversation_id: str = "conv_audio_1", stamp: int = 1739537400) -> dict:
    data = {"agent_id": "agent_private_123", "conversation_id": conversation_id, "full_audio": AUDIO_B64}
    if session_id:
        data["conversation_initiation_client_data"] = {"dynamic_variables": {"job_command_session_id": session_id}}
    return {"type": "post_call_audio", "event_timestamp": stamp, "data": data}


async def _all_voice_rows_text(conn) -> str:
    chunks = []
    for table in ("voice_session_events", "voice_sessions", "voice_transcript_turns",
                  "voice_transcription_runs", "voice_work_queue", "voice_mint_attempts"):
        rows = await conn.fetch(f"SELECT row_to_json(t)::text AS j FROM {table} t")
        chunks.extend(r["j"] for r in rows)
    return "\n".join(chunks)


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# ------------------------------------------------ F1 (CRITICAL) unit tests --

def test_f1_storage_payload_redacts_full_audio_for_matched_session():
    payload = voice_sessions.storage_payload(_audio_event(str(uuid4())), matched_session=True)
    text = json.dumps(payload)
    assert AUDIO_B64 not in text
    assert AUDIO_B64[:64] not in text
    assert payload["data"]["full_audio"]["redacted"] is True
    assert len(payload["data"]["full_audio"]["sha256"]) == 64
    assert payload["audio_redacted"] is True


def test_f1_unmatched_callback_is_metadata_only_envelope():
    event = _audio_event(None)
    event["data"]["transcript"] = [{"role": "user", "message": "private words"}]
    payload = voice_sessions.storage_payload(event, matched_session=False)
    text = json.dumps(payload)
    assert AUDIO_B64[:64] not in text
    assert "private words" not in text
    assert payload["minimal_envelope"] is True
    assert payload["data"] == {"agent_id": "agent_private_123", "conversation_id": "conv_audio_1"}


def test_f1_redacts_blob_under_unexpected_key_and_nested_lists():
    event = {"type": "post_call_audio", "data": {"extras": [{"weird_field": AUDIO_B64}], "note": "hello world"}}
    redacted = voice_sessions.redact_binary_fields(event)
    assert AUDIO_B64[:64] not in json.dumps(redacted)
    assert redacted["data"]["note"] == "hello world"


# ----------------------------------------------- F1 (CRITICAL) DB tests ----

@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f1_no_audio_persists_anywhere_without_consent(voice_pg_pool, voice_env):
    session_id = await _session(consent=False)
    result = await voice_sessions.ingest_postcall_event(_audio_event(session_id))
    assert result["matched_session"] is True
    assert result["archive_run_id"] is None

    async with voice_pg_pool.acquire() as conn:
        everything = await _all_voice_rows_text(conn)
        event_row = await conn.fetchrow("SELECT payload FROM voice_session_events WHERE session_id=$1", session_id)
        runs = await conn.fetchval("SELECT count(*) FROM voice_transcription_runs")
        queued = await conn.fetchval("SELECT count(*) FROM voice_work_queue")
    assert AUDIO_B64 not in everything
    assert AUDIO_B64[:64] not in everything
    payload = json.loads(event_row["payload"])
    assert payload["data"]["full_audio"]["redacted"] is True
    assert "audio_ref" not in payload
    assert runs == 0 and queued == 0
    assert _files(voice_env["storage"]) == []  # nothing written to disk either


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f1_unmatched_audio_callback_persists_no_audio(voice_pg_pool, voice_env):
    result = await voice_sessions.ingest_postcall_event(_audio_event(None, conversation_id="conv_unknown"))
    assert result["matched_session"] is False
    async with voice_pg_pool.acquire() as conn:
        everything = await _all_voice_rows_text(conn)
        row = await conn.fetchrow("SELECT payload FROM voice_session_events WHERE provider_conversation_id='conv_unknown'")
    assert AUDIO_B64[:64] not in everything
    assert json.loads(row["payload"])["minimal_envelope"] is True
    assert _files(voice_env["storage"]) == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f1_consented_audio_is_stored_by_reference_not_base64(voice_pg_pool, voice_env):
    session_id = await _session(consent=True)
    result = await voice_sessions.ingest_postcall_event(_audio_event(session_id))
    assert result["archive_run_id"]

    async with voice_pg_pool.acquire() as conn:
        everything = await _all_voice_rows_text(conn)
        payload = json.loads(await conn.fetchval(
            "SELECT payload FROM voice_session_events WHERE session_id=$1", session_id))
        queued = await conn.fetchval("SELECT count(*) FROM voice_work_queue WHERE session_id=$1", session_id)
    assert AUDIO_B64[:64] not in everything
    assert payload["audio_ref"].startswith(f"voice/{session_id}/")
    stored = voice_env["storage"] / payload["audio_ref"]
    assert stored.read_bytes() == AUDIO_BYTES  # consented archive keeps the bytes on the storage path
    assert queued == 1


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f1_migration_scrubs_legacy_audio_rows(voice_pg_pool):
    root = Path(__file__).resolve().parents[1]
    migration = (root / "supabase" / "migrations" / "20260317000008_job_command_voice_audit_fixes.sql").read_text()
    legacy = {"type": "post_call_audio", "data": {"conversation_id": "c1", "full_audio": AUDIO_B64,
                                                  "transcript": [{"message": "hi"}]}}
    async with voice_pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO voice_session_events(provider_event_key, provider_event_type, payload) VALUES('k1','post_call_audio',$1::jsonb)",
            json.dumps(legacy),
        )
        await conn.execute(migration)  # idempotent re-apply
        payload = json.loads(await conn.fetchval("SELECT payload FROM voice_session_events WHERE provider_event_key='k1'"))
    assert "full_audio" not in payload["data"]
    assert "transcript" not in payload["data"]  # unmatched row reduced to envelope
    assert payload["audio_redacted"] is True


# ----------------------------------------------------- F2 isolation tests --

def test_f2_voice_routes_are_off_by_default(client):
    from app.config import settings

    assert settings.job_command_voice_enabled is False
    sid = "00000000-0000-0000-0000-000000000123"
    for method, path in (("post", "/voice/sessions"), ("get", f"/voice/sessions/{sid}"),
                         ("post", f"/voice/sessions/{sid}/provider-conversation"),
                         ("post", "/webhooks/elevenlabs/voice")):
        kwargs = {"json": {}} if method == "post" else {}
        response = getattr(client, method)(path, headers={"X-API-Key": JC_KEY}, **kwargs)
        assert response.status_code == 404, path


def test_f2_mse_key_is_rejected_on_voice_routes(client, jc_on):
    with patch("app.api.voice.voice_sessions.create_session", new_callable=AsyncMock) as create:
        for header in ({"X-API-Key": os.environ["MEDIA_ENGINE_API_KEY"]},
                       {"Authorization": f"Bearer {os.environ['MEDIA_ENGINE_API_KEY']}"}, {}):
            response = client.post("/voice/sessions", headers=header, json={"visitor_ref": "user:1"})
            assert response.status_code == 401
    create.assert_not_awaited()


def test_f2_job_command_key_is_rejected_on_mse_routes(client, jc_on):
    response = client.get("/jobs", headers={"X-API-Key": JC_KEY})
    assert response.status_code == 401


def test_f2_missing_job_command_key_fails_closed(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_command_voice_enabled", True)
    monkeypatch.setattr(settings, "job_command_api_key", "")
    response = client.post("/voice/sessions", headers={"X-API-Key": ""}, json={"visitor_ref": "u"})
    assert response.status_code == 503


def test_f2_job_command_key_equal_to_mse_key_fails_closed(client, monkeypatch):
    from app.config import settings

    mse = os.environ["MEDIA_ENGINE_API_KEY"]
    monkeypatch.setattr(settings, "job_command_voice_enabled", True)
    monkeypatch.setattr(settings, "job_command_api_key", mse)
    response = client.post("/voice/sessions", headers={"X-API-Key": mse}, json={"visitor_ref": "u"})
    assert response.status_code == 503
    assert "must differ" in response.json()["detail"]


def test_f2_dependency_rejects_mse_key_even_without_middleware(jc_on):
    from fastapi import HTTPException
    from app.auth import require_job_command_api_key

    with pytest.raises(HTTPException) as exc:
        require_job_command_api_key(credentials=None, x_api_key=os.environ["MEDIA_ENGINE_API_KEY"])
    assert exc.value.status_code == 401
    assert require_job_command_api_key(credentials=None, x_api_key=JC_KEY) == JC_KEY


def test_f2_uses_constant_time_compare():
    import inspect
    from app import auth

    assert "hmac.compare_digest" in inspect.getsource(auth._keys_match)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_f2_worker_runs_voice_archive_only_when_flag_on(monkeypatch, enabled):
    from app import worker
    from app.config import settings

    monkeypatch.setattr(settings, "job_command_voice_enabled", enabled)
    archive = AsyncMock(return_value={"claimed": 0})
    monkeypatch.setattr(worker.voice_sessions, "process_archive_queue", archive)

    class _Tx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(worker, "transaction", lambda: _Tx())
    monkeypatch.setattr(worker.queue, "reclaim_stale_running", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker.queue, "claim_due", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker.outbox, "reclaim_stale_delivering", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker.outbox, "flush_outbox", AsyncMock(return_value={}))
    stats = await worker.tick()
    assert archive.await_count == (1 if enabled else 0)
    assert stats["voice_archive"] == ({"claimed": 0} if enabled else {})


def test_f2_no_cross_pipeline_credential_fallbacks(monkeypatch, tmp_path):
    from app.config import settings
    from app.services import elevenlabs_agents, gemini_transcribe

    monkeypatch.setattr(settings, "elevenlabs_api_key", "mse-tts-key")
    monkeypatch.setattr(settings, "elevenlabs_agents_api_key", "")
    monkeypatch.setattr(settings, "elevenlabs_agent_id", "agent")
    with pytest.raises(elevenlabs_agents.ElevenLabsAgentNotConfigured):
        elevenlabs_agents._configured()

    monkeypatch.setattr(settings, "elevenlabs_webhook_secret", "mse-tts-webhook-secret")
    monkeypatch.setattr(settings, "elevenlabs_agents_webhook_secret", "")
    with pytest.raises(elevenlabs_agents.ElevenLabsAgentError, match="not configured"):
        elevenlabs_agents.verify_postcall_webhook(b"{}", "t=1,v0=abc")

    monkeypatch.setattr(settings, "gemini_api_key", "mse-image-gate-key")
    monkeypatch.setattr(settings, "gemini_transcribe_api_key", "")
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    with pytest.raises(gemini_transcribe.GeminiTranscribeNotConfigured):
        gemini_transcribe._transcribe_sync(str(audio), mode="diarized", custom_vocabulary=[])


# ------------------------------------------------------- F4 mint limits ----

def test_f4_limit_maps_to_429_with_retry_after(client, jc_on):
    with patch("app.api.voice.voice_sessions.create_session", new_callable=AsyncMock) as create:
        create.side_effect = VoiceMintLimited("key", "Too many voice sessions for this API key; slow down.", 60)
        response = client.post("/voice/sessions", headers={"X-API-Key": jc_on}, json={"visitor_ref": "user:1"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_f4_api_passes_fingerprints_not_raw_key(client, jc_on):
    with patch("app.api.voice.voice_sessions.create_session", new_callable=AsyncMock) as create:
        create.return_value = {"session": {}, "connection": {}, "campaign": {}}
        response = client.post("/voice/sessions", headers={"X-API-Key": jc_on}, json={"visitor_ref": "user:1"})
    assert response.status_code == 201
    kwargs = create.await_args.kwargs
    assert kwargs["api_key_fingerprint"] == fingerprint(jc_on)
    assert jc_on not in json.dumps(kwargs)
    assert kwargs["client_ip_fingerprint"] not in {"", "unknown"}


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f4_daily_cap_refuses_before_provider_call(voice_pg_pool, voice_env, monkeypatch):
    s = voice_env["settings"]
    monkeypatch.setattr(s, "job_command_voice_daily_mint_cap", 2)
    await _session(False, visitor="v1")
    await _session(False, visitor="v2")
    with pytest.raises(VoiceMintLimited) as exc:
        await _session(False, visitor="v3")
    assert exc.value.scope == "daily"
    assert voice_env["mint"].await_count == 2
    async with voice_pg_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM voice_sessions") == 2


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize("scope,setting,kw_a,kw_b", [
    ("visitor", "job_command_voice_rate_limit_per_visitor", {"visitor": "same"}, {"visitor": "same"}),
    ("key", "job_command_voice_rate_limit_per_key",
     {"visitor": "a", "api_key_fingerprint": "k1"}, {"visitor": "b", "api_key_fingerprint": "k1"}),
    ("ip", "job_command_voice_rate_limit_per_ip",
     {"visitor": "a", "client_ip_fingerprint": "ip1"}, {"visitor": "b", "client_ip_fingerprint": "ip1"}),
])
async def test_f4_short_window_rate_limits(voice_pg_pool, voice_env, monkeypatch, scope, setting, kw_a, kw_b):
    monkeypatch.setattr(voice_env["settings"], setting, 1)
    a = dict(kw_a)
    b = dict(kw_b)
    await _session(False, visitor=a.pop("visitor"), **a)
    with pytest.raises(VoiceMintLimited) as exc:
        await _session(False, visitor=b.pop("visitor"), **b)
    assert exc.value.scope == scope
    assert voice_env["mint"].await_count == 1


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f4_zero_cap_fails_closed(voice_pg_pool, voice_env, monkeypatch):
    monkeypatch.setattr(voice_env["settings"], "job_command_voice_daily_mint_cap", 0)
    with pytest.raises(VoiceMintLimited):
        await _session(False)
    voice_env["mint"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f4_failed_provider_call_still_consumes_budget(voice_pg_pool, voice_env, monkeypatch):
    from app.services.elevenlabs_agents import ElevenLabsAgentError

    monkeypatch.setattr(voice_env["settings"], "job_command_voice_daily_mint_cap", 1)
    voice_env["mint"].side_effect = ElevenLabsAgentError("boom")
    with pytest.raises(ElevenLabsAgentError):
        await _session(False, visitor="v1")
    with pytest.raises(VoiceMintLimited):
        await _session(False, visitor="v2")
    assert voice_env["mint"].await_count == 1
    async with voice_pg_pool.acquire() as conn:
        outcome = await conn.fetchval("SELECT outcome FROM voice_mint_attempts")
    assert outcome == "failed"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f4_concurrent_mints_never_exceed_cap(voice_pg_pool, voice_env, monkeypatch):
    monkeypatch.setattr(voice_env["settings"], "job_command_voice_daily_mint_cap", 3)
    results = await asyncio.gather(
        *[_session(False, visitor=f"v{i}") for i in range(6)], return_exceptions=True
    )
    ok = [r for r in results if isinstance(r, str)]
    limited = [r for r in results if isinstance(r, VoiceMintLimited)]
    assert len(ok) == 3 and len(limited) == 3
    assert voice_env["mint"].await_count == 3


# ------------------------------------------- F7 / F8+F11 trivial mediums ---

@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f7_stale_running_archive_work_is_reclaimed(voice_pg_pool, voice_env):
    stale_session = await _session(consent=True, visitor="a")
    exhausted_session = await _session(consent=True, visitor="b")
    fresh_session = await _session(consent=True, visitor="c")
    insert = """INSERT INTO voice_work_queue(session_id, step, status, attempts, max_attempts, updated_at)
                VALUES($1,'gemini_archive','running',$2,3, now() - make_interval(secs => $3)) RETURNING id"""
    async with voice_pg_pool.acquire() as conn:
        stale = await conn.fetchval(insert, stale_session, 1, 7200.0)
        exhausted = await conn.fetchval(insert, exhausted_session, 3, 7200.0)
        fresh = await conn.fetchval(insert, fresh_session, 1, 5.0)
        reclaimed = await voice_sessions.reclaim_stale_archive_work(conn, 900)
        statuses = {r["id"]: r["status"] for r in await conn.fetch("SELECT id, status FROM voice_work_queue")}
    assert reclaimed == 2
    assert statuses[stale] == "pending"
    assert statuses[exhausted] == "dead"
    assert statuses[fresh] == "running"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_f11_bind_refuses_overwrite_steal_and_expired(voice_pg_pool, voice_env):
    from uuid import UUID

    first = UUID(await _session(False, visitor="a"))
    second = UUID(await _session(False, visitor="b"))
    bound = await voice_sessions.bind_conversation(first, provider_conversation_id="conv_A")
    assert bound["state"] == "connected"
    # idempotent re-bind of the same id is fine
    await voice_sessions.bind_conversation(first, provider_conversation_id="conv_A")
    with pytest.raises(voice_sessions.VoiceSessionConflict):
        await voice_sessions.bind_conversation(first, provider_conversation_id="conv_B")
    with pytest.raises(voice_sessions.VoiceSessionConflict):
        await voice_sessions.bind_conversation(second, provider_conversation_id="conv_A")
    async with voice_pg_pool.acquire() as conn:
        await conn.execute("UPDATE voice_sessions SET token_expires_at = now() - interval '1 minute' WHERE id=$1", second)
    with pytest.raises(voice_sessions.VoiceSessionConflict):
        await voice_sessions.bind_conversation(second, provider_conversation_id="conv_C")
    with pytest.raises(LookupError):
        await voice_sessions.bind_conversation(uuid4(), provider_conversation_id="conv_D")


def test_f11_bind_conflict_maps_to_409(client, jc_on):
    with patch("app.api.voice.voice_sessions.bind_conversation", new_callable=AsyncMock) as bind:
        bind.side_effect = voice_sessions.VoiceSessionConflict("already bound")
        response = client.post(
            "/voice/sessions/00000000-0000-0000-0000-000000000123/provider-conversation",
            headers={"X-API-Key": jc_on}, json={"provider_conversation_id": "conv"},
        )
    assert response.status_code == 409
