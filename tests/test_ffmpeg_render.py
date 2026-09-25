"""Tests for app/services/ffmpeg_render.py.

Per the task brief: ffmpeg IS actually installed in this environment (via
the imageio-ffmpeg pip package), so this exercises it for REAL against small
synthetic clips generated with ffmpeg's own lavfi test sources — no mocking
of subprocess or ffmpeg itself. This is free (no API key, no network) and
validates the ported render_core.py recipe end to end, not just its Python
glue.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services import ffmpeg_render as fr


def _ffmpeg_bin() -> str:
    found = shutil.which('ffmpeg')
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = _ffmpeg_bin()


def _make_clip(path: Path, *, seconds: float = 3.0, color: str = 'red') -> None:
    """Generate a tiny synthetic portrait clip with video + audio via lavfi."""
    cmd = [
        FFMPEG, '-y',
        '-f', 'lavfi', '-i', f'color=c={color}:size=720x1280:duration={seconds}:rate=15',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
        '-shortest', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-c:a', 'aac',
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]


def _probe_duration_seconds(path: Path) -> float:
    """No ffprobe binary bundled with imageio-ffmpeg; parse ffmpeg -i's stderr."""
    result = subprocess.run([FFMPEG, '-i', str(path)], capture_output=True, text=True)
    m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', result.stderr)
    assert m, f'could not find Duration in ffmpeg -i output:\n{result.stderr}'
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


@pytest.fixture(autouse=True)
def _use_bundled_font(monkeypatch):
    # Deterministic: don't depend on ffmpeg_caption_font_path being unset in env.
    monkeypatch.setattr(settings, 'ffmpeg_caption_font_path', '')


# ---------------------------------------------------------------------------
# parse_bracket_script — pure, no ffmpeg needed
# ---------------------------------------------------------------------------

def test_parse_bracket_script_splits_sentences_proportionally():
    text = (
        "[0:00-0:03] Stop scrolling! "
        "[0:03-0:15] Here's the move. Do it today. "
        "[0:15-0:23] Follow for more."
    )
    captions = fr.parse_bracket_script(text)
    assert len(captions) == 4
    assert captions[0] == (0.0, 3.0, 'Stop scrolling!')
    assert captions[1][0] == 3.0
    assert captions[-1][1] == 23.0
    assert captions[-1][2] == 'Follow for more.'


def test_parse_bracket_script_raises_without_brackets():
    with pytest.raises(fr.FfmpegRenderError):
        fr.parse_bracket_script("just plain text, no timing at all")


def test_parse_captions_best_effort_returns_empty_list_instead_of_raising():
    assert fr.parse_captions_best_effort("plain hook body cta, no brackets") == []


# ---------------------------------------------------------------------------
# ffmpeg binary discovery (config check)
# ---------------------------------------------------------------------------

def test_require_ffmpeg_override_missing_raises_not_configured(monkeypatch):
    monkeypatch.setattr(settings, 'ffmpeg_bin_path', '/no/such/ffmpeg/binary')
    with pytest.raises(fr.FfmpegNotConfigured):
        fr._require_ffmpeg()


def test_require_ffmpeg_falls_back_to_imageio_ffmpeg(monkeypatch):
    monkeypatch.setattr(settings, 'ffmpeg_bin_path', '')
    monkeypatch.setattr(fr.shutil, 'which', lambda *_a, **_k: None)
    found = fr._require_ffmpeg()
    assert Path(found).exists()


def test_require_ffmpeg_explicit_arg_wins(monkeypatch):
    assert fr._require_ffmpeg(FFMPEG) == FFMPEG


# ---------------------------------------------------------------------------
# render_post — REAL ffmpeg against synthetic clips
# ---------------------------------------------------------------------------

def test_render_post_produces_captioned_video_from_real_clips(tmp_path):
    clip_paths = []
    for i, color in enumerate(['red', 'green', 'blue']):
        p = tmp_path / f'clip_{i}.mp4'
        _make_clip(p, seconds=10.0, color=color)
        clip_paths.append(str(p))

    captions = [(0.0, 3.0, 'Stop scrolling!'), (3.0, 15.0, "Here's the move."), (15.0, 23.0, 'Follow for more.')]
    output = tmp_path / 'final.mp4'
    result_path = fr.render_post([str(p) for p in clip_paths], captions, str(output))

    assert result_path == str(output)
    assert output.exists()
    assert output.stat().st_size > 1000
    duration = _probe_duration_seconds(output)
    # 3 clips trimmed to TARGET_TOTAL_SECONDS/3 each then concatenated
    assert abs(duration - fr.TARGET_TOTAL_SECONDS) < 1.0


def test_render_post_without_captions_still_concatenates(tmp_path):
    # 2 clips -> TARGET_TOTAL_SECONDS/2 = 12.5s trimmed each; clips must be
    # at least that long or the trim is a no-op and total duration is short.
    clip_paths = []
    for i in range(2):
        p = tmp_path / f'clip_{i}.mp4'
        _make_clip(p, seconds=14.0, color='red' if i == 0 else 'blue')
        clip_paths.append(str(p))

    output = tmp_path / 'final_no_captions.mp4'
    fr.render_post(clip_paths, [], str(output))
    assert output.exists()
    duration = _probe_duration_seconds(output)
    assert abs(duration - fr.TARGET_TOTAL_SECONDS) < 1.0


def test_render_post_requires_at_least_one_clip():
    with pytest.raises(fr.FfmpegRenderError):
        fr.render_post([], [], '/tmp/whatever.mp4')


def test_render_post_missing_clip_file_raises(tmp_path):
    with pytest.raises(fr.FfmpegRenderError):
        fr.render_post([str(tmp_path / 'does_not_exist.mp4')], [], str(tmp_path / 'out.mp4'))


# ---------------------------------------------------------------------------
# async render() entrypoint — matches remotion.render()'s return shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_render_matches_provider_return_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'asset_storage_dir', str(tmp_path / 'assets'))
    clip_paths = []
    for i in range(3):
        p = tmp_path / f'clip_{i}.mp4'
        _make_clip(p, seconds=9.0, color=['red', 'green', 'blue'][i])
        clip_paths.append(str(p))

    job = {
        'id': 'job-async-1',
        'script_text': "[0:00-0:03] Hook! [0:03-0:15] Body line. [0:15-0:23] CTA line.",
    }
    result = await fr.render(job, clip_paths)

    assert result['storage_path'] == 'final/job-async-1.mp4'
    assert result['url'] is None
    assert result['meta']['provider'] == 'ffmpeg'
    assert result['meta']['clip_count'] == 3
    assert result['meta']['caption_count'] == 3

    out_file = tmp_path / 'assets' / 'final' / 'job-async-1.mp4'
    assert out_file.exists()
    assert out_file.stat().st_size > 1000


@pytest.mark.asyncio
async def test_async_render_without_bracket_script_renders_uncaptioned(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'asset_storage_dir', str(tmp_path / 'assets'))
    p = tmp_path / 'clip_0.mp4'
    _make_clip(p, seconds=25.0, color='red')

    job = {'id': 'job-async-2', 'script_text': 'plain hook body cta with no bracket timings'}
    result = await fr.render(job, [str(p)])
    assert result['meta']['caption_count'] == 0
    out_file = tmp_path / 'assets' / 'final' / 'job-async-2.mp4'
    assert out_file.exists()


@pytest.mark.asyncio
async def test_async_render_not_configured_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'ffmpeg_bin_path', '/no/such/binary')
    with pytest.raises(fr.FfmpegNotConfigured):
        await fr.render({'id': 'job-x', 'script_text': ''}, ['/tmp/whatever.mp4'])
