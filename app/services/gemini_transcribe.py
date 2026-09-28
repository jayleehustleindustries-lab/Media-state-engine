"""Gemini 3.5 Transcribe archive adapter for opted-in Job Command recordings."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..config import settings


class GeminiTranscribeError(RuntimeError):
    pass


class GeminiTranscribeNotConfigured(GeminiTranscribeError):
    pass


def build_transcription_config(*, mode: str, custom_vocabulary: list[str]) -> dict[str, Any]:
    """Create a valid, mutually exclusive Gemini transcription configuration.

    Gemini's file transcription does not permit custom vocabulary alongside
    diarization or word timestamps. Job Command archives choose diarization for
    speaker review, while a future vocabulary-focused archive can explicitly opt
    into the smart/vocabulary path.
    """
    if mode == "diarized":
        return {
            "transcription_config": {
                "mode": {"type": "verbatim", "diarization_mode": "speaker"},
                "language_codes": [],
            }
        }
    if mode == "smart":
        return {
            "transcription_config": {
                "mode": {"type": "smart"},
                "language_codes": [],
                "custom_vocabulary": custom_vocabulary[:100],
            }
        }
    raise GeminiTranscribeError(f"Unsupported archive mode: {mode}")


def _transcribe_sync(audio_path: str, *, mode: str, custom_vocabulary: list[str]) -> dict[str, Any]:
    api_key = (settings.gemini_transcribe_api_key or settings.gemini_api_key or "").strip()
    if not api_key:
        raise GeminiTranscribeNotConfigured("Gemini archive transcription is not configured: set GEMINI_TRANSCRIBE_API_KEY.")
    if not Path(audio_path).is_file():
        raise GeminiTranscribeError("Archived call audio is unavailable.")
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise GeminiTranscribeError("google-genai is not installed for Gemini transcription.") from exc
    try:
        client = genai.Client(api_key=api_key)
        audio_file = client.files.upload(file=audio_path)
        interaction = client.interactions.create(
            model=settings.gemini_transcribe_model,
            input=[{
                "type": "audio",
                "uri": audio_file.uri,
                "mime_type": audio_file.mime_type,
            }],
            generation_config=build_transcription_config(mode=mode, custom_vocabulary=custom_vocabulary),
        )
    except Exception as exc:  # provider SDK normalizes network/provider errors inconsistently
        raise GeminiTranscribeError("Gemini archive transcription request failed.") from exc
    transcript = getattr(interaction, "output_text", None)
    if not isinstance(transcript, str) or not transcript.strip():
        raise GeminiTranscribeError("Gemini transcription response did not include transcript text.")
    return {
        "transcript_text": transcript.strip(),
        "model": settings.gemini_transcribe_model,
        "mode": mode,
        "provider_file_uri": getattr(audio_file, "uri", None),
        "config": build_transcription_config(mode=mode, custom_vocabulary=custom_vocabulary),
    }


async def transcribe_archive(audio_path: str, *, mode: str, custom_vocabulary: list[str]) -> dict[str, Any]:
    """Run the blocking provider SDK off the async worker event loop."""
    return await asyncio.to_thread(
        _transcribe_sync,
        audio_path,
        mode=mode,
        custom_vocabulary=custom_vocabulary,
    )


__all__ = [
    "GeminiTranscribeError",
    "GeminiTranscribeNotConfigured",
    "build_transcription_config",
    "transcribe_archive",
]
