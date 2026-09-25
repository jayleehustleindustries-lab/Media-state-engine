"""Vision scorers must actually attach real image bytes to the outgoing API call.

Regression coverage for the bug where `_score_gemini` / `_score_grok` only ever
interpolated `candidate_url` and `refs.hero.path` as TEXT into the prompt, so the
vision model never actually looked at any pixels. These tests mock the httpx
transport and assert on the real outgoing request body — not just that "a request
was made" — to prove the fix is structurally correct without spending API credits.
"""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.image_gate.refs import load_active_reference_set
from app.services.image_gate.scorer import ScorerError, _score_gemini, _score_grok

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[1]
REF_PACK = REPO / "reference_images" / "ACTIVE_REFERENCE_SET.json"

CANDIDATE_BYTES = b"\xff\xd8\xff\xe0 fake-candidate-jpeg-bytes not-a-real-jpeg"
CANDIDATE_URL = "https://storage.example.com/signed/candidate-abc123.jpg?token=secret"

VALID_SCORE_JSON = (
    '{"face_consistency":9,"lighting":9,"composition":9,"text_legibility":9,'
    '"brand_fit":9,"no_artifacts":9,"identity_likeness":9,"overall":9,'
    '"identity_drift":false,"failure_reasons":[]}'
)


def _refs():
    return load_active_reference_set(REF_PACK)


class FakeHeaders(dict):
    def get(self, key, default=None):  # case-insensitive-ish enough for tests
        return dict.get(self, key, default)


class FakeGetResp:
    def __init__(self, content: bytes, status_code: int = 200, content_type: str = "image/jpeg"):
        self.content = content
        self.status_code = status_code
        self.headers = FakeHeaders({"content-type": content_type})


class FakePostResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    """Records every get()/post() call for later assertion."""

    def __init__(self, *a, **k):
        self.calls: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, *a, **k):
        self.calls["get_url"] = url
        return FakeGetResp(CANDIDATE_BYTES)

    async def post(self, url, json=None, headers=None, **k):
        self.calls["post_url"] = url
        self.calls["post_json"] = json
        self.calls["post_headers"] = headers
        return FakePostResp(self._response_payload())

    def _response_payload(self):
        raise NotImplementedError


class FakeGeminiClient(FakeClient):
    def _response_payload(self):
        return {"candidates": [{"content": {"parts": [{"text": VALID_SCORE_JSON}]}}]}


class FakeGrokClient(FakeClient):
    def _response_payload(self):
        return {"choices": [{"message": {"content": VALID_SCORE_JSON}}]}


async def test_score_gemini_attaches_real_hero_and_candidate_bytes(monkeypatch):
    monkeypatch.setattr(settings, "image_scorer_gemini_api_key", "test-gemini-key")
    monkeypatch.setattr(settings, "image_scorer_provider", "gemini")
    refs = _refs()
    hero_bytes = refs.hero.path.read_bytes()
    assert hero_bytes  # sanity: real file, real bytes

    client = FakeGeminiClient()
    with patch("httpx.AsyncClient", lambda *a, **k: client):
        card = await _score_gemini(CANDIDATE_URL, refs, "a prompt")

    assert card.scorer == "gemini"
    assert card.identity_likeness == 9.0

    # The candidate URL must actually have been fetched for bytes.
    assert client.calls["get_url"] == CANDIDATE_URL

    body = client.calls["post_json"]
    parts = body["contents"][0]["parts"]
    # text part + two inline_data image parts (hero, candidate)
    image_parts = [p for p in parts if "inline_data" in p]
    assert len(image_parts) == 2

    hero_part, candidate_part = image_parts[0]["inline_data"], image_parts[1]["inline_data"]
    assert hero_part["mime_type"].startswith("image/")
    assert base64.b64decode(hero_part["data"]) == hero_bytes

    assert candidate_part["mime_type"].startswith("image/")
    assert base64.b64decode(candidate_part["data"]) == CANDIDATE_BYTES

    # The old bug: candidate_url / hero path typed into text instead of attached as bytes.
    text_parts = [p["text"] for p in parts if "text" in p]
    assert not any(CANDIDATE_URL in t for t in text_parts)
    assert not any(str(refs.hero.path) in t for t in text_parts)


async def test_score_grok_attaches_real_hero_and_candidate_bytes(monkeypatch):
    monkeypatch.setattr(settings, "image_scorer_grok_api_key", "test-grok-key")
    refs = _refs()
    hero_bytes = refs.hero.path.read_bytes()

    client = FakeGrokClient()
    with patch("httpx.AsyncClient", lambda *a, **k: client):
        card = await _score_grok(CANDIDATE_URL, refs, "a prompt")

    assert card.scorer == "grok"

    body = client.calls["post_json"]
    content = body["messages"][0]["content"]
    assert isinstance(content, list), "grok body must use multi-part content, not a plain string"

    image_url_parts = [p for p in content if p.get("type") == "image_url"]
    assert len(image_url_parts) == 2

    hero_url = image_url_parts[0]["image_url"]["url"]
    candidate_url_sent = image_url_parts[1]["image_url"]["url"]

    assert hero_url.startswith("data:image/")
    assert candidate_url_sent.startswith("data:image/")

    hero_b64 = hero_url.split(",", 1)[1]
    candidate_b64 = candidate_url_sent.split(",", 1)[1]
    assert base64.b64decode(hero_b64) == hero_bytes
    assert base64.b64decode(candidate_b64) == CANDIDATE_BYTES

    text_parts = [p["text"] for p in content if p.get("type") == "text"]
    assert not any(CANDIDATE_URL in t for t in text_parts)


async def test_score_gemini_fails_closed_on_candidate_fetch_404(monkeypatch):
    monkeypatch.setattr(settings, "image_scorer_gemini_api_key", "test-gemini-key")
    refs = _refs()

    class Fake404Client(FakeGeminiClient):
        async def get(self, url, *a, **k):
            self.calls["get_url"] = url
            return FakeGetResp(b"", status_code=404)

    client = Fake404Client()
    with patch("httpx.AsyncClient", lambda *a, **k: client):
        with pytest.raises(ScorerError, match="404"):
            await _score_gemini(CANDIDATE_URL, refs, "a prompt")

    # Must fail before ever calling the vision API with a body lacking real bytes.
    assert "post_json" not in client.calls


async def test_score_grok_fails_closed_on_empty_candidate_body(monkeypatch):
    monkeypatch.setattr(settings, "image_scorer_grok_api_key", "test-grok-key")
    refs = _refs()

    class FakeEmptyClient(FakeGrokClient):
        async def get(self, url, *a, **k):
            self.calls["get_url"] = url
            return FakeGetResp(b"")

    client = FakeEmptyClient()
    with patch("httpx.AsyncClient", lambda *a, **k: client):
        with pytest.raises(ScorerError, match="empty"):
            await _score_grok(CANDIDATE_URL, refs, "a prompt")

    assert "post_json" not in client.calls
