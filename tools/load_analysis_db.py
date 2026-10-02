#!/usr/bin/env python3
"""Load the analysis database (docs/db-schema.sql) from the encounter archive.

Inputs, all files the platform already produces:

  --archive DIR     a local copy of s3://relational-fluency-study-data/encounters/
                    (one folder per encounter: manifest.json, record.json,
                    events.jsonl, recovered_transcript.md; media are not needed)
  --scenarios DIR   scenarios/v3 (the YAML bank)
  --runs FILE       optional: the /api/runs export (JSON list), which fills the
                    run rows properly; without it runs are stubbed from what the
                    encounters say about themselves.

Idempotent: every row is upserted on its natural key and the per-encounter
child rows are replaced, so re-running after a fresh `aws s3 sync` is the
whole refresh. Requires psycopg 3 and pyyaml.

  aws s3 sync s3://relational-fluency-study-data/encounters/ ~/Desktop/RF_archive/encounters/ \
      --exclude "*" --include "*.json" --include "*.jsonl" --include "*.md"
  python tools/load_analysis_db.py --dsn postgresql://postgres:rf@localhost:5433/rf \
      --archive ~/Desktop/RF_archive/encounters --scenarios scenarios/v3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - exercised on CI, which has no driver
    # The mapping functions (load_encounter, load_runs, ...) only build rows and
    # hand them to a cursor, so they are importable and testable without the
    # Postgres driver; tests/test_review_fixes.py drives them with a fake
    # cursor on a CI runner that does not install it. Writing to a real
    # database is what needs the driver, and main() says so.
    psycopg = None

    class Jsonb:  # the shape psycopg's Jsonb exposes to a cursor: .obj
        def __init__(self, obj):
            self.obj = obj

CONSTRUCT_LABELS = {
    "conflict_management": "Conflict Management",
    "influence": "Influence",
    "inspirational_leadership": "Inspirational Leadership",
    "teamwork": "Teamwork",
}


def ts(epoch: Optional[float]) -> Optional[datetime]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc)


def read_json(p: Path) -> Optional[Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_events(p: Path) -> List[dict]:
    out: List[dict] = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# 1. Scenario bank
# ---------------------------------------------------------------------------

def load_scenarios(cur, scen_dir: Path) -> int:
    n = 0
    for f in sorted(scen_dir.glob("S*.yaml")):
        raw = f.read_bytes()
        spec = yaml.safe_load(raw)
        sid = spec["id"]
        construct = spec["construct"]
        cur.execute(
            "INSERT INTO construct (construct, label) VALUES (%s, %s) "
            "ON CONFLICT (construct) DO NOTHING",
            (construct, CONSTRUCT_LABELS.get(construct, construct)))
        for key, label in (spec.get("esci_items") or {}).items():
            cur.execute(
                "INSERT INTO esci_item (construct, item_key, label, reverse_keyed) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (construct, item_key) DO UPDATE "
                "SET label = EXCLUDED.label, reverse_keyed = EXCLUDED.reverse_keyed",
                (construct, key, str(label), key.endswith("_r")))
        dur = spec.get("duration_minutes") or [7, 12]
        if not isinstance(dur, list):
            dur = [dur, dur]
        cur.execute(
            """INSERT INTO scenario (scenario_id, construct, variant, parallel_form, title,
                   duration_min_minutes, duration_max_minutes, skill_measured, setup,
                   spec_sha256)
               VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (scenario_id) DO UPDATE SET
                   construct = EXCLUDED.construct, variant = EXCLUDED.variant,
                   title = EXCLUDED.title, duration_min_minutes = EXCLUDED.duration_min_minutes,
                   duration_max_minutes = EXCLUDED.duration_max_minutes,
                   skill_measured = EXCLUDED.skill_measured, setup = EXCLUDED.setup,
                   spec_sha256 = EXCLUDED.spec_sha256""",
            (sid, construct, spec.get("variant", sid[-1]), spec.get("title", sid),
             int(dur[0]), int(dur[-1]), spec.get("skill_measured"), spec.get("setup"),
             hashlib.sha256(raw).hexdigest()))
        cur.execute("DELETE FROM planted_trigger WHERE scenario_id = %s", (sid,))
        cur.execute("DELETE FROM interaction WHERE scenario_id = %s", (sid,))
        cur.execute("DELETE FROM scenario_agent WHERE scenario_id = %s", (sid,))
        for aid, a in (spec.get("agents") or {}).items():
            rv = a.get("realtime_voice") or {}
            prompt = a.get("system_prompt")
            cur.execute(
                """INSERT INTO scenario_agent (scenario_id, agent_id, name, display_role,
                       internal_role, voice_gemini_live, voice_gpt_realtime, system_prompt,
                       system_prompt_sha256)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (sid, aid, a.get("name", aid), a.get("display_role"), a.get("role"),
                 rv.get("gemini-live") or a.get("voice"), rv.get("gpt-realtime"), prompt,
                 hashlib.sha256(prompt.encode()).hexdigest() if prompt else None))
        for pos, it in enumerate(spec.get("interactions") or [], start=1):
            agents = it.get("agents") or ([it["agent"]] if it.get("agent") else [])
            cur.execute(
                """INSERT INTO interaction (scenario_id, interaction_id, position, mode, kind,
                       label, opening, observe, agents)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (sid, it["id"], pos, it.get("mode", "one_to_one"), it.get("kind"),
                 it.get("label", it["id"]), it.get("opening"), it.get("observe"), agents))
            for tpos, tr in enumerate(it.get("triggers") or [], start=1):
                sc = tr.get("scores") or {}
                hi, lo = sc.get("high") or {}, sc.get("low") or {}
                cur.execute(
                    """INSERT INTO planted_trigger (scenario_id, interaction_id, trigger_id,
                           position, cue, on_silence, esci_items, high_answer, high_why,
                           low_answer, low_why)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (sid, it["id"], tr["id"], tpos, tr.get("cue", ""), tr.get("on_silence"),
                     list(tr.get("esci") or []), hi.get("answer"), hi.get("why"),
                     lo.get("answer"), lo.get("why")))
        n += 1
    # parallel_form after every scenario exists (self-referencing FK)
    for f in sorted(scen_dir.glob("S*.yaml")):
        spec = yaml.safe_load(f.read_text(encoding="utf-8"))
        pf = spec.get("parallel_form")
        if isinstance(pf, str):
            cur.execute("UPDATE scenario SET parallel_form = %s WHERE scenario_id = %s "
                        "AND EXISTS (SELECT 1 FROM scenario WHERE scenario_id = %s)",
                        (pf, spec["id"], pf))
    return n


# ---------------------------------------------------------------------------
# 2/3/4. Encounters and everything under them
# ---------------------------------------------------------------------------

def upsert_participant(cur, pid: Optional[str], cohort: str, key: Optional[str],
                       created: Optional[float], run_id: Optional[str]) -> None:
    if not pid:
        return
    if created is None and pid.startswith("p_"):
        try:
            created = float(pid.split("_")[1])
        except (IndexError, ValueError):
            created = None
    cur.execute(
        """INSERT INTO participant (participant_id, cohort, created_at, minted_for_run_id)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (participant_id) DO UPDATE SET
               cohort = EXCLUDED.cohort,
               minted_for_run_id = COALESCE(participant.minted_for_run_id, EXCLUDED.minted_for_run_id)""",
        (pid, cohort, ts(created) or ts(0), run_id))
    if key:
        cur.execute(
            """INSERT INTO participant_identity (participant_id, participant_key)
               VALUES (%s,%s) ON CONFLICT (participant_id) DO UPDATE
               SET participant_key = EXCLUDED.participant_key""",
            (pid, str(key)))


def stub_run(cur, run_id: Optional[str], pid: Optional[str], cohort: str,
             started: Optional[float]) -> None:
    """A run row from what an encounter knows about its run. The /api/runs
    export (load_runs) overwrites these with the real document."""
    if not run_id:
        return
    cur.execute(
        """INSERT INTO run (run_id, participant_id, cohort, created_at, order_scheme)
           VALUES (%s,%s,%s,%s,'unknown')
           ON CONFLICT (run_id) DO UPDATE SET
               created_at = LEAST(run.created_at, EXCLUDED.created_at),
               participant_id = COALESCE(run.participant_id, EXCLUDED.participant_id)""",
        (run_id, pid, cohort, ts(started) or ts(0)))


# The provenance keys an analyst splits the archive on (docs/OPERATIONS.md,
# "What changed on 2026-09-23"). Read from record.json's provenance block, and
# from the realtime_session_started event for records built before the block
# carried them.
PIPELINE_KEYS = ("pipeline_version", "room_pacing_version", "input_rate",
                 "input_transcription_model", "max_output_tokens", "resampler",
                 "input_resampler", "turn_gate", "pacing", "record",
                 "cancelled_output", "agent_transcript_items",
                 # Room memory hygiene (pipeline 2026-10-01a).
                 "room_memory",
                 # The serving image's commit (server/build_info.py); absent
                 # on encounters from images built before BUILD_SHA.
                 "build")


def pipeline_provenance(prov: dict, rt_started: dict) -> Dict[str, Any]:
    """The pipeline stamps and knob values an encounter ran under, record
    first, the session event where the record has no value."""
    out = {}
    for k in PIPELINE_KEYS:
        v = prov.get(k)
        if v is None:
            v = rt_started.get(k)
        if v is not None:
            out[k] = v
    return out


def ensure_pipeline_columns(cur) -> None:
    """Add the pipeline columns to an encounter table created from an older
    docs/db-schema.sql, so a refresh does not need the database rebuilt."""
    for col, typ in (("pipeline_version", "text"),
                     ("room_pacing_version", "text"),
                     ("pipeline_provenance", "jsonb")):
        cur.execute(f"ALTER TABLE encounter ADD COLUMN IF NOT EXISTS {col} {typ}")


def load_encounter(cur, d: Path, s3_prefix: str) -> Optional[str]:
    manifest = read_json(d / "manifest.json")
    record = read_json(d / "record.json") or {}
    events = read_events(d / "events.jsonl")
    if not manifest and not record:
        return None
    m = manifest or {}
    sid = m.get("session_id") or record.get("encounter_id") or d.name
    scenario = m.get("scenario") or record.get("scenario")
    if not scenario:
        return None
    cur.execute("SELECT construct FROM scenario WHERE scenario_id = %s", (scenario,))
    row = cur.fetchone()
    if not row:
        print(f"  skip {sid}: scenario {scenario} not in the bank", file=sys.stderr)
        return None
    construct = row[0]

    cohort = m.get("cohort") or record.get("cohort") or "study"
    pid = m.get("participant_id") or record.get("participant_id")
    run_id = m.get("run_id") or record.get("run_id")
    pkey = m.get("participant_key") or record.get("participant_key")
    slot = m.get("encounter_index", record.get("encounter_index"))
    started = m.get("started_at")
    if started is None and sid.startswith("s_"):
        try:
            started = float(sid.split("_")[1])
        except (IndexError, ValueError):
            started = None
    ended = m.get("ended_at")
    prov = record.get("provenance") or {}
    counts = record.get("counts") or {}
    pch = record.get("participant_channel") or {}
    vup = record.get("video_upload") or {}
    fp = m.get("spec_fingerprint") or record.get("spec_fingerprint") or {}
    rt_started = next((e for e in events if e.get("type") == "realtime_session_started"), {})
    pipeline = pipeline_provenance(prov, rt_started)

    upsert_participant(cur, pid, cohort, pkey, None, run_id)
    stub_run(cur, run_id, pid, cohort, started)
    if run_id is not None and slot is not None and 0 <= int(slot) <= 3:
        cur.execute(
            """INSERT INTO run_slot (run_id, slot, construct, scenario_id, encounter_id)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, slot) DO UPDATE SET
                   scenario_id = EXCLUDED.scenario_id, construct = EXCLUDED.construct,
                   encounter_id = EXCLUDED.encounter_id""",
            (run_id, int(slot), construct, scenario, sid))
    else:
        slot = None

    # Replace the encounter and its children wholesale.
    for table in ("event", "director_route", "trigger_firing", "stage_direction", "turn",
                  "encounter_cast", "media", "transcript_version"):
        cur.execute(f"DELETE FROM {table} WHERE encounter_id = %s", (sid,))
    cur.execute("DELETE FROM encounter WHERE encounter_id = %s", (sid,))
    cur.execute(
        """INSERT INTO encounter (encounter_id, run_id, slot, participant_id, scenario_id, cohort,
               started_at, ended_at, duration_s, status, gateway, realtime_model, text_model,
               director_model, steering_model, pipeline_version, room_pacing_version,
               pipeline_provenance, spec_sha256, spec_trigger_ids,
               participant_turns, agent_turns, stage_directions, script_mismatch_turns,
               unheard_turns, participant_channel_state, participant_channel_losses,
               untranscribed_s, video_upload_state, video_upload_attempts, video_upload_error,
               archive_uri)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (sid, run_id, slot, pid, scenario, cohort, ts(started) or ts(0), ts(ended),
         (round(ended - started, 1) if started and ended else None),
         m.get("status") or "closed",
         prov.get("gateway") or rt_started.get("gateway") or "unknown",
         prov.get("realtime_model") or m.get("model") or rt_started.get("realtime_model") or "unknown",
         prov.get("text_model") or rt_started.get("text_model"),
         prov.get("director_model") or next((r.get("director_model") for r in record.get("steering_log") or []
                                             if r.get("director_model")), None),
         prov.get("steering_model"),
         pipeline.get("pipeline_version"), pipeline.get("room_pacing_version"),
         Jsonb(pipeline) if pipeline else None,
         fp.get("sha256"), fp.get("trigger_ids"),
         counts.get("participant_turns", 0), counts.get("agent_turns", 0),
         counts.get("stage_directions", 0), counts.get("script_mismatch_turns", 0),
         counts.get("unheard_turns", 0),
         pch.get("state"), len(pch.get("losses") or []) if isinstance(pch.get("losses"), list)
         else pch.get("losses"),
         pch.get("untranscribed_s"), vup.get("state"), vup.get("attempts"),
         vup.get("error") or vup.get("client_error"), f"{s3_prefix}{sid}/"))

    for c in record.get("cast") or []:
        cur.execute(
            "INSERT INTO encounter_cast (encounter_id, agent_id, name, voice) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (sid, c.get("id"), c.get("name") or c.get("id"),
             next((r.get("voice") for r in record.get("transcript") or []
                   if r.get("agent_id") == c.get("id") and r.get("voice")), None)))

    # Turns from the record; audio_ms from the matching assistant_turn event.
    audio_ms_by_t = {round(float(e["t"]), 3): e.get("audio_ms")
                     for e in events if e.get("type") == "assistant_turn" and "t" in e}
    turn_ids: List[int] = []
    for seq, r in enumerate(record.get("transcript") or []):
        role = "participant" if r.get("role") in ("user", "participant") else "agent"
        cur.execute(
            """INSERT INTO turn (encounter_id, seq, t, wall, role, agent_id, segment,
                   interaction_id, text, interrupted, transcript_missing, garbled,
                   script_mismatch, participant_channel, audio_ms, latency_s)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING turn_id""",
            (sid, seq, r.get("t", 0), ts(started + float(r.get("t", 0))) if started else None,
             role, r.get("agent_id") if role == "agent" else None, r.get("segment"),
             r.get("interaction"), r.get("text"), bool(r.get("interrupted")),
             bool(r.get("transcript_missing")), bool(r.get("garbled")),
             bool(r.get("script_mismatch")), r.get("participant_channel"),
             audio_ms_by_t.get(round(float(r.get("t", 0)), 3)) if role == "agent" else None,
             r.get("latency_s")))
        turn_ids.append(cur.fetchone()[0])
    # Pair a steering row with the agent turn that carries the same direction.
    transcript = record.get("transcript") or []
    for s in record.get("steering_log") or []:
        reply = next((turn_ids[i] for i, r in enumerate(transcript)
                      if r.get("agent_id") == s.get("agent_id")
                      and r.get("stage_direction") == s.get("stage_direction")
                      and r.get("trigger_id") == s.get("trigger_id")
                      and float(r.get("t", 0)) >= float(s.get("t", 0)) - 0.001), None)
        cur.execute(
            """INSERT INTO stage_direction (encounter_id, turn_index, reply_turn_id, t, agent_id,
                   segment, interaction_id, direction, trigger_id, esci_items, probing, opening,
                   via, acked, director_model, instructions_sha256)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (sid, s.get("turn"), reply, s.get("t", 0), s.get("agent_id") or "?", s.get("segment"),
             s.get("interaction"), s.get("stage_direction"), s.get("trigger_id"),
             list(s.get("esci") or []) or None, bool(s.get("probing")), bool(s.get("opening")),
             s.get("via"), s.get("acked"), s.get("director_model"), s.get("instructions_sha256")))

    for e in events:
        et = e.get("type")
        if et in ("trigger_fired", "trigger_undelivered", "trigger_deferred"):
            cur.execute(
                """INSERT INTO trigger_firing (encounter_id, trigger_id, interaction_id, plan_index,
                       t, agent_id, probing, esci_items, outcome, reason, routed_to)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (sid, e.get("trigger_id") or "?", e.get("interaction"), e.get("index"),
                 e.get("t", 0), e.get("agent_id"), bool(e.get("probing")),
                 list(e.get("esci") or []) or None, et.split("_", 1)[1],
                 e.get("reason"), e.get("routed_to")))
        elif et == "director_route":
            cur.execute(
                """INSERT INTO director_route (encounter_id, t, speakers, addressed, fallback,
                       fallback_reason, fallback_detail)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (sid, e.get("t", 0), list(e.get("speakers") or []), e.get("addressed"),
                 bool(e.get("fallback")), e.get("fallback_reason"),
                 json.dumps(e.get("fallback_detail")) if isinstance(e.get("fallback_detail"), (dict, list))
                 else e.get("fallback_detail")))
    cur.executemany(
        "INSERT INTO event (encounter_id, seq, t, wall, type, agent_id, payload) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        [(sid, i, e.get("t"), ts(e.get("wall")), e.get("type") or "?", e.get("agent_id"), Jsonb(e))
         for i, e in enumerate(events)])

    audio = record.get("audio") or m.get("audio") or {}
    if audio.get("participant"):
        cur.execute(
            "INSERT INTO media (encounter_id, kind, s3_key, sample_rate, channels, format) "
            "VALUES (%s,'participant_audio',%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (sid, f"{s3_prefix}{sid}/{audio['participant']}", audio.get("sample_rate"),
             audio.get("channels"), audio.get("format")))
    for i, a in enumerate(audio.get("agents") or []):
        cur.execute(
            "INSERT INTO media (encounter_id, kind, agent_id, s3_key, sample_rate, channels, format) "
            "VALUES (%s,'agent_audio',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (sid, None, f"{s3_prefix}{sid}/{a}", audio.get("sample_rate"), audio.get("channels"),
             audio.get("format")))
    for v in record.get("video") or []:
        key = v if isinstance(v, str) else (v.get("key") or v.get("name") or v.get("file"))
        if key:
            cur.execute(
                "INSERT INTO media (encounter_id, kind, s3_key, format) VALUES (%s,'webcam_video',%s,'webm') "
                "ON CONFLICT DO NOTHING",
                (sid, key if key.startswith(s3_prefix) else f"{s3_prefix}{sid}/{key}"))
    if (d / "webcam.webm").exists() or any(p.suffix == ".webm" for p in d.iterdir()):
        for p in d.glob("*.webm"):
            cur.execute(
                "INSERT INTO media (encounter_id, kind, s3_key, bytes, format) VALUES (%s,'webcam_video',%s,%s,'webm') "
                "ON CONFLICT DO NOTHING", (sid, f"{s3_prefix}{sid}/{p.name}", p.stat().st_size))

    hq = record.get("participant_transcript_hq")
    if isinstance(hq, dict):
        hq_text, hq_model = hq.get("text"), hq.get("model")
    else:
        hq_text, hq_model = hq, None
    if hq_text:
        cur.execute(
            "INSERT INTO transcript_version (encounter_id, source, scope, model, text) "
            "VALUES (%s,'retranscribed','participant',%s,%s) ON CONFLICT DO NOTHING",
            (sid, hq_model, str(hq_text)))
    live = "\n".join(f"{(r.get('agent_id') or 'participant')}: {r.get('text') or ''}"
                     for r in transcript)
    if live.strip():
        cur.execute(
            "INSERT INTO transcript_version (encounter_id, source, scope, text) "
            "VALUES (%s,'live','dialogue',%s) ON CONFLICT DO NOTHING", (sid, live))
    rec_md = d / "recovered_transcript.md"
    if rec_md.exists():
        cur.execute(
            "INSERT INTO transcript_version (encounter_id, source, scope, model, text) "
            "VALUES (%s,'recovered_from_video','dialogue','gemini-2.5-pro',%s) ON CONFLICT DO NOTHING",
            (sid, rec_md.read_text(encoding="utf-8")))
    return sid


# ---------------------------------------------------------------------------
# Encounters rebuilt from webcam video (tools/recover_from_video.py)
# ---------------------------------------------------------------------------

def load_recovered(cur, index_csv: Path, archive: Path, s3_prefix: str) -> int:
    """Encounters whose records were lost before EFS (2026-09-17) and were
    rebuilt from the webcam video: an encounter row with status 'recovered',
    the diarised transcript, and the video pointer. Skips any session that
    already has a real record."""
    import csv
    n = 0
    for r in csv.DictReader(index_csv.open(encoding="utf-8")):
        sid = r.get("session")
        if not sid:
            continue
        cur.execute("SELECT status FROM encounter WHERE encounter_id = %s", (sid,))
        row = cur.fetchone()
        if row and row[0] != "recovered":
            continue
        scenario = r.get("scenario")
        cur.execute("SELECT 1 FROM scenario WHERE scenario_id = %s", (scenario,))
        if not cur.fetchone():
            scenario = None
        pid = r.get("participant") or None
        try:
            started = float(sid.split("_")[1])
        except (IndexError, ValueError):
            started = 0.0
        secs = float(r.get("audio_seconds") or 0) or None
        upsert_participant(cur, pid, "study", None, None, None)
        for table in ("media", "transcript_version"):
            cur.execute(f"DELETE FROM {table} WHERE encounter_id = %s", (sid,))
        cur.execute("DELETE FROM encounter WHERE encounter_id = %s", (sid,))
        cur.execute(
            """INSERT INTO encounter (encounter_id, participant_id, scenario_id, cohort, started_at,
                   ended_at, duration_s, status, gateway, realtime_model, archive_uri)
               VALUES (%s,%s,%s,'study',%s,%s,%s,'recovered','unknown','unknown',%s)""",
            (sid, pid, scenario, ts(started), ts(started + secs) if secs else None, secs,
             f"{s3_prefix}{sid}/"))
        cur.execute(
            "INSERT INTO media (encounter_id, kind, s3_key, duration_s, format) "
            "VALUES (%s,'webcam_video',%s,%s,'webm')",
            (sid, f"{s3_prefix}{sid}/webcam.webm", secs))
        md = archive / sid / "recovered_transcript.md"
        if md.exists():
            cur.execute(
                "INSERT INTO transcript_version (encounter_id, source, scope, model, text) "
                "VALUES (%s,'recovered_from_video','dialogue','nto.gemini-2.5-pro',%s)",
                (sid, md.read_text(encoding="utf-8")))
        n += 1
    return n


# ---------------------------------------------------------------------------
# Media rows from the bucket listing
# ---------------------------------------------------------------------------

def load_s3_media(cur, s3_prefix: str) -> int:
    """One media row per audio/video object in the archive. record.json is
    written when the encounter closes, before the browser has finished
    uploading webcam.webm, so its `video` list is usually empty; the bucket is
    the truth about what was captured."""
    import boto3
    assert s3_prefix.startswith("s3://")
    bucket, _, prefix = s3_prefix[5:].partition("/")
    cur.execute("SELECT encounter_id FROM encounter")
    known = {r[0] for r in cur.fetchall()}
    kinds = {".webm": "webcam_video", ".mp4": "webcam_video", ".wav": None, ".mp3": "participant_audio"}
    n = 0
    paginator = boto3.client("s3").get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            rel = o["Key"][len(prefix):]
            sid, _, name = rel.partition("/")
            if sid not in known or not name:
                continue
            ext = Path(name).suffix.lower()
            if ext not in kinds:
                continue
            kind = kinds[ext]
            agent = None
            if kind is None:  # .wav: user_audio.wav | assistant_audio[_<agent>].wav
                if name.startswith("user_audio"):
                    kind = "participant_audio"
                else:
                    kind = "agent_audio"
                    stem = Path(name).stem
                    agent = stem.split("assistant_audio_", 1)[1] if "assistant_audio_" in stem else None
            cur.execute(
                """INSERT INTO media (encounter_id, kind, agent_id, s3_key, bytes, format, uploaded_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (s3_key) DO UPDATE SET bytes = EXCLUDED.bytes, agent_id = EXCLUDED.agent_id,
                       uploaded_at = EXCLUDED.uploaded_at""",
                (sid, kind, agent, f"s3://{bucket}/{o['Key']}", o["Size"], ext.lstrip("."),
                 o["LastModified"]))
            n += 1
    return n


# ---------------------------------------------------------------------------
# Runs export (optional)
# ---------------------------------------------------------------------------

def load_runs(cur, runs: Iterable[dict]) -> int:
    n = 0
    for r in runs:
        rid = r.get("run_id")
        if not rid:
            continue
        order = r.get("order") or {}
        pool = r.get("construct_pool") or {}
        cur.execute(
            """INSERT INTO run (run_id, participant_id, cohort, created_at, arm, construct_pool,
                   order_scheme, order_row, form_exclusions, variants, survey1_response_id,
                   completion_code, other_arm_runs)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                   participant_id = COALESCE(EXCLUDED.participant_id, run.participant_id),
                   cohort = EXCLUDED.cohort, created_at = EXCLUDED.created_at, arm = EXCLUDED.arm,
                   construct_pool = EXCLUDED.construct_pool, order_scheme = EXCLUDED.order_scheme,
                   order_row = EXCLUDED.order_row, form_exclusions = EXCLUDED.form_exclusions,
                   variants = EXCLUDED.variants, survey1_response_id = EXCLUDED.survey1_response_id,
                   completion_code = EXCLUDED.completion_code, other_arm_runs = EXCLUDED.other_arm_runs""",
            (rid, r.get("participant_id"), r.get("cohort") or "study", ts(r.get("created_at")) or ts(0),
             pool.get("arm") or "full", Jsonb(pool) if pool else None,
             order.get("scheme") or ("williams_4x4" if order.get("row") is not None else "unknown"),
             order.get("row"), Jsonb(r.get("form_exclusions") or []),
             Jsonb(r.get("variants")) if r.get("variants") is not None else None,
             r.get("qualtrics_id"), r.get("completion_code"),
             Jsonb(r.get("other_arm_runs")) if r.get("other_arm_runs") else None))
        if r.get("participant_id"):
            upsert_participant(cur, r["participant_id"], r.get("cohort") or "study",
                               r.get("participant_key") or r.get("raw_participant_key"),
                               r.get("created_at"), rid)
            cur.execute(
                "UPDATE participant_identity SET participant_key_status = %s, raw_participant_key = %s "
                "WHERE participant_id = %s",
                (r.get("participant_key_status"), r.get("raw_participant_key"), r["participant_id"]))
        for slot, sc in enumerate(r.get("scenarios") or []):
            scid = sc if isinstance(sc, str) else (sc.get("scenario") or sc.get("id"))
            if not scid or slot > 3:
                continue
            cur.execute("SELECT construct FROM scenario WHERE scenario_id = %s", (scid,))
            row = cur.fetchone()
            if not row:
                continue
            cur.execute(
                """INSERT INTO run_slot (run_id, slot, construct, scenario_id)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (run_id, slot) DO UPDATE
                   SET scenario_id = EXCLUDED.scenario_id, construct = EXCLUDED.construct""",
                (rid, slot, row[0], scid))
        w = r.get("withdrawn")
        if isinstance(w, dict) and w.get("at"):
            cur.execute(
                """INSERT INTO withdrawal (run_id, withdrawn_at, reason, encounter_id, slot_index,
                       completed_count, withdrawn_on_run_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING""",
                (rid, ts(w["at"]), w.get("reason") or "participant_withdrew", w.get("session_id"),
                 w.get("index"), w.get("completed"), w.get("withdrawn_on_run") or rid))
            if r.get("participant_id"):
                cur.execute("UPDATE participant SET withdrawn_at = COALESCE(withdrawn_at, %s) "
                            "WHERE participant_id = %s", (ts(w["at"]), r["participant_id"]))
        n += 1
    # completed_at: when the last slot's encounter closed
    cur.execute("""
        UPDATE run r SET completed_at = x.ended
        FROM (SELECT e.run_id, max(e.ended_at) AS ended, count(*) AS n
              FROM encounter e WHERE e.status = 'closed' GROUP BY e.run_id) x
        WHERE x.run_id = r.run_id AND x.n >= 4""")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--archive", type=Path, required=True, help="local copy of encounters/")
    ap.add_argument("--scenarios", type=Path, default=Path("scenarios/v3"))
    ap.add_argument("--runs", type=Path, help="/api/runs export (JSON list)")
    ap.add_argument("--s3-prefix", default="s3://relational-fluency-study-data/encounters/")
    ap.add_argument("--s3-media", action="store_true",
                    help="list the S3 bucket and add media rows (audio/video sizes) for every "
                         "encounter; the record is written before the webcam upload lands, so "
                         "this is the only way has_video is right")
    ap.add_argument("--recovered-index", type=Path,
                    help="tools/recover_from_video.py index CSV; default: _recovered_index_*.csv in --archive")
    args = ap.parse_args()

    if psycopg is None:
        sys.exit("tools/load_analysis_db.py needs the Postgres driver: "
                 "pip install 'psycopg[binary]'")
    with psycopg.connect(args.dsn) as conn:
        conn.execute("SET search_path TO rf, public")
        with conn.cursor() as cur:
            ensure_pipeline_columns(cur)
            n_scen = load_scenarios(cur, args.scenarios)
            print(f"scenarios: {n_scen}")
            loaded = skipped = 0
            for d in sorted(p for p in args.archive.iterdir() if p.is_dir()):
                if load_encounter(cur, d, args.s3_prefix):
                    loaded += 1
                else:
                    skipped += 1
            print(f"encounters: {loaded} loaded, {skipped} without a record")
            idx = args.recovered_index or next(iter(sorted(args.archive.glob("_recovered_index_*.csv"))), None)
            if idx:
                print(f"recovered from video: {load_recovered(cur, idx, args.archive, args.s3_prefix)} "
                      f"(index {idx.name})")
            if args.s3_media:
                print(f"media from S3: {load_s3_media(cur, args.s3_prefix)} objects")
            if args.runs:
                runs = json.loads(args.runs.read_text(encoding="utf-8"))
                if isinstance(runs, dict) and "detail" in runs and len(runs) == 1:
                    sys.exit(f"{args.runs} is an API error, not an export: {runs['detail']!r}. "
                             "Re-run the curl with the current SESSION_KEY.")
                if isinstance(runs, dict):
                    runs = runs.get("runs") or runs.get("items") or []
                print(f"runs: {load_runs(cur, runs)} from export")
            else:
                cur.execute("""
                    UPDATE run r SET completed_at = x.ended
                    FROM (SELECT e.run_id, max(e.ended_at) AS ended, count(*) AS n
                          FROM encounter e WHERE e.status = 'closed' GROUP BY e.run_id) x
                    WHERE x.run_id = r.run_id AND x.n >= 4""")
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
