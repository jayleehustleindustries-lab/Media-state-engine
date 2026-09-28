"""Durable Job Command voice-session control plane and post-call archive work."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from ..config import settings
from ..db import transaction
from . import storage
from . import elevenlabs_agents, gemini_transcribe, voice_mint_limits, voice_router


class VoiceSessionError(RuntimeError):
    pass


class VoiceSessionConflict(VoiceSessionError):
    """Bind refused: wrong state, expired, or conversation already bound (HTTP 409)."""


MAX_ARCHIVE_AUDIO_BYTES = 32 * 1024 * 1024

# PR21-F1: provider fields that carry raw audio. They are never written to
# Postgres; a redaction summary (size + sha256) replaces them.
AUDIO_FIELD_NAMES = frozenset({
    "full_audio", "audio", "audio_base64", "audio_b64", "audio_data",
    "raw_audio", "audio_chunk", "audio_content", "recording",
})
# Defensive: any long, whitespace-free base64-looking string is treated as a
# binary blob even under an unexpected key name.
_BLOB_MIN_CHARS = 1024
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
# Fields kept in the metadata-only envelope for unmatched/unknown sessions.
_ENVELOPE_DATA_FIELDS = ("agent_id", "conversation_id", "status", "start_time_unix_secs", "call_duration_secs")


def _audio_summary(value: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        summary["encoded_chars"] = len(value)
        summary["sha256"] = sha256(value.encode("utf-8")).hexdigest()
    elif isinstance(value, (bytes, bytearray)):
        summary["bytes"] = len(value)
        summary["sha256"] = sha256(bytes(value)).hexdigest()
    return summary


def _looks_like_blob(value: str) -> bool:
    return len(value) >= _BLOB_MIN_CHARS and bool(_BASE64ISH.match(value))


def redact_binary_fields(value: Any) -> Any:
    """Return a deep copy of ``value`` with every audio/binary field redacted."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in AUDIO_FIELD_NAMES and item is not None:
                out[key] = _audio_summary(item)
            else:
                out[key] = redact_binary_fields(item)
        return out
    if isinstance(value, list):
        return [redact_binary_fields(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return _audio_summary(value)
    if isinstance(value, str) and _looks_like_blob(value):
        return _audio_summary(value)
    return value


def storage_payload(event: dict[str, Any], *, matched_session: bool) -> dict[str, Any]:
    """Build the ONLY payload shape allowed into ``voice_session_events``.

    * Unmatched callbacks: metadata-only envelope (no transcript, no audio).
    * Matched callbacks: full event with audio/binary fields redacted. Audio
      bytes are persisted only on the consented filesystem archive path and
      referenced by ``audio_ref``; raw base64 never reaches Postgres, whether or
      not ``archive_consent`` is true.
    """
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    had_audio = any(isinstance(k, str) and k.lower() in AUDIO_FIELD_NAMES for k in data)
    if not matched_session:
        return {
            "type": event.get("type"),
            "event_timestamp": event.get("event_timestamp"),
            "data": {k: data[k] for k in _ENVELOPE_DATA_FIELDS if k in data and not isinstance(data[k], (dict, list))},
            "minimal_envelope": True,
            "audio_redacted": had_audio,
        }
    payload = redact_binary_fields(event)
    if had_audio:
        payload["audio_redacted"] = True
    return payload


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _parse(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _out(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key in {"metadata", "presentation_policy", "payload", "config"}:
            result[key] = _parse(value)
    return result


def _supported_languages() -> list[str]:
    return [item.strip() for item in (settings.job_command_supported_languages or "en").split(",") if item.strip()]


def _asr_keywords() -> list[str]:
    return [item.strip() for item in (settings.job_command_asr_keywords or "").split(",") if item.strip()][:50]


def _policy() -> dict[str, Any]:
    route = voice_router.route_job_command(
        requested_language=None,
        default_language=settings.job_command_default_language,
        supported_languages=_supported_languages(),
    )
    return route.presentation_profile


async def _ensure_profile(conn) -> dict[str, Any]:
    if not settings.job_command_operator_authorized:
        raise VoiceSessionError(
            "Job Command voice profile is locked until JOB_COMMAND_OPERATOR_AUTHORIZED=true confirms the operator-owned voice and avatar."
        )
    agent_id = (settings.elevenlabs_agent_id or "").strip()
    if not agent_id:
        raise VoiceSessionError("Job Command voice profile needs ELEVENLABS_AGENT_ID.")
    row = await conn.fetchrow(
        """
        INSERT INTO voice_agent_profiles(
          code, provider, provider_agent_id, owner_voice_ref, owner_avatar_ref,
          default_language, supported_languages, presentation_policy, enabled
        ) VALUES($1, 'elevenlabs', $2, $3, $4, $5, $6::text[], $7::jsonb, true)
        ON CONFLICT (code) DO UPDATE SET
          provider_agent_id = EXCLUDED.provider_agent_id,
          owner_voice_ref = EXCLUDED.owner_voice_ref,
          owner_avatar_ref = EXCLUDED.owner_avatar_ref,
          default_language = EXCLUDED.default_language,
          supported_languages = EXCLUDED.supported_languages,
          presentation_policy = EXCLUDED.presentation_policy,
          enabled = true
        RETURNING *
        """,
        voice_router.JOB_COMMAND_PROFILE,
        agent_id,
        settings.job_command_owner_voice_ref,
        settings.job_command_owner_avatar_ref,
        settings.job_command_default_language,
        _supported_languages(),
        _json(_policy()),
    )
    return dict(row)


async def create_session(
    *,
    visitor_ref: str,
    requested_language: str | None,
    surface: str,
    archive_consent: bool,
    api_key_fingerprint: str = "unknown",
    client_ip_fingerprint: str = "unknown",
) -> dict[str, Any]:
    """Issue an application session followed by an ephemeral provider credential.

    PR21-F4: a mint reservation (rate limits + daily cap) is taken in the same
    transaction as the session row, before any provider call. Over-limit raises
    ``voice_mint_limits.VoiceMintLimited`` and ElevenLabs is never contacted.

    The signed URL stays in the response only and is never written to Postgres.
    Its connection options are returned separately so a Vercel client can hand
    them to the official ElevenLabs client SDK when starting the conversation.
    """
    if surface not in {"web", "phone"}:
        raise VoiceSessionError("surface must be 'web' or 'phone'.")
    route = voice_router.route_job_command(
        requested_language=requested_language,
        default_language=settings.job_command_default_language,
        supported_languages=_supported_languages(),
    )
    # Persist a conservative expiration before network I/O so the lifecycle row
    # is valid even when ElevenLabs is temporarily unavailable.  The provider's
    # response replaces this value after the signed URL is issued.
    provisional_expiry = datetime.now(timezone.utc) + timedelta(
        seconds=settings.voice_session_token_ttl_seconds
    )
    async with transaction() as conn:
        attempt_id = await voice_mint_limits.reserve_mint(
            conn,
            api_key_fingerprint=api_key_fingerprint,
            client_ip_fingerprint=client_ip_fingerprint,
            visitor_ref=visitor_ref,
        )
        profile = await _ensure_profile(conn)
        row = await conn.fetchrow(
            """
            INSERT INTO voice_sessions(
              profile_id, visitor_ref, requested_language, resolved_language,
              surface, provider_agent_id, token_expires_at, archive_consent, metadata
            ) VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            RETURNING *
            """,
            profile["id"],
            visitor_ref,
            requested_language,
            route.language,
            surface,
            profile["provider_agent_id"],
            provisional_expiry,
            archive_consent,
            _json({"profile": route.presentation_profile, "supported_languages": list(route.supported_languages)}),
        )
        session = dict(row)
    try:
        provider = await elevenlabs_agents.create_signed_url()
    except Exception:
        async with transaction() as conn:
            await conn.execute("UPDATE voice_sessions SET state='failed' WHERE id=$1", session["id"])
            await voice_mint_limits.record_outcome(conn, attempt_id, outcome="failed", session_id=session["id"])
        raise
    async with transaction() as conn:
        await conn.execute(
            "UPDATE voice_sessions SET token_expires_at=$2 WHERE id=$1",
            session["id"],
            provider["expires_at"],
        )
        await voice_mint_limits.record_outcome(conn, attempt_id, outcome="minted", session_id=session["id"])
    session_id = str(session["id"])
    return {
        "session": _out({**session, "token_expires_at": provider["expires_at"]}),
        "connection": {
            "signed_url": provider["signed_url"],
            "expires_at": provider["expires_at"].isoformat(),
            "conversation_init": route.conversation_init(
                session_id=session_id,
                visitor_ref=visitor_ref,
                asr_keywords=_asr_keywords(),
            ),
        },
        "campaign": {
            "profile": route.profile_code,
            "language": route.language,
            "supported_languages": list(route.supported_languages),
            "presentation_policy": route.presentation_profile,
        },
    }


async def bind_conversation(session_id: UUID, *, provider_conversation_id: str) -> dict[str, Any]:
    """Bind a client-started provider conversation for later signed callbacks.

    The Vercel application must authorize its signed-in user before calling this
    bridge.  This engine endpoint is API-key protected; it is not a substitute
    for end-user authorization in the public frontend.
    """
    value = provider_conversation_id.strip()
    if not value or len(value) > 255:
        raise VoiceSessionError("provider_conversation_id must be 1–255 characters.")
    # PR21-F8/F11: only bind a live (issued/connected), unexpired session, and
    # never overwrite a different conversation id or steal one already bound.
    try:
        async with transaction() as conn:
            row = await conn.fetchrow(
                """
                UPDATE voice_sessions
                SET provider_conversation_id=$2,
                    state=CASE WHEN state='issued' THEN 'connected' ELSE state END,
                    connected_at=COALESCE(connected_at, now())
                WHERE id=$1
                  AND state IN ('issued','connected')
                  AND token_expires_at > now()
                  AND (provider_conversation_id IS NULL OR provider_conversation_id=$2)
                RETURNING *
                """,
                session_id,
                value,
            )
            if not row:
                existing = await conn.fetchrow("SELECT id FROM voice_sessions WHERE id=$1", session_id)
                if not existing:
                    raise LookupError("voice session not found")
                raise VoiceSessionConflict(
                    "voice session cannot be bound: it is not live, has expired, or is bound to another conversation."
                )
    except asyncpg.UniqueViolationError as exc:
        raise VoiceSessionConflict("provider conversation is already bound to another voice session.") from exc
    return _out(row) or {}


async def get_session(session_id: UUID) -> dict[str, Any] | None:
    async with transaction() as conn:
        row = await conn.fetchrow("SELECT * FROM voice_sessions WHERE id=$1", session_id)
    return _out(row)


def _session_from_event(event: dict[str, Any]) -> tuple[str | None, str | None]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    conversation_id = data.get("conversation_id")
    init = data.get("conversation_initiation_client_data")
    dynamic = init.get("dynamic_variables") if isinstance(init, dict) and isinstance(init.get("dynamic_variables"), dict) else {}
    session_id = dynamic.get("job_command_session_id")
    return str(session_id) if session_id else None, str(conversation_id) if conversation_id else None


def _event_key(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "unknown")
    _, conversation_id = _session_from_event(event)
    stamp = str(event.get("event_timestamp") or "")
    if not conversation_id or not stamp:
        raise VoiceSessionError("ElevenLabs post-call event lacks conversation_id or event_timestamp.")
    return f"{event_type}:{conversation_id}:{stamp}"


def _turns(event: dict[str, Any]) -> list[dict[str, Any]]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    transcript = data.get("transcript")
    if not isinstance(transcript, list):
        return []
    turns: list[dict[str, Any]] = []
    for ordinal, item in enumerate(transcript):
        if not isinstance(item, dict):
            continue
        text = item.get("message") or item.get("text") or item.get("transcript") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        raw_role = str(item.get("role") or item.get("source") or "unknown").lower()
        role = "agent" if raw_role in {"agent", "ai"} else "user" if raw_role in {"user", "customer"} else "unknown"
        turns.append({
            "ordinal": ordinal,
            "speaker_role": role,
            "speaker_label": item.get("speaker") or item.get("speaker_id"),
            "language": item.get("language"),
            "text": text.strip(),
            "start_ms": item.get("start_ms") or item.get("start_time_ms"),
            "end_ms": item.get("end_ms") or item.get("end_time_ms"),
            "meta": {k: v for k, v in item.items() if k not in {"message", "text", "transcript"}},
        })
    return turns


async def _queue_archive(conn, *, session_id: UUID, audio_relative_path: str) -> UUID:
    existing = await conn.fetchrow(
        """
        SELECT id FROM voice_transcription_runs
        WHERE session_id=$1 AND provider='gemini' AND status IN ('queued','running','completed')
        ORDER BY created_at DESC LIMIT 1
        """,
        session_id,
    )
    if existing:
        return existing["id"]
    run = await conn.fetchrow(
        """
        INSERT INTO voice_transcription_runs(session_id, provider, model, mode, status, source_storage_path, config)
        VALUES($1, 'gemini', $2, 'diarized', 'queued', $3, $4::jsonb)
        RETURNING *
        """,
        session_id,
        settings.gemini_transcribe_model,
        audio_relative_path,
        _json(gemini_transcribe.build_transcription_config(mode="diarized", custom_vocabulary=[])),
    )
    await conn.execute(
        """
        INSERT INTO voice_work_queue(session_id, run_id, step, payload)
        VALUES($1, $2, 'gemini_archive', '{}'::jsonb)
        ON CONFLICT DO NOTHING
        """,
        session_id,
        run["id"],
    )
    return run["id"]


async def ingest_postcall_event(event: dict[str, Any]) -> dict[str, Any]:
    """Persist a verified ElevenLabs event, never trusting it before HMAC verification."""
    event_type = str(event.get("type") or "")
    if event_type not in {"post_call_transcription", "post_call_audio", "call_initiation_failure"}:
        raise VoiceSessionError("Unsupported ElevenLabs post-call event type.")
    key = _event_key(event)
    session_hint, conversation_id = _session_from_event(event)
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    archive_run_id: UUID | None = None

    async with transaction() as conn:
        session = None
        if session_hint:
            try:
                session = await conn.fetchrow("SELECT * FROM voice_sessions WHERE id=$1 FOR UPDATE", UUID(session_hint))
            except ValueError:
                raise VoiceSessionError("Webhook session identifier is invalid.")
        if not session and conversation_id:
            session = await conn.fetchrow(
                "SELECT * FROM voice_sessions WHERE provider_conversation_id=$1 FOR UPDATE", conversation_id
            )
        inserted = await conn.fetchrow(
            """
            INSERT INTO voice_session_events(session_id, provider_event_key, provider_event_type, provider_conversation_id, payload)
            VALUES($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (provider_event_key) DO NOTHING
            RETURNING id
            """,
            session["id"] if session else None,
            key,
            event_type,
            conversation_id,
            # PR21-F1: never _json(event) — audio/binary fields are redacted and
            # unmatched callbacks are reduced to a metadata-only envelope.
            _json(storage_payload(event, matched_session=bool(session))),
        )
        if not inserted:
            return {"accepted": True, "duplicate": True, "event_type": event_type}
        if not session:
            # A callback without a session mapping is retained only as a minimal
            # verified event; it never causes recording or downstream provider work.
            return {"accepted": True, "matched_session": False, "event_type": event_type}

        await conn.execute(
            """
            UPDATE voice_sessions
            SET provider_conversation_id=COALESCE(
                  provider_conversation_id,
                  (SELECT $2::text WHERE NOT EXISTS (
                     SELECT 1 FROM voice_sessions other
                     WHERE other.provider_conversation_id=$2::text AND other.id<>$1))
                ),
                state=CASE WHEN $3='call_initiation_failure' THEN 'failed' WHEN state='archived' THEN state ELSE 'completed' END,
                completed_at=COALESCE(completed_at, now())
            WHERE id=$1
            """,
            session["id"],
            conversation_id,
            event_type,
        )
        if event_type == "post_call_transcription":
            for turn in _turns(event):
                await conn.execute(
                    """
                    INSERT INTO voice_transcript_turns(
                      session_id, source, ordinal, speaker_role, speaker_label, language,
                      text, start_ms, end_ms, is_final, meta
                    ) VALUES($1,'elevenlabs',$2,$3,$4,$5,$6,$7,$8,true,$9::jsonb)
                    ON CONFLICT (session_id, source, ordinal) DO UPDATE SET
                      text=EXCLUDED.text, speaker_role=EXCLUDED.speaker_role,
                      speaker_label=EXCLUDED.speaker_label, language=EXCLUDED.language,
                      start_ms=EXCLUDED.start_ms, end_ms=EXCLUDED.end_ms, meta=EXCLUDED.meta
                    """,
                    session["id"], turn["ordinal"], turn["speaker_role"], turn["speaker_label"],
                    turn["language"], turn["text"], turn["start_ms"], turn["end_ms"], _json(turn["meta"]),
                )
        elif event_type == "post_call_audio" and bool(session["archive_consent"]):
            encoded = data.get("full_audio")
            if not isinstance(encoded, str):
                raise VoiceSessionError("ElevenLabs post-call audio event lacks full_audio.")
            try:
                audio = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise VoiceSessionError("ElevenLabs post-call audio was not valid base64.") from exc
            if not audio or len(audio) > MAX_ARCHIVE_AUDIO_BYTES:
                raise VoiceSessionError("Post-call audio is empty or exceeds the 32 MB archive limit.")
            relative = storage.save_bytes(f"voice/{session['id']}/{conversation_id}.mp3", audio)
            archive_run_id = await _queue_archive(conn, session_id=session["id"], audio_relative_path=relative)
            # Consented path: keep a storage reference, never the raw base64.
            await conn.execute(
                "UPDATE voice_session_events SET payload = payload || jsonb_build_object('audio_ref', $2::text) WHERE id=$1",
                inserted["id"],
                relative,
            )
        await conn.execute("UPDATE voice_session_events SET processed_at=now() WHERE id=$1", inserted["id"])

    return {
        "accepted": True,
        "duplicate": False,
        "matched_session": True,
        "session_id": str(session["id"]),
        "event_type": event_type,
        "archive_run_id": str(archive_run_id) if archive_run_id else None,
    }


async def claim_archive_work(conn, limit: int = 5) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        WITH due AS (
          SELECT id FROM voice_work_queue
          WHERE status='pending' AND next_attempt_at <= now()
          ORDER BY next_attempt_at, id
          FOR UPDATE SKIP LOCKED LIMIT $1
        )
        UPDATE voice_work_queue q
        SET status='running', attempts=attempts+1, started_at=COALESCE(started_at, now())
        FROM due WHERE q.id=due.id
        RETURNING q.*
        """,
        limit,
    )
    return [dict(row) for row in rows]


async def process_archive_work(item: dict[str, Any]) -> dict[str, Any]:
    """Run a consented audio archive through Gemini 3.5 Transcribe asynchronously."""
    run_id = item.get("run_id")
    session_id = item["session_id"]
    if not run_id:
        raise VoiceSessionError("Archive work has no transcription run.")
    async with transaction() as conn:
        run = await conn.fetchrow("SELECT * FROM voice_transcription_runs WHERE id=$1 FOR UPDATE", run_id)
        if not run:
            raise LookupError("voice transcription run not found")
        await conn.execute(
            "UPDATE voice_transcription_runs SET status='running', started_at=COALESCE(started_at, now()) WHERE id=$1",
            run_id,
        )
    try:
        output = await gemini_transcribe.transcribe_archive(
            storage.absolute_path(run["source_storage_path"]), mode=run["mode"], custom_vocabulary=[]
        )
    except Exception as exc:
        async with transaction() as conn:
            await conn.execute(
                "UPDATE voice_transcription_runs SET status='failed', last_error=$2, completed_at=now() WHERE id=$1",
                run_id,
                str(exc)[:2000],
            )
        raise
    async with transaction() as conn:
        await conn.execute(
            """
            UPDATE voice_transcription_runs
            SET status='completed', transcript_text=$2, config=$3::jsonb, completed_at=now()
            WHERE id=$1
            """,
            run_id,
            output["transcript_text"],
            _json(output["config"]),
        )
        await conn.execute(
            """
            INSERT INTO voice_transcript_turns(session_id, source, ordinal, speaker_role, text, is_final, meta)
            VALUES($1,'gemini',0,'speaker',$2,true,$3::jsonb)
            ON CONFLICT (session_id, source, ordinal) DO UPDATE SET text=EXCLUDED.text, meta=EXCLUDED.meta
            """,
            session_id,
            output["transcript_text"],
            _json({"model": output["model"], "mode": output["mode"]}),
        )
        await conn.execute("UPDATE voice_sessions SET state='archived' WHERE id=$1 AND state='completed'", session_id)
    return {"session_id": str(session_id), "run_id": str(run_id), "status": "completed", "model": output["model"]}


async def mark_archive_work(conn, work_id: int, *, result: dict[str, Any] | None = None, error: str | None = None, attempts: int = 1, max_attempts: int = 3) -> str:
    if error is None:
        await conn.execute(
            "UPDATE voice_work_queue SET status='done', finished_at=now(), last_error=NULL, payload=payload || $2::jsonb WHERE id=$1",
            work_id,
            _json({"result": result or {}}),
        )
        return "done"
    status = "dead" if attempts >= max_attempts else "pending"
    await conn.execute(
        """
        UPDATE voice_work_queue
        SET status=$2, last_error=$3,
            next_attempt_at=CASE WHEN $2='pending' THEN now() + interval '30 seconds' ELSE next_attempt_at END,
            finished_at=CASE WHEN $2='dead' THEN now() ELSE NULL END
        WHERE id=$1
        """,
        work_id,
        status,
        error[:2000],
    )
    return status


async def reclaim_stale_archive_work(conn, stale_seconds: int | float | None = None) -> int:
    """PR21-F7: return crashed ``running`` archive claims to ``pending`` (or ``dead``).

    Mirrors the MSE ``work_queue`` reclaim. ``updated_at`` is bumped by trigger
    on claim, so it marks when the current claim started.
    """
    seconds = float(settings.voice_work_queue_stale_seconds if stale_seconds is None else stale_seconds)
    rows = await conn.fetch(
        """
        UPDATE voice_work_queue
        SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
            next_attempt_at = now(),
            last_error = 'reclaimed stale running claim',
            finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END
        WHERE status='running' AND updated_at < now() - make_interval(secs => $1)
        RETURNING id
        """,
        seconds,
    )
    return len(rows)


async def process_archive_queue(limit: int = 5) -> dict[str, int]:
    """Run pending archive jobs; invoked by the existing worker tick."""
    async with transaction() as conn:
        reclaimed = await reclaim_stale_archive_work(conn)
        items = await claim_archive_work(conn, limit)
    done = failed = dead = 0
    for item in items:
        try:
            result = await process_archive_work(item)
            async with transaction() as conn:
                await mark_archive_work(conn, int(item["id"]), result=result)
            done += 1
        except Exception as exc:  # noqa: BLE001
            async with transaction() as conn:
                status = await mark_archive_work(
                    conn,
                    int(item["id"]),
                    error=str(exc),
                    attempts=int(item.get("attempts") or 1),
                    max_attempts=int(item.get("max_attempts") or 3),
                )
            failed += 1
            dead += int(status == "dead")
    return {"claimed": len(items), "done": done, "failed": failed, "dead": dead, "reclaimed": reclaimed}


__all__ = [
    "VoiceSessionConflict",
    "VoiceSessionError",
    "redact_binary_fields",
    "reclaim_stale_archive_work",
    "storage_payload",
    "bind_conversation",
    "create_session",
    "get_session",
    "ingest_postcall_event",
    "process_archive_queue",
]
