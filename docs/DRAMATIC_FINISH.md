# Dramatic finish

Post-render operator step for media-state-engine. Runs on the rendered vertical MP4 **after** `rendered` and **before** human approve. It does not change job status, does not call a paid provider, and does not post.

Nothing here bypasses the approve gate.

## Where it sits

```
pending → script_ready → audio_generating → audio_ready
        ↘              ↘
         rendering → rendered → [dramatic finish, local] → staged → approved → delivered
```

`staged` is still the human review hold. Drop the finished file into the job's review asset (or `data/staging/{job_id}/`) and approve only after you have watched picture + score.

## What it does

1. Drops dead holds and black frames (the 9s flash-to-black in gym exports is the usual offender).
2. Tightens with per-beat speed. `speed > 1` shortens. Hero beats (squat) stay under 1.0.
3. Push-in, industrial grade (contrast, bone highlights, amber shadow, vignette, grain), 1–2 frame white/black cut flashes.
4. Synthesizes a hit-synced score when source audio is room tone or missing: drone, chain, whoosh, squat impact + heartbeat, pencil bed, end sting.

Default beat sheet is the JayLeeFit gym export (pushup → stare → walk → drink → smile → portrait → squat → lock → log). Override with `--beats` for the next render.

## Run

```bash
python scripts/dramatic_finish.py INPUT.mp4 -o finished.mp4
python scripts/dramatic_finish.py INPUT.mp4 --beats beats.json -o finished.mp4
```

Requires `ffmpeg`, `ffprobe`, and `numpy`. No network. Writes a sidecar `finished.json` with beat in/out points.

Beat object:

```json
{"name": "squat", "start": 17.50, "end": 19.55, "speed": 0.88, "zoom": 0.07, "flash": "white", "hit": "impact"}
```

`flash`: `white`, `black`, `none`. `hit`: `chain`, `whoosh`, `impact`, `pencil`, `tick`, `hit`.

## Repeatable rule

- Do not bake a new status for this. It is a local finish on the rendered file.
- Do not auto-approve. Watch the cut, then `POST /jobs/{id}/approve`.
- Next export: copy `DEFAULT_BEATS` out to JSON, retimes only, rerun. Grade and score stay locked.
