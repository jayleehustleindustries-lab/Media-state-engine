"""Phase 3 script generation for short-form vertical video.

Produces structured fields optimized for TikTok / Reels / Shorts:
  - hook (0–3s / first ~2s on screen)
  - punchy body
  - clear CTA
  - duration_target_seconds (15–60)
  - aspect_ratio primary 9:16

No external LLM required — rule-based templates so the engine stays offline-safe.
"""
from __future__ import annotations

import re
from typing import Any

MIN_DURATION = 15
MAX_DURATION = 60
DEFAULT_DURATION = 30
PRIMARY_ASPECT = "9:16"
HORIZONTAL_ASPECT = "16:9"

PLATFORM_HASHTAGS = {
    "tiktok": ["#fyp", "#viral", "#jayleehustle"],
    "reels": ["#reels", "#explore", "#jayleehustle"],
    "shorts": ["#shorts", "#youtube", "#jayleehustle"],
}


def _clamp_duration(seconds: int | None) -> int:
    if seconds is None:
        return DEFAULT_DURATION
    return max(MIN_DURATION, min(MAX_DURATION, int(seconds)))


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p and p.strip()]


def _normalize_hook(text: str) -> str:
    t = text.strip()
    # Keep hook short for the critical first 2 seconds
    if len(t) > 120:
        t = t[:117].rstrip() + "..."
    if not t.endswith(("!", "?", ".")):
        t = t + "!"
    return t


def _default_cta(topic: str | None = None) -> str:
    if topic:
        return f"Follow for more on {topic.strip()[:60]} — tap now."
    return "Follow for the next tip — tap follow now."


def generate_from_topic(
    topic: str,
    *,
    duration_target_seconds: int | None = None,
    cta: str | None = None,
) -> dict[str, Any]:
    """Build a short-form script from a topic/brief."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")
    duration = _clamp_duration(duration_target_seconds)
    hook = _normalize_hook(f"Stop scrolling — {topic}")
    # Punchy body sized roughly to duration (≈2.5 words/sec spoken)
    target_words = max(40, min(140, int(duration * 2.2)))
    body = (
        f"Here's the move on {topic}: keep it simple, act fast, and cut the fluff. "
        f"Most people overthink the first step — you won't. "
        f"Do one clear action today that compounds. "
        f"That's how hustlers win in under {duration} seconds of attention."
    )
    words = body.split()
    if len(words) > target_words:
        body = " ".join(words[:target_words]) + "."
    cta_text = (cta or _default_cta(topic)).strip()
    full = f"{hook} {body} {cta_text}".strip()
    return {
        "hook": hook,
        "body": body,
        "cta": cta_text,
        "duration_target_seconds": duration,
        "aspect_ratio": PRIMARY_ASPECT,
        "full_text": full,
        "source": "topic",
    }


def generate_from_text(
    script_text: str,
    *,
    duration_target_seconds: int | None = None,
    cta: str | None = None,
) -> dict[str, Any]:
    """Parse/structure an existing script into hook / body / CTA."""
    text = (script_text or "").strip()
    if not text:
        raise ValueError("script_text is required")
    duration = _clamp_duration(duration_target_seconds)
    sents = _sentences(text)
    if len(sents) == 1:
        hook = _normalize_hook(sents[0][:100])
        body = sents[0]
        cta_text = (cta or _default_cta()).strip()
    elif len(sents) == 2:
        hook = _normalize_hook(sents[0])
        body = sents[1]
        cta_text = (cta or _default_cta()).strip()
    else:
        hook = _normalize_hook(sents[0])
        # Last sentence as CTA if it looks like one; else append default
        last = sents[-1]
        cta_like = bool(re.search(
            r"\b(follow|subscribe|comment|share|link|tap|click|dm|join)\b",
            last,
            re.I,
        ))
        if cta:
            cta_text = cta.strip()
            body = " ".join(sents[1:])
        elif cta_like:
            cta_text = last
            body = " ".join(sents[1:-1]) or sents[1]
        else:
            body = " ".join(sents[1:])
            cta_text = _default_cta()
    full = f"{hook} {body} {cta_text}".strip()
    return {
        "hook": hook,
        "body": body,
        "cta": cta_text,
        "duration_target_seconds": duration,
        "aspect_ratio": PRIMARY_ASPECT,
        "full_text": full,
        "source": "script_text",
    }


def build_platform_captions(script: dict[str, Any], platforms: list[str] | None = None) -> dict[str, Any]:
    """Stage platform-specific captions + overlay cues (not published)."""
    platforms = platforms or ["tiktok", "reels", "shorts"]
    hook = script.get("hook") or ""
    body = script.get("body") or ""
    cta = script.get("cta") or ""
    out: dict[str, Any] = {}
    for p in platforms:
        key = p.lower().strip()
        tags = PLATFORM_HASHTAGS.get(key, ["#jayleehustle"])
        caption = f"{hook} {cta} {' '.join(tags)}".strip()
        out[key] = {
            "caption": caption[:2200],  # TikTok-ish cap; Reels/Shorts similar
            "overlays": [
                {"t_start_s": 0.0, "t_end_s": 2.0, "text": hook[:80], "role": "hook"},
                {"t_start_s": 2.0, "t_end_s": float(script.get("duration_target_seconds") or 30) - 3, "text": body[:100], "role": "body"},
                {"t_start_s": max(0.0, float(script.get("duration_target_seconds") or 30) - 3), "t_end_s": float(script.get("duration_target_seconds") or 30), "text": cta[:80], "role": "cta"},
            ],
            "hashtags": tags,
            "staged": True,
            "published": False,
        }
    return out


def build_formats_meta(*, include_horizontal: bool = False, heygen_dual_paid: bool = False) -> dict[str, Any]:
    """Dual-format staging. Horizontal never burns a second HeyGen credit by default."""
    primary = {
        "aspect": PRIMARY_ASPECT,
        "width": 1080,
        "height": 1920,
        "role": "primary",
        "provider_render": "heygen",
    }
    horizontal: dict[str, Any]
    if not include_horizontal:
        horizontal = {
            "aspect": HORIZONTAL_ASPECT,
            "width": 1920,
            "height": 1080,
            "role": "horizontal",
            "mode": "not_requested",
            "status": "skipped",
            "heygen_paid": False,
        }
    elif heygen_dual_paid:
        horizontal = {
            "aspect": HORIZONTAL_ASPECT,
            "width": 1920,
            "height": 1080,
            "role": "horizontal",
            "mode": "heygen_paid",
            "status": "requested",
            "heygen_paid": True,
            "note": "Second HeyGen Direct Video call authorized (opt-in + HEYGEN_ALLOW_DUAL_FORMAT).",
        }
    else:
        horizontal = {
            "aspect": HORIZONTAL_ASPECT,
            "width": 1920,
            "height": 1080,
            "role": "horizontal",
            "mode": "derived",
            "status": "staged",
            "heygen_paid": False,
            "note": (
                "Horizontal staged as derived/letterbox-from-primary. "
                "No second HeyGen call (cost guardrail). "
                "Set include_horizontal + HEYGEN_ALLOW_DUAL_FORMAT for paid dual render."
            ),
        }
    return {
        "primary": primary,
        "horizontal": horizontal,
        "include_horizontal": bool(include_horizontal),
    }


def compose_script(
    *,
    script_text: str | None = None,
    topic: str | None = None,
    duration_target_seconds: int | None = None,
    cta: str | None = None,
    include_horizontal: bool = False,
    heygen_dual_paid: bool = False,
    platforms: list[str] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return (script_text, script_struct, job_meta) ready to persist."""
    if topic and not script_text:
        script = generate_from_topic(topic, duration_target_seconds=duration_target_seconds, cta=cta)
    elif script_text:
        script = generate_from_text(script_text, duration_target_seconds=duration_target_seconds, cta=cta)
    else:
        raise ValueError("provide script_text or topic")
    captions = build_platform_captions(script, platforms=platforms)
    formats = build_formats_meta(include_horizontal=include_horizontal, heygen_dual_paid=heygen_dual_paid)
    meta = {
        "formats": formats,
        "platform_captions": captions,
        "awaiting_approval": True,
        "auto_publish": False,
    }
    return script["full_text"], script, meta
