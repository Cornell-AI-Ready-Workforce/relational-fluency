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

from .storage import replace_with_retry


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
                # The runner records these on the steering pair specifically so a
                # rater can tell a truncated or lost delivery from a bad one, and
                # they were being dropped here, at the one place a rater reads.
                # `interrupted` means the participant spoke over this line, so the
                # text is what the actor was saying rather than what was heard;
                # `transcript_missing` means the audio played but its text never
                # arrived, so an empty line is a gateway failure, not silence from
                # the character. Scoring either as a weak reply is a rating error
                # the record can prevent for the cost of three fields.
                "interrupted": bool(actor.get("interrupted")),
                "transcript_missing": bool(actor.get("transcript_missing")),
                "latency_s": actor.get("latency_total_s"),
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
    #
    # `video` lists what can be PLAYED, so it takes the last event carrying bytes
    # — last wins, matching verify_record and upload_receipt, because the page
    # retries the confirm and a "failed" event routinely sits ahead of the "ok"
    # one. An empty list therefore means "nothing to play", and on its own it
    # cannot say why, which is the defect `video_upload` below exists to fix.
    upload_events = [e for e in events if e.get("type") == "video_uploaded"]
    vid_ev = next(
        (e for e in reversed(upload_events) if (e.get("bytes") or 0) > 0), None,
    )
    if vid_ev:
        video = [{"key": vid_ev.get("key"), "bytes": vid_ev.get("bytes")}]
    else:
        video = sorted(p.name for p in session_dir.glob("webcam*"))

    # Three states, and the record has to keep them apart.
    #
    # This used to filter the events on bytes > 0 and keep nothing else, so a
    # confirm that said `status: "failed"` — captured, and the upload broke or
    # could not be confirmed — left exactly the same record as an encounter
    # whose camera was never on: `"video": []`. record.json is the artefact the
    # study ships to analysts and the evidence view renders, and on both of them
    # a lost recording read as "no video". Those are different judgements. An
    # encounter rated from the transcript because there was nothing to film is
    # measurement working as designed; one rated from the transcript because the
    # upload broke is a fault to report, and it can only be reported by somebody
    # who is told it happened. A record that cannot say which is which quietly
    # converts a recoverable fault into a permanent silence, since the bucket
    # listing that would have settled it is gone by the time anyone looks.
    #
    # `client_error` is carried through for the same reason the confirm endpoint
    # records it: the browser knows which leg broke (put_http_403,
    # confirm_timeout, abandoned) and this server does not. It is kept in its own
    # field, never merged into `error`, because `error` is this server's own
    # finding from its own HEAD and this is a claim from a client.
    if video:
        upload_state = "ok"
        upload_error = None
        client_error = None
    elif upload_events:
        last = upload_events[-1]
        upload_state = "failed"
        upload_error = last.get("error") or "no reason recorded"
        client_error = last.get("client_error")
    else:
        upload_state = "absent"
        upload_error = None
        client_error = None
    video_upload = {
        # "ok" — a recording exists. "failed" — one was made and could not be
        # stored or confirmed. "absent" — no confirmation ever arrived, so the
        # encounter was never captured (or the tab died before it could say so,
        # which is the same absence from here).
        "state": upload_state,
        "error": upload_error,
        "client_error": client_error,
        "attempts": len(upload_events),
    }

    return {
        "encounter_id": session_dir.name,
        "scenario": start.get("scenario"),
        "participant_id": start.get("participant_id"),
        # The study context, inherited from the session_start event, which
        # session.py writes precisely so this build (events.jsonl and nothing
        # else) does not have to open the manifest. Without it the
        # analysis-facing artifact could not say which run an encounter belonged
        # to or which cohort it was in, so a scorer or rater reading record.json
        # alone had no way to keep internal test traffic out of the study set.
        # Null on sessions that did not come through a run, and on records
        # rebuilt from events written before the context existed.
        "run_id": start.get("run_id"),
        "cohort": start.get("cohort"),
        "participant_key": start.get("participant_key"),
        "encounter_index": start.get("encounter_index"),
        # The planted triggers this encounter was actually run against, as the
        # spec stood at session start. Coverage is reported as fired/planted and
        # the spec files are edited between waves, so verifying an archived
        # encounter against today's YAML silently moves its denominator. Null
        # for encounters recorded before the stamp existed.
        "spec_fingerprint": start.get("spec_fingerprint"),
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
        "video_upload": video_upload,
        "counts": {
            "participant_turns": sum(1 for t in turns if t["role"] == "participant"),
            "agent_turns": sum(1 for t in turns if t["role"] == "agent"),
            "stage_directions": len(directions),
        },
    }


def write(session_dir: Path) -> Path:
    record = build(session_dir)
    out = session_dir / "record.json"
    # Temp file + rename, the same as the manifest next to it, and for the same
    # reason: this runs inside SessionStore.close while the console, the rater
    # packet builder and any verification pass may be reading record.json.
    # Writing in place truncates the file first, so a reader that arrives in
    # that window gets a partial JSON document and reports the encounter as
    # unreadable — and on Windows a reader holding the file can fail the write
    # outright. replace_with_retry covers the Windows rename window; POSIX
    # rename is atomic and never blocks.
    tmp = session_dir / "record.json.tmp"
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    replace_with_retry(tmp, out)
    return out
