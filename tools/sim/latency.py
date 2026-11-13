"""Offline coarse latency intervals from session events or saved sim reports."""
from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

from server.voice.realtime import family_of
from tools.encounter_health import read_events
from tools.sim.analyze import percentile
from tools.sim.check import load_baseline as read_json, write_json

# These intervals are disjoint when their recorded endpoints are ordered.
INTERVALS = {
    "speech_end_to_commit": ("vad_speech_end", "commit_sent"),
    "commit_to_grant": ("commit_sent", "grant_sent"),
    "gateway_to_client": ("first_audio_from_gateway", "first_audio_to_client"),
    "client_to_played": ("first_audio_to_client", "first_audio_played"),
}
TOTAL = ("vad_speech_end", "first_audio_played")
DIAGNOSTICS = {
    "commit_to_transcript_arrival": ("commit_sent", "transcript_arrived"),
    "speech_end_to_director_decision": ("vad_speech_end", "director_decided"),
}
CAVEATS = [
    "first_audio_played is the server receipt time of the page ACK minus lag_s; "
    "it includes return network time and does not establish when sound was heard.",
    "The page queues audio, adds a 20 ms cushion, and uses a media element. "
    "output_latency_s is recorded but is not applied to these timestamps.",
    "vad_speech_end follows the VAD silence run; the last voiced frame is absent.",
    "cascade.llm_first_token_ms is overwritten until the first sentence is released; "
    "it is not time to first token. Final-flush replies may have no sentence-release stamp.",
    "cascade.llm_total_ms includes TTS waits and is not pure LLM generation time.",
    "cascade.stt_ms can be stale between replies; it is never charged to the total.",
    "Room member cascade timings do not describe the scribe's STT.",
    "STT, generation and TTS connection overlap. Cascade durations are diagnostics, "
    "not additive stages or exposed waits; no reply start timestamp anchors them.",
    "transcript_arrived is not transcript accepted; director_decided is an endpoint, "
    "not a director duration. These diagnostic intervals can overlap the breakdown.",
    "Saved sim summaries cannot recover stages, means, or first-versus-later splits. "
    "The sim does not model the browser audio path.",
    "Mode uses session metadata. Mixed room and 1:1 interactions are not separated; "
    "the current v3 scenarios use one mode per session.",
]
MISSING_BOUNDARIES = [
    "reply request sent", "first content token", "first sentence ready",
    "first TTS send", "TTS first audio", "transcript accepted",
    "floor lock acquired", "director start", "director end", "browser receipt",
]
BOUNDARIES = tuple(dict.fromkeys((
    *TOTAL, *(field for pair in (*INTERVALS.values(), *DIAGNOSTICS.values()) for field in pair),
)))
GROUP_FIELDS = ("scenario", "mode", "encounter_reply", "participant_reply", "family", "model")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def context(record, events, scenario=None):
    start = next((e for e in events if e.get("type") == "session_start"), {})
    rt = next((e for e in events if e.get("type") == "realtime_session_started"), {})
    prov = record.get("provenance") or {}
    model = rt.get("model") or rt.get("realtime_model") or prov.get("realtime_model") \
        or start.get("realtime_model") or "unknown"
    mode = start.get("mode") or record.get("mode")
    if any(e.get("type") == "group_room_opened" for e in events):
        mode = "group"
    mode = {"single": "1:1", "one_to_one": "1:1", "group": "room",
            "room": "room", "1:1": "1:1"}.get(mode, "unknown")
    return {"scenario": start.get("scenario") or record.get("scenario") or scenario or "unknown",
            "mode": mode, "family": family_of(model) or "unknown", "model": model}


def turn_intervals(event, mode):
    """Keep ordered, in-range spans. Leave every other second unexplained."""
    a, b = (event.get(k) for k in TOTAL)
    out = {"intervals_s": {name: None for name in (*INTERVALS, "unexplained")},
           "diagnostics_s": {name: None for name in DIAGNOSTICS},
           "unexplained_spans": [], "issues": []}
    if not (number(a) and number(b) and 0 <= a <= b):
        out["total_s"] = None
        out["issues"].append("missing_or_invalid_total")
        return out
    out["total_s"] = round(b - a, 6)
    covered = []
    for name, (start, end) in INTERVALS.items():
        if name == "commit_to_grant" and mode != "room":
            out["intervals_s"][name] = None
            continue
        x, y = event.get(start), event.get(end)
        value = None
        if number(x) and number(y):
            if a <= x <= y <= b and not any(x < v and y > u for u, v, *_ in covered):
                value = round(y - x, 6)
                covered.append((x, y, start, end))
            else:
                out["issues"].append(f"invalid_or_overlapping:{name}")
        out["intervals_s"][name] = value
    # Preserve the actual endpoints of each uncovered span, including missing stages.
    gaps, cursor, field = [], a, TOTAL[0]
    for x, y, start, end in sorted(covered):
        if x > cursor:
            gaps.append({"start_field": field, "end_field": start,
                         "duration_s": round(x - cursor, 6)})
        if y >= cursor:
            cursor, field = y, end
    if b > cursor:
        gaps.append({"start_field": field, "end_field": TOTAL[1],
                     "duration_s": round(b - cursor, 6)})
    out["unexplained_spans"] = gaps
    out["intervals_s"]["unexplained"] = round(sum(g["duration_s"] for g in gaps), 6)
    for name, (start, end) in DIAGNOSTICS.items():
        x, y = event.get(start), event.get(end)
        out["diagnostics_s"][name] = round(y - x, 6) if number(x) and number(y) and 0 <= x <= y else None
    return out


def session_rows(record, events, source, scenario=None):
    meta = context(record, events, scenario)
    timings = [e for e in events if e.get("type") == "turn_timing"]
    # turn_timing is written at play_end; file order is not reply order.
    numbered = [e["turn"] for e in timings if number(e.get("turn"))]
    first_turn = min(numbered) if numbered else None
    by_speech = defaultdict(list)
    for e in timings:
        if number(e.get("vad_speech_end")):
            by_speech[e["vad_speech_end"]].append(e)
    rows = []
    for speech, replies in sorted(by_speech.items()):
        # Old records have no reply_index. Use page turn order, never play_end order.
        replies.sort(key=lambda e: (e.get("turn") if number(e.get("turn")) else math.inf,
                                    e.get("first_audio_played") if number(e.get("first_audio_played")) else math.inf))
        for i, e in enumerate(replies):
            index = e.get("reply_index")
            participant_reply = ("first" if index == 0 else "later") if number(index) else ("first" if i == 0 else "later")
            row = {**meta, "source": source, "turn": e.get("turn"),
                   "vad_speech_end": speech, "participant_reply": participant_reply,
                   "boundaries_s": {k: e.get(k) if number(e.get(k)) else None for k in BOUNDARIES},
                   "encounter_reply": ("first" if e.get("turn") == first_turn else "later") if first_turn is not None else "unknown",
                   "reply_index_inferred": not number(index),
                   "cascade_diagnostics_ms": {k: v for k, v in (e.get("cascade") or {}).items() if number(v)},
                   **turn_intervals(e, meta["mode"])}
            rows.append(row)
    mark_conversation_replies(rows, events)
    return rows


def mark_conversation_replies(rows, events):
    """Separate the first 1:1 reply after each start or handoff boundary."""
    boundaries, awaiting = [], None
    for event in events:
        kind, reason, at = event.get("type"), event.get("reason"), event.get("t")
        if reason not in ("start", "handoff") or not number(at):
            continue
        if kind == "awaiting_participant":
            boundaries.append((at, reason, "vad_speech_end"))
            awaiting = reason
        elif kind == "participant_opened":
            # Acceptance often follows VAD speech end. Prefer the earlier
            # awaiting boundary, rather than requiring speech after acceptance.
            if awaiting != reason:
                waited = event.get("waited_s")
                if number(waited) and 0 <= waited <= at:
                    boundaries.append((at - waited, reason, "vad_speech_end"))
                else:
                    boundaries.append((at, reason, "first_audio_to_client"))
            awaiting = None
    boundaries.sort()
    for i, (at, reason, field) in enumerate(boundaries):
        until = boundaries[i + 1][0] if i + 1 < len(boundaries) else math.inf
        eligible = [row for row in rows if row["mode"] == "1:1" and row["participant_reply"] == "first"
                    and number(row["boundaries_s"].get(field))
                    and at <= row["boundaries_s"][field] < until]
        if eligible:
            first = min(eligible, key=lambda row: row["turn"] if number(row["turn"]) else row["vad_speech_end"])
            first["encounter_reply"] = "first_after_handoff" if reason == "handoff" else "first"


def stats(rows, field, diagnostic=False):
    values, shares = [], []
    for row in rows:
        value = row.get("total_s") if field == "total" else row.get("diagnostics_s" if diagnostic else "intervals_s", {}).get(field)
        if value is None:
            continue
        values.append(value)
        if row["total_s"] > 0:
            shares.append(value / row["total_s"])
    return {"n": len(values), "p50_s": percentile(values, 50), "p90_s": percentile(values, 90),
            "mean_share": round(sum(shares) / len(shares), 6) if shares and not diagnostic else None,
            "share_n": len(shares) if not diagnostic else 0}


def summarize_rows(rows):
    buckets = defaultdict(list)
    for row in rows:
        buckets[tuple(row[k] for k in GROUP_FIELDS)].append(row)
    groups = []
    for key, members in sorted(buckets.items()):
        groups.append({**dict(zip(GROUP_FIELDS, key)), "source_kind": "turn_timing",
                       "reply_rows": len(members),
                       "missing_played_ack": sum(r["boundaries_s"]["first_audio_played"] is None for r in members),
                       "stages": {name: stats(members, name) for name in ("total", *INTERVALS, "unexplained")},
                       "diagnostics": {name: stats(members, name, True) for name in DIAGNOSTICS}})
    return groups


def summary_groups(report, source, issues):
    """Keep published quantiles intact. Never invent samples from quantiles."""
    groups = []
    for scenario, result in sorted(report.get("scenarios", {}).items()):
        if not isinstance(result, dict):
            issues.append(f"{source}: {scenario}: invalid scenario result")
            result = {"error": "invalid scenario result"}
        metrics = result.get("metrics") or {}
        total = metrics.get("speech_end_to_first_played_s")
        if result.get("error"):
            issues.append(f"{source}: {scenario}: {result['error']}")
        elif not total:
            issues.append(f"{source}: {scenario}: no saved latency samples")
        meta = context({"scenario": scenario, "provenance": metrics.get("provenance") or report.get("health", {})}, [])
        # Reports omit mode. Only the four fixed sim scenarios have a known mode.
        meta["mode"] = {"S1A": "1:1", "S2A": "1:1", "S3A": "room", "S4A": "room"}.get(scenario, "unknown")
        unavailable = {"n": 0, "p50_s": None, "p90_s": None, "mean_share": None, "share_n": 0}
        stages = {name: dict(unavailable) for name in ("total", *INTERVALS, "unexplained")}
        if total:
            stages["total"].update(n=total.get("n", 0), p50_s=total.get("p50"), p90_s=total.get("p90"))
        completed = metrics.get("completed")
        status = "error" if result.get("error") else {True: "completed", False: "incomplete"}.get(completed, "unknown")
        groups.append({**meta, "source": source, "source_kind": "sim_summary",
                       "encounter_reply": "unknown", "participant_reply": "first",
                       "mode_basis": "fixed sim scenario", "reply_rows": None, "missing_played_ack": None,
                       "completed": completed, "status": status, "error": result.get("error"), "stages": stages,
                       "diagnostics": {}, "generated_at": report.get("generated_at"),
                       "build": report.get("build"), "session_id": metrics.get("session_id") or result.get("session_id")})
    return groups


def analyze_paths(paths):
    sessions, reports, timelines = {}, {}, {}
    rows, sources, issues, unavailable = [], [], [], []
    documents = {}

    def read_object(path):
        if path not in documents:
            try:
                doc = read_json(path)
                if not isinstance(doc, dict):
                    raise ValueError("expected a JSON object")
                documents[path] = doc
            except (OSError, ValueError) as exc:
                issues.append(f"{path}: {exc}")
                documents[path] = {}
        return documents[path]

    for path in paths:
        path = Path(path).resolve()
        if not path.exists():
            raise ValueError(f"input does not exist: {path}")
        candidates = sorted(path.rglob("*.json")) + sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
        for candidate in candidates:
            if path.is_dir() and candidate.suffix == ".jsonl" and not (
                    candidate.name == "events.jsonl" or candidate.name.endswith(".events.jsonl")):
                continue
            if candidate.name == "record.json" or candidate.suffix == ".jsonl":
                sibling = candidate.parent / "events.jsonl"
                selected = sibling if candidate.name == "record.json" and sibling.exists() else candidate
                sessions[selected] = selected.parent
            elif candidate.name.endswith(".timeline.json"):
                tl = read_object(candidate)
                if tl.get("session_id"):
                    timelines[tl["session_id"]] = tl.get("scenario")
            elif candidate.suffix == ".json" and candidate.name not in ("manifest.json", "audio_timeline.json"):
                doc = read_object(candidate)
                if isinstance(doc.get("scenarios"), dict) and "generated_at" in doc:
                    reports[candidate] = doc
    session_ids = set()
    for selected, directory in sorted(sessions.items()):
        event_path = selected
        record_path = directory / "record.json"
        record = read_object(record_path) if record_path.exists() else {}
        source = str(event_path if event_path.suffix == ".jsonl" else record_path)
        try:
            events, problems = read_events(event_path) if event_path.suffix == ".jsonl" else ([], [])
        except (OSError, ValueError) as exc:
            events, problems = [], [str(exc)]
        issues.extend(f"{source}: {p}" for p in problems)
        valid_events = [e for e in events if isinstance(e, dict)]
        if len(valid_events) != len(events):
            issues.append(f"{source}: non-object events ignored")
        current = session_rows(record, valid_events, source, timelines.get(directory.name))
        rows.extend(current)
        sources.append({"path": source, "reply_rows": len(current)})
        if any(e.get("type") == "turn_timing" for e in valid_events):
            session_ids.add(record.get("encounter_id") or directory.name)
        else:
            issues.append(f"{source}: no turn_timing rows; record.json does not retain stage timings")
            unavailable.append({**context(record, valid_events, timelines.get(directory.name)),
                "source": source, "source_kind": "no_turn_timing", "encounter_reply": "unknown",
                "participant_reply": "unknown", "reply_rows": 0, "missing_played_ack": None,
                "stages": {name: stats([], name) for name in ("total", *INTERVALS, "unexplained")},
                "diagnostics": {}})
    groups = summarize_rows(rows) + unavailable
    for path, report in sorted(reports.items()):
        # A run directory can contain both the summary and its underlying events.
        groups.extend(g for g in summary_groups(report, str(path), issues) if g.get("session_id") not in session_ids)
    return {"schema_version": 2, "units": "seconds (cascade diagnostics remain milliseconds)",
            "group_fields": list(GROUP_FIELDS),
            "definitions": {"total": {"start_field": TOTAL[0], "end_field": TOTAL[1]},
                            **{k: {"start_field": v[0], "end_field": v[1]} for k, v in INTERVALS.items()},
                            "unexplained": {"formula": "total - sum(available disjoint intervals)",
                                            "endpoints": "rows[].unexplained_spans[].start_field/end_field"},
                            "diagnostics": {k: {"start_field": v[0], "end_field": v[1], "additive": False} for k, v in DIAGNOSTICS.items()}},
            "sampling": {"participant_reply": "first = reply_index 0; later = follow-up to the same speech end; legacy rows use page turn order",
                         "encounter_reply": "first = first 1:1 reply after the start boundary; first_after_handoff = first 1:1 reply after each handoff; later = other replies. Awaiting-participant boundaries take precedence over acceptance timestamps. Without boundaries, the lowest page turn is first and handoffs cannot be identified.",
                         "sim_summary_difference": "sim summaries use the earliest first_audio_played per vad_speech_end regardless of reply_index. If reply 0 has no played ACK and reply 1 plays, the sim counts that wait as first response; raw rows keep reply 1 in participant_reply later.",
                         "total_n": "reply rows with finite, nonnegative speech-end-to-ACK intervals; later participant replies are not first-response latency",
                         "percentiles": "linear interpolation, tools.sim.analyze.percentile; seconds rounded to 0.001",
                         "mean_share": "mean of each available interval / its own positive total; share_n is its denominator; missing is not zero",
                         "small_n": "all groups and per-stage sample counts are retained"},
            "sources": sources, "issues": issues, "caveats": CAVEATS,
            "missing_boundaries": MISSING_BOUNDARIES, "rows": rows, "groups": groups,
            "counts": dict(Counter("timed" if r["total_s"] is not None else "missing_or_invalid_total" for r in rows))}


def markdown(report):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    def seconds(value):
        return "unavailable" if value is None else f"{value:.3f}"

    lines = ["| Scenario | Mode | Encounter reply | Participant reply | Family / model | Status | Reply rows | No played ACK | n | Total p50 / p90 (s) | Unexplained n; p50 / p90 (s); mean share |",
             "|---|---|---|---|---|---|---:|---:|---:|---|---|"]
    for group in report["groups"]:
        total, residual = (group["stages"][k] for k in ("total", "unexplained"))
        share = "unavailable" if residual["mean_share"] is None else f'{residual["mean_share"]:.1%}'
        lines.append("| " + " | ".join(map(cell, [group["scenario"], group["mode"], group["encounter_reply"],
            group["participant_reply"], f'{group["family"]} / {group["model"]}',
            group.get("status", group["source_kind"]),
            group["reply_rows"] if group["reply_rows"] is not None else "unavailable",
            group["missing_played_ack"] if group["missing_played_ack"] is not None else "unavailable", total["n"],
            f'{seconds(total["p50_s"])} / {seconds(total["p90_s"])}',
            f'{residual["n"]}; {seconds(residual["p50_s"])} / {seconds(residual["p90_s"])}; {share}'])) + " |")
    lines.extend(["", "| Interval | Start field | End field |", "|---|---|---|"])
    for name, fields in {"total": TOTAL, **INTERVALS, **DIAGNOSTICS}.items():
        lines.append(f"| {name} | {fields[0]} | {fields[1]} |")
    measured = [g for g in report["groups"] if g["source_kind"] == "turn_timing"]
    if measured:
        lines.extend(["", "| Scenario / mode / encounter reply / participant reply / model | Interval | n | p50 / p90 (s) | Mean share | share_n |",
                      "|---|---|---:|---|---|---:|"])
        for group in measured:
            label = " / ".join(group[k] for k in ("scenario", "mode", "encounter_reply", "participant_reply", "model"))
            for name in INTERVALS:
                stage = group["stages"][name]
                share = "unavailable" if stage["mean_share"] is None else f'{stage["mean_share"]:.1%}'
                lines.append(f'| {cell(label)} | {name} | {stage["n"]} | '
                             f'{seconds(stage["p50_s"])} / {seconds(stage["p90_s"])} | {share} | {stage["share_n"]} |')
    lines.extend(["", "Unexplained = total minus the available disjoint intervals. JSON rows retain each uncovered span's exact endpoint fields.",
                  "Participant reply uses reply_index; later replies include room follow-ups and probes. Sim summaries use the earliest played reply even when reply 0 has no ACK. Encounter reply separates the first reply at start, the first after each 1:1 handoff, and later replies.",
                  "Each stage uses its own available replies. Mean shares with different share_n values need not sum to 100%. Missing intervals are unavailable. Small groups remain visible.", ""])
    lines.extend(f"- {item}" for item in report["caveats"])
    if report["issues"]:
        lines.extend([""] + [f"- {item}" for item in report["issues"]])
    lines.extend(["", "Boundaries not recorded: " + "; ".join(report["missing_boundaries"]) + "."])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = analyze_paths(args.inputs)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    write_json(args.json_out, report)
    table = markdown(report)
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(table, encoding="utf-8", newline="")
    else:
        sys.stdout.write(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
