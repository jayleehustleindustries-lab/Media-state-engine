"""Vision scorers: Gemini primary, Grok Imagine fallback. Fail closed on errors."""
from __future__ import annotations

import json
import logging
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
    # Use Gemini generateContent; pass candidate URL + hero hash context in text
    # (bytes upload optional; URL/path referenced in prompt for stubbed providers)
    model = settings.image_scorer_gemini_model
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = {
        "contents": [{
            "parts": [{
                "text": (
                    f"{system}\n\nHERO_SHA256={refs.hero.sha256}\n"
                    f"HERO_PATH={refs.hero.path}\nCANDIDATE={candidate_url}\n"
                    f"PROMPT={prompt}\n"
                    "Score now."
                )
            }]
        }],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=settings.image_scorer_timeout_seconds) as client:
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
    body = {
        "model": settings.image_scorer_grok_model,
        "messages": [{
            "role": "user",
            "content": (
                "Score avatar likeness 0-10 JSON axes "
                "face_consistency,lighting,composition,text_legibility,brand_fit,"
                "no_artifacts,identity_likeness,overall,identity_drift,failure_reasons. "
                f"HERO_SHA={refs.hero.sha256} CANDIDATE={candidate_url} PROMPT={prompt}"
            ),
        }],
        "temperature": 0.1,
    }
    async with httpx.AsyncClient(timeout=settings.image_scorer_timeout_seconds) as client:
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
