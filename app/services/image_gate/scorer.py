"""Vision scorers: Gemini primary, Grok Imagine fallback. Fail closed on errors."""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from typing import Any

from ...config import settings
from .refs import ActiveReferenceSet

log = logging.getLogger("media_state.image_scorer")


class ScorerError(RuntimeError):
    """Scorer transport/parse failure — callers MUST fail closed."""


AXES = (
    "face_consistency",
    "lighting",
    "composition",
    "text_legibility",
    "brand_fit",
    "no_artifacts",
    "identity_likeness",
)


@dataclass
class ScoreCard:
    face_consistency: float
    lighting: float
    composition: float
    text_legibility: float
    brand_fit: float
    no_artifacts: float
    identity_likeness: float
    overall: float
    identity_drift: bool
    failure_reasons: list[str] = field(default_factory=list)
    scorer: str = "gemini"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_axes_dict(self) -> dict[str, float]:
        return {k: float(getattr(self, k)) for k in AXES}


def _clamp(n: float) -> float:
    return max(0.0, min(10.0, float(n)))


def _parse_score_payload(data: dict[str, Any], scorer: str) -> ScoreCard:
    missing = [a for a in AXES if a not in data and a.replace("_", "") not in data]
    # allow short aliases
    aliases = {
        "face_consistency": ["face_consistency", "face", "avatar_consistency"],
        "lighting": ["lighting"],
        "composition": ["composition"],
        "text_legibility": ["text_legibility", "text"],
        "brand_fit": ["brand_fit", "brand"],
        "no_artifacts": ["no_artifacts", "artifacts_inverted", "artifacts"],
        "identity_likeness": ["identity_likeness", "identity", "likeness"],
    }
    vals: dict[str, float] = {}
    for axis, keys in aliases.items():
        found = None
        for k in keys:
            if k in data:
                found = data[k]
                break
        if found is None:
            raise ScorerError(f"scorer payload missing axis {axis}")
        vals[axis] = _clamp(float(found))
        # artifacts: if key is raw 'artifacts' (higher=worse), invert
        if axis == "no_artifacts" and "artifacts" in data and "no_artifacts" not in data:
            vals[axis] = _clamp(10.0 - float(data["artifacts"]))

    overall = data.get("overall")
    if overall is None:
        overall = sum(vals.values()) / len(vals)
    overall = _clamp(float(overall))

    drift = data.get("identity_drift")
    if drift is None:
        drift = data.get("identity_drift", False)
    identity_drift = bool(drift)
    # Hard heuristic: likeness < 8 implies drift
    if vals["identity_likeness"] < 8.0:
        identity_drift = True

    reasons = data.get("failure_reasons") or data.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    reasons = [str(r) for r in reasons]

    return ScoreCard(
        face_consistency=vals["face_consistency"],
        lighting=vals["lighting"],
        composition=vals["composition"],
        text_legibility=vals["text_legibility"],
        brand_fit=vals["brand_fit"],
        no_artifacts=vals["no_artifacts"],
        identity_likeness=vals["identity_likeness"],
        overall=overall,
        identity_drift=identity_drift,
        failure_reasons=reasons,
        scorer=scorer,
        raw=data,
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ScorerError("scorer returned non-JSON")
        return json.loads(m.group(0))


def _guess_image_mime(name_or_url: str, content_type: str | None = None) -> str:
    """Best-effort image MIME type from a response Content-Type header or filename/URL."""
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct.startswith("image/"):
            return ct
    guessed, _ = mimetypes.guess_type(name_or_url)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/jpeg"


def _read_hero_bytes(refs: ActiveReferenceSet) -> tuple[bytes, str]:
    """Read the locked hero identity reference image off disk as real bytes."""
    path = refs.hero.path
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ScorerError(f"failed to read hero reference image {path}: {exc}") from exc
    if not data:
        raise ScorerError(f"hero reference image is empty: {path}")
    return data, _guess_image_mime(str(path))


async def _fetch_candidate_bytes(client: Any, candidate_url: str) -> tuple[bytes, str]:
    """Download the candidate image's real bytes so the vision model can actually see it.

    candidate_url may be a signed/authenticated URL this app's own network can reach but a
    third-party vision API's servers cannot — so we always fetch the bytes ourselves here
    and attach them inline, rather than handing the bare URL to the provider.
    """
    try:
        resp = await client.get(candidate_url)
    except Exception as exc:  # noqa: BLE001 - any transport failure must fail closed
        raise ScorerError(f"failed to fetch candidate image {candidate_url}: {exc}") from exc
    status = getattr(resp, "status_code", 200)
    if status >= 400:
        raise ScorerError(f"candidate image fetch HTTP {status}: {candidate_url}")
    data = resp.content
    if not data:
        raise ScorerError(f"candidate image fetch returned empty body: {candidate_url}")
    mime = _guess_image_mime(candidate_url, resp.headers.get("content-type") if resp.headers else None)
    return data, mime


async def _score_gemini(candidate_url: str, refs: ActiveReferenceSet, prompt: str) -> ScoreCard:
    api_key = (settings.image_scorer_gemini_api_key or settings.gemini_api_key or "").strip()
    if not api_key:
        raise ScorerError("GEMINI API key not configured")
    # Deterministic offline/mock path when provider is mock
    if settings.image_scorer_provider == "mock":
        raise ScorerError("mock provider should not call _score_gemini")
    try:
        import httpx
    except ImportError as exc:
        raise ScorerError("httpx required for gemini scorer") from exc

    system = (
        "You are a strict likeness auditor. Compare the CANDIDATE image to the HERO "
        "identity reference. Score axes 0-10. identity_likeness is mandatory. "
        "Set identity_drift=true if face is not the same person. "
        "Return ONLY JSON with keys: face_consistency,lighting,composition,"
        "text_legibility,brand_fit,no_artifacts,identity_likeness,overall,"
        "identity_drift,failure_reasons."
    )
    hero_bytes, hero_mime = _read_hero_bytes(refs)

    model = settings.image_scorer_gemini_model
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    async with httpx.AsyncClient(timeout=settings.image_scorer_timeout_seconds) as client:
        # candidate_url may be a signed/app-internal URL that Google's servers cannot
        # reach, so we fetch its real bytes ourselves rather than passing a file_data URL.
        candidate_bytes, candidate_mime = await _fetch_candidate_bytes(client, candidate_url)
        body = {
            "contents": [{
                "parts": [
                    {
                        "text": (
                            f"{system}\n\nHERO_SHA256={refs.hero.sha256}\n"
                            f"PROMPT={prompt}\n"
                            "The HERO identity reference image is attached first, followed "
                            "by the CANDIDATE image to score.\nScore now."
                        )
                    },
                    {
                        "inline_data": {
                            "mime_type": hero_mime,
                            "data": base64.b64encode(hero_bytes).decode("ascii"),
                        }
                    },
                    {
                        "inline_data": {
                            "mime_type": candidate_mime,
                            "data": base64.b64encode(candidate_bytes).decode("ascii"),
                        }
                    },
                ]
            }],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        resp = await client.post(url, json=body)
        if resp.status_code >= 400:
            raise ScorerError(f"gemini HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ScorerError(f"gemini unexpected payload: {payload!r}") from exc
    return _parse_score_payload(_extract_json(text), "gemini")


async def _score_grok(candidate_url: str, refs: ActiveReferenceSet, prompt: str) -> ScoreCard:
    api_key = (settings.image_scorer_grok_api_key or settings.xai_api_key or "").strip()
    if not api_key:
        raise ScorerError("GROK/xAI API key not configured")
    import httpx

    hero_bytes, hero_mime = _read_hero_bytes(refs)

    async with httpx.AsyncClient(timeout=settings.image_scorer_timeout_seconds) as client:
        # xAI's chat completions API is OpenAI-vision-compatible: image content parts are
        # {"type": "image_url", "image_url": {"url": ...}}, where url accepts either an
        # http(s) URL or a base64 data URI. candidate_url may not be reachable from xAI's
        # servers (signed/app-internal), so we fetch bytes ourselves and send data URIs
        # for both images rather than trusting either URL to be externally fetchable.
        candidate_bytes, candidate_mime = await _fetch_candidate_bytes(client, candidate_url)
        hero_data_uri = f"data:{hero_mime};base64,{base64.b64encode(hero_bytes).decode('ascii')}"
        candidate_data_uri = (
            f"data:{candidate_mime};base64,{base64.b64encode(candidate_bytes).decode('ascii')}"
        )
        body = {
            "model": settings.image_scorer_grok_model,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Score avatar likeness 0-10 JSON axes "
                            "face_consistency,lighting,composition,text_legibility,brand_fit,"
                            "no_artifacts,identity_likeness,overall,identity_drift,failure_reasons. "
                            f"HERO_SHA={refs.hero.sha256} PROMPT={prompt} "
                            "The first attached image is the HERO identity reference. The "
                            "second attached image is the CANDIDATE to score."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": hero_data_uri}},
                    {"type": "image_url", "image_url": {"url": candidate_data_uri}},
                ],
            }],
            "temperature": 0.1,
        }
        resp = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        if resp.status_code >= 400:
            raise ScorerError(f"grok HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ScorerError(f"grok unexpected payload: {payload!r}") from exc
    return _parse_score_payload(_extract_json(text), "grok")


async def score_candidate(
    *,
    candidate_url: str,
    refs: ActiveReferenceSet,
    prompt: str = "",
    forced_card: ScoreCard | None = None,
) -> ScoreCard:
    """Score candidate vs hero. Fail closed. Optional forced_card for tests."""
    if forced_card is not None:
        return forced_card

    provider = (settings.image_scorer_provider or "gemini").lower()
    if provider == "mock":
        raise ScorerError(
            "IMAGE_SCORER_PROVIDER=mock requires forced_card in tests; "
            "refusing live score (fail closed)"
        )

    errors: list[str] = []
    order = [provider]
    if provider == "gemini":
        order.append("grok")
    elif provider == "grok":
        order.append("gemini")

    for name in order:
        try:
            if name == "gemini":
                return await _score_gemini(candidate_url, refs, prompt)
            if name in ("grok", "grok_imagine"):
                return await _score_grok(candidate_url, refs, prompt)
        except ScorerError as exc:
            log.warning("scorer %s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")
            continue
    raise ScorerError("all scorers failed: " + "; ".join(errors))
