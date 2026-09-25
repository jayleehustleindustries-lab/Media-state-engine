"""Local ffmpeg render provider: stitch N Vertex clips + burned-in captions
into one finished vertical MP4.

Structurally mirrors ``remotion.py``'s provider-module shape (an async
``render(job, ...)`` returning ``{'url', 'storage_path', 'meta'}``), but this
provider runs a local subprocess instead of calling a render API, so its
config check verifies the ``ffmpeg`` binary is findable (``shutil.which`` /
the ``imageio-ffmpeg`` pip package) rather than an API key, and blocking
subprocess + PIL work is wrapped in ``asyncio.to_thread`` so it never blocks
the worker's event loop (see ``app/worker.py``'s dispatch pattern).

The trim/concat/caption-overlay recipe (``parse_bracket_script`` /
``render_post``) is ported near-verbatim from a working, already
manually-validated-against-real-clips pure function developed this session
(``render_core.py``) rather than rewritten from scratch — only the config
check, exception types, and the async worker-facing entrypoint are new.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from ..config import settings
from . import storage

CANVAS_W, CANVAS_H = 720, 1280
FONT_SIZE = 46
TARGET_TOTAL_SECONDS = 25.0

_DEFAULT_FONT_CANDIDATES = [
    "/mnt/skills/examples/canvas-design/canvas-fonts/Outfit-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

_BRACKET_RE = re.compile(
    r"\[(\d+):(\d+)-(\d+):(\d+)\]\s*(.*?)(?=\[\d+:\d+-\d+:\d+\]|$)", re.DOTALL
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


class FfmpegError(RuntimeError):
    """Base class for expected ffmpeg render errors."""


class FfmpegNotConfigured(FfmpegError):
    pass


class FfmpegRenderError(FfmpegError):
    pass


def _require_ffmpeg(ffmpeg_bin: str | None = None) -> str:
    """Config check for this provider: find a working ffmpeg binary.

    Precedence: explicit arg -> FFMPEG_BIN_PATH setting -> PATH -> the
    imageio-ffmpeg pip package's bundled static binary.
    """
    if ffmpeg_bin:
        if Path(ffmpeg_bin).exists():
            return ffmpeg_bin
        raise FfmpegNotConfigured(f"ffmpeg_bin override not found: {ffmpeg_bin}")
    override = (getattr(settings, "ffmpeg_bin_path", "") or "").strip()
    if override:
        if Path(override).exists():
            return override
        raise FfmpegNotConfigured(f"FFMPEG_BIN_PATH is set but not found: {override}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise FfmpegNotConfigured(
            "ffmpeg is not configured: no system ffmpeg on PATH and imageio-ffmpeg "
            "is not installed (pip install imageio-ffmpeg), or set FFMPEG_BIN_PATH"
        ) from exc


def _require_font() -> str:
    override = (getattr(settings, "ffmpeg_caption_font_path", "") or "").strip()
    candidates = ([override] if override else []) + _DEFAULT_FONT_CANDIDATES
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FfmpegRenderError(f"No caption font found. Looked for: {candidates}")


# ---------------------------------------------------------------------------
# Script -> caption timing (ported from render_core.parse_bracket_script)
# ---------------------------------------------------------------------------

def parse_bracket_script(script_text: str) -> list[tuple[float, float, str]]:
    """Parse a ``[mm:ss-mm:ss] text ...`` bracket-format script (this repo's
    hook/body/cta script text, when it carries explicit timing brackets) into
    a flat list of ``(start_seconds, end_seconds, caption_text)`` tuples, one
    per sentence, with each bracket's time window divided proportionally
    across its sentences by character count.
    """
    matches = _BRACKET_RE.findall(script_text or "")
    if not matches:
        raise FfmpegRenderError(
            "No [mm:ss-mm:ss] bracket segments found in script text."
        )

    captions: list[tuple[float, float, str]] = []
    for m0, s0, m1, s1, text in matches:
        start = int(m0) * 60 + int(s0)
        end = int(m1) * 60 + int(s1)
        text = text.strip()
        if not text:
            continue
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
        if not sentences:
            continue
        total_chars = sum(len(s) for s in sentences) or 1
        window = max(end - start, 0.1)
        cursor = float(start)
        for i, sentence in enumerate(sentences):
            share = len(sentence) / total_chars
            duration = window * share
            seg_end = end if i == len(sentences) - 1 else cursor + duration
            captions.append((round(cursor, 2), round(seg_end, 2), sentence))
            cursor = seg_end
    return captions


def parse_captions_best_effort(script_text: str) -> list[tuple[float, float, str]]:
    """Like ``parse_bracket_script`` but returns ``[]`` instead of raising
    when the text carries no ``[mm:ss-mm:ss]`` brackets, so a render can
    still proceed (uncaptioned) for plain hook/body/cta script text."""
    try:
        return parse_bracket_script(script_text)
    except FfmpegRenderError:
        return []


# ---------------------------------------------------------------------------
# Caption PNG rendering (ported from render_core._render_caption_png)
# ---------------------------------------------------------------------------

def _render_caption_png(text: str, out_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font_path = _require_font()
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, FONT_SIZE)
    lines = textwrap.fill(text, width=26).split("\n")

    line_heights, max_w = [], 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=4)
        line_heights.append(bbox[3] - bbox[1])
        max_w = max(max_w, bbox[2] - bbox[0])

    line_gap = 12
    pad_x, pad_y = 36, 24
    total_h = sum(line_heights) + line_gap * (len(lines) - 1)
    box_w, box_h = max_w + pad_x * 2, total_h + pad_y * 2
    box_x0 = (CANVAS_W - box_w) / 2
    box_y0 = CANVAS_H - 340 - box_h

    draw.rounded_rectangle(
        [box_x0, box_y0, box_x0 + box_w, box_y0 + box_h], radius=18, fill=(0, 0, 0, 140)
    )
    y = box_y0 + pad_y
    for line, lh in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=4)
        x = (CANVAS_W - (bbox[2] - bbox[0])) / 2
        draw.text(
            (x, y), line, font=font, fill=(255, 255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0, 255)
        )
        y += lh + line_gap
    img.save(out_path)


# ---------------------------------------------------------------------------
# ffmpeg trim/concat/overlay (ported from render_core._run / render_post)
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FfmpegRenderError(f"Command failed ({' '.join(cmd)}):\n{result.stderr[-4000:]}")


def render_post(
    clip_paths: list[str],
    captions: list[tuple[float, float, str]],
    output_path: str,
    *,
    ffmpeg_bin: str | None = None,
) -> str:
    """Pure, blocking function: N local clip files (assumed roughly equal
    length, e.g. 3x ~8-10s Vertex clips) + a caption timing list -> one
    finished vertical MP4 with burned-in captions, written to output_path.

    No network I/O. Caller downloads clip inputs and uploads/serves the
    result. Call via ``render()`` below from async code (wrapped in
    ``asyncio.to_thread``), not directly from the event loop.
    """
    if not clip_paths:
        raise FfmpegRenderError("render_post requires at least one clip path.")
    ffmpeg = _require_ffmpeg(ffmpeg_bin)

    with tempfile.TemporaryDirectory(prefix="ffmpeg_render_") as tmp:
        tmp_dir = Path(tmp)
        per_clip_seconds = TARGET_TOTAL_SECONDS / len(clip_paths)

        trimmed = []
        for i, clip in enumerate(clip_paths):
            clip_path = Path(clip)
            if not clip_path.exists():
                raise FfmpegRenderError(f"Clip not found: {clip}")
            out = tmp_dir / f"trim_{i}.mp4"
            _run(
                [ffmpeg, "-y", "-i", str(clip_path), "-t", f"{per_clip_seconds:.3f}", "-c", "copy", str(out)],
                cwd=tmp_dir,
            )
            trimmed.append(out)

        filelist = tmp_dir / "filelist.txt"
        filelist.write_text("\n".join(f"file '{p.name}'" for p in trimmed))
        combined = tmp_dir / "combined_raw.mp4"
        _run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(filelist), "-c", "copy", str(combined)],
            cwd=tmp_dir,
        )

        if not captions:
            final_out = Path(output_path)
            final_out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(combined, output_path)
            return output_path

        caption_files = []
        for i, (_, _, text) in enumerate(captions):
            png = tmp_dir / f"cap_{i:02d}.png"
            _render_caption_png(text, png)
            caption_files.append(png)

        inputs = ["-i", str(combined)]
        for png in caption_files:
            inputs += ["-i", str(png)]

        filter_parts = []
        prev = "0:v"
        for i, (start, end, _) in enumerate(captions):
            label = f"v{i}"
            filter_parts.append(
                f"[{prev}][{i + 1}:v]overlay=0:0:enable='between(t,{start},{end})'[{label}]"
            )
            prev = label
        filter_complex = ";".join(filter_parts)

        final_out = Path(output_path)
        final_out.parent.mkdir(parents=True, exist_ok=True)
        cmd = (
            [ffmpeg, "-y"] + inputs
            + ["-filter_complex", filter_complex, "-map", f"[{prev}]", "-map", "0:a?",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(final_out)]
        )
        _run(cmd, cwd=tmp_dir)

    return output_path


# ---------------------------------------------------------------------------
# Async provider entrypoint — matches remotion.render()'s return shape
# ---------------------------------------------------------------------------

async def render(job: dict, clip_paths: list[str], *, storage_relative_path: str | None = None) -> dict:
    """Async entrypoint used by ``pipeline.render_vertex``.

    Mirrors ``remotion.render(job, audio)``'s return shape
    (``{'url', 'storage_path', 'meta'}``) so the pipeline's asset-insert code
    can stay identical across providers. All blocking ffmpeg/PIL work runs
    off the event loop via ``asyncio.to_thread``.
    """
    _require_ffmpeg()  # fail fast (config check) before touching the thread pool

    job_id = job.get("id") if isinstance(job, dict) else job["id"]
    script_text = job.get("script_text") if isinstance(job, dict) else job["script_text"]
    captions = parse_captions_best_effort(script_text or "")

    relative = storage_relative_path or f"final/{job_id}.mp4"
    output_path = storage.absolute_path(relative)

    def _do_render() -> str:
        return render_post(clip_paths, captions, output_path)

    await asyncio.to_thread(_do_render)

    return {
        "url": None,
        "storage_path": relative,
        "meta": {
            "provider": "ffmpeg",
            "clip_count": len(clip_paths),
            "caption_count": len(captions),
            "duration_seconds": TARGET_TOTAL_SECONDS,
        },
    }
