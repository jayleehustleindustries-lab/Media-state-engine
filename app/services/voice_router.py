"""Deterministic language and identity routing for Job Command live sessions.

This is intentionally policy-driven rather than personality mimicry.  It selects
only the operator-owned voice profile configured in ElevenLabs, validates a
caller-selected language against an allowlist, and passes a minimal session
context to the provider.  It does not infer a person's identity from a voice or
route to a public figure's likeness.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
JOB_COMMAND_PROFILE = "job-command"


class VoiceRouteError(ValueError):
    """The requested voice session cannot be routed under the campaign policy."""


@dataclass(frozen=True)
class VoiceRoute:
    profile_code: str
    language: str
    supported_languages: tuple[str, ...]
    presentation_profile: dict[str, object]

    def conversation_init(self, *, session_id: str, visitor_ref: str, asr_keywords: list[str]) -> dict:
        """Return provider-safe initialization data; no provider credential is included."""
        return {
            "dynamic_variables": {
                "job_command_session_id": session_id,
                "job_command_visitor_ref": visitor_ref,
                "job_command_profile": self.profile_code,
            },
            "conversation_config_override": {
                # Must be explicitly enabled in the ElevenLabs agent Security
                # settings.  The server never exposes arbitrary voice routing.
                "agent": {"language": self.language},
                "asr": {"keywords": asr_keywords[:50]},
            },
        }


def normalize_language(value: str | None, *, default: str, supported: Iterable[str]) -> str:
    """Normalize a BCP-47-ish language selection and enforce the configured route list."""
    available = tuple(item.strip() for item in supported if item and item.strip())
    if not available:
        raise VoiceRouteError("No Job Command voice languages are configured.")
    candidate = (value or default or "").strip()
    if not LANGUAGE_RE.fullmatch(candidate):
        raise VoiceRouteError("Language must be a supported BCP-47 code such as en or es-MX.")
    # First match exact code, then accept a requested base language if a regional
    # profile for that base was configured.  This keeps an operator in control of
    # the actual pronunciation/voice preset offered by the agent.
    if candidate in available:
        return candidate
    base = candidate.split("-", 1)[0]
    matches = [item for item in available if item.split("-", 1)[0] == base]
    if len(matches) == 1:
        return matches[0]
    raise VoiceRouteError(
        f"Language {candidate!r} is not enabled for Job Command. Supported: {', '.join(available)}."
    )


def route_job_command(
    *,
    requested_language: str | None,
    default_language: str,
    supported_languages: Iterable[str],
) -> VoiceRoute:
    """Build the only permitted live-voice route for the Job Command campaign."""
    languages = tuple(item.strip() for item in supported_languages if item and item.strip())
    resolved = normalize_language(requested_language, default=default_language, supported=languages)
    return VoiceRoute(
        profile_code=JOB_COMMAND_PROFILE,
        language=resolved,
        supported_languages=languages,
        presentation_profile={
            "identity_source": "operator_authorized_avatar_and_voice_only",
            "presentation": ["direct", "calm", "decisive", "welcoming", "plainspoken"],
            "exclude": [
                "public_figure likeness or voice imitation",
                "public-figure script, hook, cadence, or content-template copying",
                "biometric speaker identification",
                "unreviewed claims or coercive persuasion",
            ],
            "voice_selection": "configured ElevenLabs language preset; never caller-supplied voice ID",
        },
    )


__all__ = ["JOB_COMMAND_PROFILE", "VoiceRoute", "VoiceRouteError", "normalize_language", "route_job_command"]
