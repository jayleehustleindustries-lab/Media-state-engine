#!/usr/bin/env python3
"""Dramatic finish pass for vertical gym / coaching renders.

Sits after render, before human approve, in the media-state-engine pipeline.
Tightens dead holds and black frames, adds a locked industrial grade plus
push-ins and cut flashes, and synthesizes a hit-synced score when the source
bed is room tone or missing.

Usage:
    python scripts/dramatic_finish.py INPUT.mp4 -o finished.mp4
    python scripts/dramatic_finish.py INPUT.mp4 --beats beats.json -o finished.mp4

Beats JSON is a list of objects:
    {"name": "squat", "start": 17.45, "end": 19.55, "speed": 0.90,
     "zoom": 0.10, "flash": "white"}

flash is "white", "black", or "none". speed > 1 tightens. zoom is the extra
scale at the end of the beat (0.10 = 10% push-in).
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import wave
from pathlib import Path

import numpy as np

# JayLeeFit industrial cut. Times are source seconds.
# The 9.0s black frame and the long 10-14s smile hold are dropped on purpose.
DEFAULT_BEATS = [
    {"name": "pushup", "start": 0.15, "end": 1.65, "speed": 1.12, "zoom": 0.08, "flash": "white", "hit": "chain"},
    {"name": "stare", "start": 2.15, "end": 3.55, "speed": 1.04, "zoom": 0.10, "flash": "black", "hit": "hit"},
    {"name": "walk", "start": 4.05, "end": 5.40, "speed": 1.18, "zoom": 0.05, "flash": "white", "hit": "whoosh"},
    {"name": "drink", "start": 6.20, "end": 7.65, "speed": 1.08, "zoom": 0.11, "flash": "black", "hit": "hit"},
    {"name": "smile", "start": 8.05, "end": 8.85, "speed": 1.00, "zoom": 0.06, "flash": "white", "hit": "tick"},
    {"name": "portrait", "start": 10.40, "end": 12.35, "speed": 0.98, "zoom": 0.12, "flash": "black", "hit": "hit"},
    {"name": "squat", "start": 17.50, "end": 19.55, "speed": 0.88, "zoom": 0.07, "flash": "white", "hit": "impact"},
    {"name": "lock", "start": 20.05, "end": 21.50, "speed": 1.00, "zoom": 0.11, "flash": "black", "hit": "hit"},
    {"name": "log", "start": 22.30, "end": 25.55, "speed": 1.06, "zoom": 0.08, "flash": "none", "hit": "pencil"},
]

WIDTH = 1080
HEIGHT = 1920
FPS = 24
SR = 44100


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {' '.join(cmd[:6])}...\n{tail}")


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(proc.stdout.strip())


def render_beat(src: Path, beat: dict, out: Path) -> float:
    start = float(beat["start"])
    end = float(beat["end"])
    speed = float(beat.get("speed", 1.0))
    zoom = float(beat.get("zoom", 0.08))
    dur = max(0.2, (end - start) / speed)
    vf = (
        f"setpts=PTS/{speed:.4f},"
        f"scale=w='trunc({WIDTH}*(1+{zoom:.4f}*min(1\\,t/{dur:.4f}))/2)*2':"
        f"h='trunc({HEIGHT}*(1+{zoom:.4f}*min(1\\,t/{dur:.4f}))/2)*2':eval=frame,"
        f"crop={WIDTH}:{HEIGHT}:(iw-ow)/2:(ih-oh)/2,"
        "eq=contrast=1.18:brightness=-0.035:saturation=0.78:gamma=0.95,"
        "colorbalance=rs=0.06:gs=-0.02:bs=-0.09:rm=0.03:gm=-0.01:bm=-0.04,"
        "vignette=PI/4.4,"
        "noise=alls=7:allf=t+u,"
        "format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(src), "-an", "-vf", vf, "-r", str(FPS),
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-threads", "4",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
    ])
    return probe_duration(out)


def render_flash(kind: str, out: Path) -> float:
    color = "white" if kind == "white" else "0x070708"
    dur = 0.06 if kind == "white" else 0.07
    run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c={color}:s={WIDTH}x{HEIGHT}:r={FPS}:d={dur}",
        "-vf", "format=yuv420p",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-threads", "4",
        "-pix_fmt", "yuv420p", "-r", str(FPS), str(out),
    ])
    return probe_duration(out)


def concat_clips(clips: list[Path], out: Path) -> float:
    lst = out.with_suffix(".concat.txt")
    lines = [f"file '{clip.resolve()}'" for clip in clips]
    lst.write_text("\n".join(lines) + "\n")
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-threads", "4",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-an",
        "-movflags", "+faststart", str(out),
    ])
    return probe_duration(out)


def _exp(n: int, sr: int, decay: float) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / sr
    return np.exp(-t / decay)


def tone_hit(sr: int, dur: float = 0.42, f0: float = 62.0, amp: float = 1.0) -> np.ndarray:
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    freq = f0 * np.exp(-t * 5.5)
    phase = 2 * math.pi * np.cumsum(freq) / sr
    body = np.sin(phase) * _exp(n, sr, 0.12)
    sub = np.sin(2 * math.pi * 40 * t) * _exp(n, sr, 0.18)
    click = np.random.default_rng(7).standard_normal(n) * _exp(n, sr, 0.02)
    return amp * (0.72 * body + 0.4 * sub + 0.18 * click)


def whoosh(sr: int, dur: float = 0.38, amp: float = 0.55) -> np.ndarray:
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(n)
    env = np.sin(np.pi * np.clip(t / dur, 0, 1)) ** 1.4
    sweep = np.sin(2 * math.pi * (180 + 900 * (t / dur)) * t) * 0.25
    return amp * (noise * 0.55 + sweep) * env


def chain_rattle(sr: int, dur: float = 0.7, amp: float = 0.4) -> np.ndarray:
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    rng = np.random.default_rng(19)
    noise = rng.standard_normal(n)
    gate = (np.sin(2 * math.pi * 14 * t) > 0.2).astype(np.float64)
    ring = (
        np.sin(2 * math.pi * 1680 * t) * 0.25
        + np.sin(2 * math.pi * 2420 * t) * 0.15
    ) * _exp(n, sr, 0.2)
    return amp * (noise * 0.35 * gate + ring) * _exp(n, sr, 0.28)


def pencil(sr: int, dur: float, amp: float = 0.16) -> np.ndarray:
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    rng = np.random.default_rng(23)
    noise = rng.standard_normal(n)
    scratch = np.abs(np.sin(2 * math.pi * 7.5 * t)) ** 3
    page = np.zeros(n)
    flip_at = int(0.55 * n)
    page[flip_at:flip_at + int(0.08 * sr)] = rng.standard_normal(int(0.08 * sr)) * np.linspace(1, 0, int(0.08 * sr))
    return amp * (noise * scratch * 0.8 + page * 1.4)


def heartbeat(sr: int, dur: float, bpm: float = 66.0, amp: float = 0.28) -> np.ndarray:
    n = int(sr * dur)
    out = np.zeros(n, dtype=np.float64)
    period = 60.0 / bpm
    t0 = 0.05
    while t0 < dur - 0.05:
        kick = tone_hit(sr, 0.18, 48.0, amp)
        i = int(t0 * sr)
        end = min(n, i + kick.size)
        out[i:end] += kick[: end - i]
        kick2 = tone_hit(sr, 0.12, 58.0, amp * 0.45)
        j = int((t0 + 0.18) * sr)
        end2 = min(n, j + kick2.size)
        if j < n:
            out[j:end2] += kick2[: end2 - j]
        t0 += period
    return out


def drone(sr: int, dur: float) -> np.ndarray:
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    lfo = 0.72 + 0.28 * np.sin(2 * math.pi * 0.07 * t)
    sig = (
        np.sin(2 * math.pi * 43.0 * t) * 0.55
        + np.sin(2 * math.pi * 43.4 * t) * 0.35
        + np.sin(2 * math.pi * 86.0 * t) * 0.08
        + np.sin(2 * math.pi * 64.5 * t + 0.4) * 0.12
    )
    air = np.random.default_rng(3).standard_normal(n) * 0.015
    fade = np.ones(n)
    fin = int(0.4 * sr)
    fade[:fin] = np.linspace(0, 1, fin)
    fade[-int(0.8 * sr):] = np.linspace(1, 0, int(0.8 * sr))[: fade[-int(0.8 * sr):].size]
    return (sig * lfo * 0.20 + air) * fade


def riser(sr: int, dur: float = 1.1, amp: float = 0.35) -> np.ndarray:
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    rng = np.random.default_rng(31)
    noise = rng.standard_normal(n)
    env = (t / dur) ** 1.6
    tone = np.sin(2 * math.pi * (90 + 220 * (t / dur)) * t)
    return amp * (noise * 0.4 + tone * 0.25) * env


def place(buf: np.ndarray, sig: np.ndarray, at: float, sr: int) -> None:
    i = max(0, int(at * sr))
    end = min(buf.size, i + sig.size)
    if i >= buf.size:
        return
    buf[i:end] += sig[: end - i]


def mix_add(*sigs: np.ndarray) -> np.ndarray:
    n = max(s.size for s in sigs)
    out = np.zeros(n, dtype=np.float64)
    for sig in sigs:
        out[: sig.size] += sig
    return out


def synthesize(timeline: list[dict], total: float, out: Path) -> None:
    n = int((total + 0.05) * SR)
    left = np.zeros(n, dtype=np.float64)
    right = np.zeros(n, dtype=np.float64)
    bed = drone(SR, total + 0.05)
    left += bed
    right += bed * 0.92
    cursor = 0.0
    squat_at = None
    log_at = None
    log_dur = 0.0
    for item in timeline:
        if item["kind"] == "beat":
            hit = item.get("hit", "hit")
            if hit == "chain":
                sig = mix_add(chain_rattle(SR, 0.8, 0.55), tone_hit(SR, 0.36, 55, 0.7))
            elif hit == "whoosh":
                sig = mix_add(whoosh(SR, 0.4, 0.6), tone_hit(SR, 0.28, 70, 0.45))
            elif hit == "impact":
                sig = mix_add(tone_hit(SR, 0.55, 42, 1.15), whoosh(SR, 0.3, 0.4))
                squat_at = cursor
            elif hit == "pencil":
                log_at = cursor
                log_dur = item["dur"]
                sig = tone_hit(SR, 0.22, 90, 0.28)
            elif hit == "tick":
                sig = tone_hit(SR, 0.16, 140, 0.28)
            else:
                sig = tone_hit(SR, 0.34, 68, 0.62)
            place(left, sig, cursor, SR)
            place(right, sig * 0.86, cursor + 0.004, SR)
            cursor += item["dur"]
        else:
            cursor += item["dur"]
    if squat_at is not None:
        place(left, riser(SR, 1.05, 0.42), max(0.0, squat_at - 1.0), SR)
        place(right, riser(SR, 1.05, 0.36), max(0.0, squat_at - 0.98), SR)
        hb = heartbeat(SR, 2.4, 72, 0.34)
        place(left, hb, squat_at, SR)
        place(right, hb * 0.9, squat_at + 0.01, SR)
    if log_at is not None:
        pen = pencil(SR, max(0.4, log_dur), 0.22)
        place(left, pen, log_at + 0.15, SR)
        place(right, pen * 0.8, log_at + 0.17, SR)
    sting = tone_hit(SR, 0.7, 48, 0.85)
    place(left, sting, max(0.0, total - 0.55), SR)
    place(right, sting * 0.9, max(0.0, total - 0.54), SR)
    mix = np.stack([left, right], axis=1)
    peak = np.max(np.abs(mix)) or 1.0
    mix = np.tanh(mix / peak * 1.15) * 0.89
    pcm = np.clip(mix * 32767, -32767, 32767).astype(np.int16)
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def mux(video: Path, audio: Path, out: Path, total: float) -> None:
    run([
        "ffmpeg", "-y", "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-shortest",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-af", f"afade=t=in:st=0:d=0.12,afade=t=out:st={max(0.2, total - 0.45):.3f}:d=0.4",
        "-movflags", "+faststart", str(out),
    ])


def finish(src: Path, dest: Path, beats: list[dict], work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    timeline: list[dict] = []
    cursor = 0.0
    for i, beat in enumerate(beats):
        flash = beat.get("flash", "none")
        if flash in {"white", "black"} and i > 0:
            flash_path = work / f"flash_{i:02d}.mp4"
            fd = render_flash(flash, flash_path)
            clips.append(flash_path)
            timeline.append({"kind": "flash", "dur": fd, "at": cursor})
            cursor += fd
        clip = work / f"beat_{i:02d}_{beat['name']}.mp4"
        dur = render_beat(src, beat, clip)
        clips.append(clip)
        timeline.append({
            "kind": "beat",
            "name": beat["name"],
            "dur": dur,
            "at": cursor,
            "hit": beat.get("hit", "hit"),
        })
        cursor += dur
    silent = work / "picture.mp4"
    total = concat_clips(clips, silent)
    wav = work / "score.wav"
    synthesize(timeline, total, wav)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    mux(silent, wav, tmp, total)
    tmp.replace(dest)
    report = {
        "source": str(src),
        "output": str(dest),
        "duration": round(total, 3),
        "beats": timeline,
        "grade": "industrial contrast, bone highlights, amber-shadow colorbalance, vignette, grain",
        "audio": "synthesized drone, cut hits, chain, squat impact + heartbeat, pencil bed",
    }
    (dest.with_suffix(".json")).write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Dramatic finish pass for vertical renders")
    parser.add_argument("src", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--beats", type=Path, help="Optional beats JSON override")
    parser.add_argument("--work", type=Path, default=Path("/tmp/dramatic_finish"))
    args = parser.parse_args()
    beats = json.loads(args.beats.read_text()) if args.beats else DEFAULT_BEATS
    report = finish(args.src, args.output, beats, args.work)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
