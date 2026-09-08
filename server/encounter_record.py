"""The aligned encounter record.

The architecture calls for "one aligned record per encounter", video, audio,
transcript, and steering log under a single id. events.jsonl is the raw
append-only trail; this builds the analysis-facing view from it, so a rater, a
scorer, or a Phase-3 training job can read one file instead of replaying events.

Every actor turn is paired with the stage direction that shaped it and the
participant turn that preceded it.

Each turn's ``t`` is the event-elapsed time (seconds since session start) at
which the event was logged. It is NOT a media offset into either WAV: the
per-channel WAVs are gapless (the mic drops samples while muted, and each
assistant_audio*.wav accumulates only during agent speech), so a turn's ``t``
does not line up with the same-second position in a WAV. Treat ``t`` as an
ordering/event timeline only, not as a seek offset into the audio.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def build(session_dir: Path) -> Dict[str, Any]:
    events_path = session_dir / "events.jsonl"
    if not events_path.exists():
        return {}

    events: List[dict] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue

    start = next((e for e in events if e.get("type") == "session_start"), {})
    realtime = next((e for e in events if e.get("type") == "realtime_session_started"), {})

    turns: List[dict] = []
    directions: List[dict] = []
    for e in events:
        etype = e.get("type")
        if etype == "stage_direction":
            directions.append({k: v for k, v in e.items() if k not in ("type", "wall")})
        elif etype == "user_turn":
            turns.append({
                "t": e.get("t"), "role": "participant", "text": e.get("text"),
            })
        elif etype == "assistant_turn" and e.get("channel") == "text":
            # Text-channel agent turns never emit a steering_pair (that comes
            # only from the realtime voice runner), so reconstruct them here or
            # the record shows a one-sided conversation.
            turns.append({
                "t": e.get("t"),
                "role": "agent",
                "agent_id": e.get("agent_id"),
                "voice": None,
                "text": e.get("text"),
                "stage_direction": None,
                "instructions_sha256": None,
                "segment": None,
                "interaction": None,
            })
        elif etype == "steering_pair":
            actor = e.get("actor") or {}
            d = e.get("direction") or {}
            turns.append({
                "t": e.get("t"),
                "role": "agent",
                "agent_id": actor.get("agent_id"),
                "voice": actor.get("voice"),
                "text": actor.get("text"),
                # The direction that produced this line; null when the turn ran
                # unsteered, which is distinguishable from a missing record.
                "stage_direction": d.get("stage_direction"),
                # The planted beat this direction fired, so the analysis view can
                # label each actor turn with its trigger and ESCI items instead of
                # re-deriving them from the separate steering log.
                "trigger_id": d.get("trigger_id"),
                "esci": d.get("esci", []),
                "probing": d.get("probing"),
                "instructions_sha256": d.get("instructions_sha256"),
                "segment": d.get("segment"),
                "interaction": d.get("interaction"),
            })

    turns.sort(key=lambda t: t.get("t") or 0)

    audio = {
        "participant": "user_audio.wav" if (session_dir / "user_audio.wav").exists() else None,
        "agents": sorted(p.name for p in session_dir.glob("assistant_audio*.wav")),
        "sample_rate": 16000,
        "channels": 1,
        "format": "pcm_s16le",
    }
    # The webcam recording is uploaded browser-direct to S3 (encounters/{id}/
    # webcam.webm), not written to the session dir, so a disk glob finds nothing
    # in production. Prefer the 'video_uploaded' event the confirm endpoint writes
    # once S3 acknowledges the PUT; fall back to a local glob for dev captures.
    vid_ev = next(
        (e for e in reversed(events)
         if e.get("type") == "video_uploaded" and (e.get("bytes") or 0) > 0),
        None,
    )
    if vid_ev:
        video = [{"key": vid_ev.get("key"), "bytes": vid_ev.get("bytes")}]
    else:
        video = sorted(p.name for p in session_dir.glob("webcam*"))

    return {
        "encounter_id": session_dir.name,
        "scenario": start.get("scenario"),
        "participant_id": start.get("participant_id"),
        "provenance": {
            "gateway": realtime.get("gateway"),
            "realtime_model": realtime.get("model") or realtime.get("realtime_model"),
            "text_model": realtime.get("text_model"),
        },
        "cast": start.get("cast", []),
        "transcript": turns,
        "steering_log": directions,
        "audio": audio,
        "video": video,
        "counts": {
            "participant_turns": sum(1 for t in turns if t["role"] == "participant"),
            "agent_turns": sum(1 for t in turns if t["role"] == "agent"),
            "stage_directions": len(directions),
        },
    }


def write(session_dir: Path) -> Path:
    record = build(session_dir)
    out = session_dir / "record.json"
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
