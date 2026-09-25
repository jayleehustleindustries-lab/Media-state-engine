"""Resolve a caller's verified identity from the ``api_keys`` table.

Reuses the auth mechanism already present in the schema (migration
``20260317000002_media_state_engine_auth_and_quality.sql``: the
``api_keys`` table + ``verify_api_key()`` RPC) instead of inventing a new
identity system.

``app.auth`` stays the coarse-grained gate for "is this caller allowed to
call mutation routes at all" (a single shared bearer secret, checked by
``require_api_key`` / ``ApiKeyMiddleware``). It answers no identity
question — every caller who knows that one secret looks identical.

This module answers the separate, stricter question a human-approval
audit trail actually needs: "which named identity, if any, does the
presented credential correspond to". It is used anywhere an action (like
job approval) must record a real, non-spoofable actor of record rather
than trusting a client-supplied free-text field (see PHASE reviews:
"who approved is just a text field the caller controls").
"""
from __future__ import annotations

import hashlib


async def resolve_actor_identity(conn, presented_key: str | None) -> dict | None:
    """Verify ``presented_key`` against ``api_keys`` via ``verify_api_key()``.

    Returns ``{'id': <uuid str>, 'name': <str>}`` on a verified match.
    Returns ``None`` when no key was presented, or when the presented
    credential does not match any row in ``api_keys`` (the common current
    deployment mode: a single shared ``MEDIA_ENGINE_API_KEY`` / ``API_KEY``
    env secret with no per-caller rows provisioned yet). Callers MUST NOT
    treat ``None`` as "unauthenticated" — ``app.auth`` already rejected the
    request if the shared secret didn't match; this only says no *named*
    identity is available for the audit trail.

    Never raises: ``verify_api_key`` raises on an invalid/unknown key, and
    that is expected here (most deployments have no api_keys rows yet) —
    it is caught and treated the same as "no match".
    """
    if not presented_key or not presented_key.strip():
        return None
    raw = presented_key.strip()
    try:
        # Nested SAVEPOINT: verify_api_key() raises on no-match, which
        # would otherwise poison the caller's outer transaction.
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM verify_api_key($1)", raw)
    except Exception:
        return None
    if not row:
        return None
    return {"id": str(row["id"]), "name": row["name"]}


def unverified_identity_label(presented_key: str) -> str:
    """Non-spoofable fallback actor label when no api_keys row matches.

    Derived from the credential the caller actually had to present to
    pass auth (a stable, one-way hash of it) — never from an arbitrary
    client-supplied display string. Two different callers who both hold
    the one shared secret are indistinguishable by design (that secret
    carries no identity); this label at least ties the record to "the
    holder of this exact credential" rather than to whatever name they
    typed into a JSON body.
    """
    digest = hashlib.sha256(presented_key.strip().encode()).hexdigest()[:12]
    return f"apikey:{digest}"
