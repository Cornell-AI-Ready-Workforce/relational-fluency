"""Is this encounter usable as data?

Run this on the first encounter recorded against a real gateway key, before
anyone is paid to sit through a second one. It answers one question the
consoles cannot: did the *manipulation* actually happen?

An encounter can look perfect and be worthless. If DIRECTOR_MODEL or
STEERING_MODEL names a model the gateway does not serve, boot still succeeds --
the preflight asks for /v1/models, checks for a 200 and throws the body away --
and every director call then 404s into the cast[0] fallback while every steering
review 404s into auto_steer_error. The encounter completes. Audio, video and
transcript are all fine, trigger coverage is full, and verify_record passes it.
The only thing missing is the steering, which is the independent variable. A
whole wave collected this way is indistinguishable from a good one until the
analysis shows no effect, and by then nobody can say whether that is the finding
or the bug.

So the checks below are deliberately not "did anything error". They are "did the
parts that make this an experiment actually run", which is a different and
harder question, and the answer lives in the event trail rather than in any
status field.

Usage:
    python tools/encounter_health.py data/sessions/<encounter-id>
    python tools/encounter_health.py --all data/sessions

Exit code is 0 only when every encounter passes. Anything else -- a missing
file, an unreadable line, a dead director -- exits non-zero and says why.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

# Any one of these voids the encounter as data. director_error and
# auto_steer_error mean a turn ran without the steering that is the study's
# independent variable; scribe_pump_ended means the group room lost its only
# participant-transcription channel, so the record shows a participant who
# simply stopped talking.
VOIDING_TYPES = (
    "director_error",
    "auto_steer_error",
    "scribe_pump_ended",
    # Added after live testing, because without it this tool returned PASS on
    # the exact failure it was written to catch.
    #
    # On nto.gemini-live-2.5-flash a mid-session session.update is inert: the
    # gateway acknowledges nothing and reports nothing, so every stage direction
    # after the opening brief is written to the record and never reaches the
    # actor. The encounter then looks perfect from here -- audio, transcript,
    # turns, planted beats, all present -- and contains no manipulation at all.
    # The runner emits steer_unacked when it asks and is not answered, and that
    # is the only signal anywhere that the independent variable went missing.
    "steer_unacked",
)

# Degraded but still scoreable, so these are reported and do not fail. A single
# undelivered trigger costs one planted beat out of the interaction's several;
# the encounter is worth less and is still worth rating, and calling it unusable
# would throw away data a rater can legitimately score.
#
# Where the line falls between these two lists is a judgement made without ever
# having seen a real encounter fail. Revisit it after the pilot: if the first
# wave shows voice_error arriving in benign clusters, or a single
# trigger_undelivered reliably accompanying something worse, move it.
DEGRADING_TYPES = (
    "voice_error",
    "trigger_undelivered",
)

# Absence of these is the silent failure. A dead director produces no
# director_error the operator will ever see -- it produces a *fallback*, which
# is a normal-looking encounter with no stage directions in it. So the check
# has to assert presence, not merely the absence of complaint.
REQUIRED_TYPES = (
    "session_start",
    "user_turn",
    "assistant_turn",
    "stage_direction",
    "steering_pair",
)


def read_events(path: Path) -> Tuple[List[dict], List[str]]:
    """Return (events, problems). Never raises on a bad line."""
    problems: List[str] = []
    events: List[dict] = []
    # encoding is named explicitly because a Windows console defaults to cp1252
    # and these files are written with ensure_ascii=False, so a transcript
    # containing a curly quote would otherwise fail to read on exactly the
    # machine most likely to be running the pilot.
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError as exc:
                # A truncated final line is the normal shape of a process that
                # died mid-write, and it is worth reporting rather than
                # skipping: it means the encounter did not close cleanly.
                problems.append(f"line {n} is not valid JSON ({exc})")
    return events, problems


def check(session_dir: Path) -> Tuple[bool, List[str], List[str], Counter]:
    """Return (usable, findings, notes, counts)."""
    events_path = session_dir / "events.jsonl"
    if not events_path.exists():
        # Loud, not zero. A check that cannot run must never report the same
        # thing as a check that ran and found nothing wrong.
        return False, [f"no events.jsonl in {session_dir}"], [], Counter()

    events, problems = read_events(events_path)
    counts = Counter(e.get("type", "<untyped>") for e in events)
    findings = list(problems)
    notes: List[str] = []

    for t in VOIDING_TYPES:
        if counts.get(t):
            findings.append(f"{counts[t]} x {t}")

    for t in DEGRADING_TYPES:
        if counts.get(t):
            notes.append(f"{counts[t]} x {t}")

    for t in REQUIRED_TYPES:
        if not counts.get(t):
            findings.append(f"no {t} events at all")

    # Directions the actor was demonstrably given, not directions we sent.
    #
    # The runner stamps `acked` on a stage direction: True when the gateway
    # answered the session.update that carried it, False when it did not. An
    # encounter full of directions with acked False is an encounter with no
    # manipulation in it, and counting sends instead of acks is precisely how
    # that reads as healthy. Directions written before the field existed carry
    # no `acked` at all, and those are counted as delivered rather than
    # retroactively condemning archived encounters on a field they predate.
    recovery = audio_recovery_note(events)
    if recovery:
        notes.append(recovery)
    for line in pipeline_notes(events):
        notes.append(line)

    directions = [e for e in events if e.get("type") == "stage_direction"]
    if directions:
        answered = [d for d in directions if d.get("acked") is not False]
        if not answered:
            findings.append(
                f"none of the {len(directions)} stage directions were "
                "acknowledged by the model, so the encounter ran unsteered")
        elif len(answered) < len(directions):
            notes.append(
                f"{len(directions) - len(answered)} of {len(directions)} stage "
                "directions went unacknowledged")

    return not findings, findings, notes, counts


def audio_recovery(events: List[dict]) -> dict:
    """How often a reply's voice was lost upstream and what the runner did.

    On nto.gemini-live-2.5-flash through this gateway a reply's audio can stop
    short of its own transcript, or never arrive at all. The runner now asks the
    gateway once more for such a reply (server/voice/realtime.py,
    retry_response) and writes down every decision:

      audio_retry             the gateway was asked again for this turn
      audio_retry_suppressed  it was not, and `why` says so: the turn had
                              already had its one retry, the room's floor had
                              moved, the participant was talking, or the
                              encounter was closing
      audio_retry_outcome     how the retried turn ended. `delivered_whole`
                              means the second attempt was heard whole: some
                              words and some audio, stream closed, not cut
                              off, not short of its own words. `recovered`
                              means that AND that it was the lost line again
                              (`overlap` of the lost line's words, at least
                              RETRY_RECOVERED_OVERLAP) rather than an answer
                              to the retry prompt. An empty turn is neither.

    So a wave can be checked for how much of it the participant actually heard,
    and a retry that is failing more often than it succeeds shows up here rather
    than in a rater's puzzlement. Returned as counts, printed as a note: a
    recovered turn is a turn the participant heard, and an encounter that
    needed three of them is still scoreable - but it is worth less than the one
    next to it, and the number of turns that stayed broken (`unrecovered`,
    which counts suppressed retries as well as failed ones) is the figure that
    says whether this gateway is fit to run the wave on at all.
    """
    retried = [e for e in events if e.get("type") == "audio_retry"]
    suppressed = [e for e in events if e.get("type") == "audio_retry_suppressed"]
    outcomes = [e for e in events if e.get("type") == "audio_retry_outcome"]
    recovered = [e for e in outcomes if e.get("recovered")]
    failed = [e for e in outcomes if not e.get("recovered")]
    # Older records carry only `recovered`; read `delivered_whole` as that
    # when it is absent, so a wave recorded before the split still totals.
    whole = [e for e in outcomes
             if e.get("delivered_whole", e.get("recovered"))]
    # A retry whose outcome was never written - the encounter ended on it - is
    # counted as not recovered rather than quietly dropped from the ratio.
    unresolved = max(0, len(retried) - len(outcomes))
    return {
        "retried": len(retried),
        "recovered": len(recovered),
        "delivered_whole": len(whole),
        "unrecovered": len(failed) + unresolved + len(suppressed),
        "suppressed": len(suppressed),
        "suppressed_why": Counter(str(e.get("why")) for e in suppressed),
        "reasons": Counter(str(e.get("reason")) for e in retried + suppressed),
    }


def audio_recovery_note(events: List[dict]) -> str:
    r = audio_recovery(events)
    if not r["retried"] and not r["suppressed"]:
        return ""
    parts = [f"{r['retried']} retried", f"{r['recovered']} recovered",
             f"{r['delivered_whole']} delivered whole",
             f"{r['unrecovered']} unrecovered"]
    if r["suppressed"]:
        why = ", ".join(f"{n} {w}" for w, n in sorted(r["suppressed_why"].items()))
        parts.append(f"{r['suppressed']} suppressed ({why})")
    reasons = ", ".join(f"{n} {w}" for w, n in sorted(r["reasons"].items()))
    return "audio lost upstream: " + ", ".join(parts) + f"; by cause: {reasons}"


def pipeline_health(events: List[dict]) -> dict:
    """What the voice pipeline did to this encounter, as counts.

    Measured live on nto.gemini-live-2.5-flash with a lost participant, an
    encounter can be complete and still have been unconversable: the
    character heard half-thoughts, answered nothing for 47 s, opened the
    scene with no framing, or lost its socket. Each of those now leaves a
    row, and this gathers them so a wave can be checked for them:

      opening_framing              which way the interaction's framing line
                                   went: `connect_brief` (folded into the
                                   one brief the family reads) or the
                                   steering path (`stage_direction`,
                                   `trigger_cue`) on a family that honours
                                   a mid-session update
      reply_missing / reply_retry  a reply the runner asked for that never
                                   began, and whether it was asked for again
      realtime_session_reconnected the gateway closed a 1:1 socket and the
                                   runner rebuilt it inside the encounter
      empty agent turns            assistant_turn rows with no text, or with
                                   text and no audio delivered
      user_turn fragments          participant turns of <= 3 words: the
                                   shape a pause split leaves behind
    """
    turns = [e for e in events if e.get("type") == "assistant_turn"]
    empty = [e for e in turns if not (e.get("text") or "").strip()]
    voiceless = [e for e in turns if (e.get("text") or "").strip()
                 and e.get("audio_ms") is not None and not e.get("audio_ms")]
    user = [e for e in events if e.get("type") == "user_turn"]
    short = [e for e in user
             if len((e.get("text") or "").split()) <= 3]
    framing = Counter(str(e.get("via")) for e in events
                      if e.get("type") == "opening_framing")
    missing = [e for e in events if e.get("type") == "reply_missing"]
    retries = [e for e in events if e.get("type") == "reply_retry"]
    withdrawn = [e for e in events if e.get("type") == "turn_end_withdrawn"]
    return {
        # A turn end the runner's bar marked inside a mid-thought pause and
        # then withdrew when speech resumed: the pause split that did NOT
        # happen. Zero on a fluent participant; a lost one produces several.
        "turn_ends_withdrawn": len(withdrawn),
        "agent_turns": len(turns),
        "empty_agent_turns": len(empty),
        "voiceless_agent_turns": len(voiceless),
        "user_turns": len(user),
        "short_user_turns": len(short),
        "opening_framing": framing,
        "reply_missing": len(missing),
        "reply_reasked": sum(1 for e in retries if e.get("asked")),
        "reconnects": sum(1 for e in events
                          if e.get("type") == "realtime_session_reconnected"),
    }


def pipeline_notes(events: List[dict]) -> List[str]:
    h = pipeline_health(events)
    out: List[str] = []
    if h["opening_framing"]:
        via = ", ".join(f"{n} via {w}" for w, n in sorted(h["opening_framing"].items()))
        out.append(f"framing line: {via}")
    if h["reply_missing"]:
        out.append(f"replies never begun: {h['reply_missing']} "
                   f"({h['reply_reasked']} asked again)")
    if h["turn_ends_withdrawn"]:
        out.append(f"mid-thought pauses held as one turn: "
                   f"{h['turn_ends_withdrawn']}")
    if h["reconnects"]:
        out.append(f"gateway closed the session {h['reconnects']} time(s); "
                   "reconnected inside the encounter")
    if h["empty_agent_turns"] or h["voiceless_agent_turns"]:
        out.append(f"agent turns with nothing in them: {h['empty_agent_turns']} "
                   f"empty, {h['voiceless_agent_turns']} voiceless, "
                   f"of {h['agent_turns']}")
    if h["user_turns"] and h["short_user_turns"] * 2 > h["user_turns"]:
        out.append(f"participant turns of three words or fewer: "
                   f"{h['short_user_turns']} of {h['user_turns']} — the shape "
                   "a pause split leaves; check the transcript for fragments")
    return out


def steered_fraction(counts: Counter) -> str:
    """How much of the encounter the director actually shaped, as a bare fact.

    Deliberately NOT a pass/fail test. A turn that runs unsteered is normal --
    the director injects a direction only when it has something to say, and
    encounter_record.build records that as `stage_direction: null` precisely so
    an unsteered turn stays distinguishable from a missing record. An earlier
    version of this file flagged "fewer directions than replies" as a failure
    and reported 15 of 27 healthy reference encounters as unusable, which is the
    exact failure this tool exists to catch, committed by the tool itself.

    So the ratio is printed for a human to weigh against the scenario's own
    planted-trigger count, and only the absolute zero case -- the director never
    answered once in the whole encounter -- is treated as a finding.
    """
    pairs, directions = counts.get("steering_pair", 0), counts.get("stage_direction", 0)
    if not pairs:
        return ""
    return f"{directions}/{pairs} replies steered"


def report(session_dir: Path, verbose: bool) -> bool:
    ok, findings, notes, counts = check(session_dir)
    name = session_dir.name
    steered = steered_fraction(counts)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   ({steered})" if steered else ""))
    if verbose and counts:
        for t, n in counts.most_common():
            print(f"        {n:5d}  {t}")
    for f in findings:
        print(f"        -> {f}")
    # Printed under a PASS as well as a FAIL: an encounter can be scoreable and
    # still be worth less than the one next to it, and a rater assigning weight
    # to coverage needs to see that even when nothing failed.
    for n in notes:
        print(f"        .  {n}")
    return ok


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", help="an encounter directory, or a sessions directory with --all")
    ap.add_argument("--all", action="store_true", help="check every encounter under PATH")
    ap.add_argument("-q", "--quiet", action="store_true", help="omit the per-type breakdown")
    args = ap.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        print(f"no such path: {root}", file=sys.stderr)
        return 2

    dirs = sorted(p for p in root.iterdir() if p.is_dir()) if args.all else [root]
    if not dirs:
        print(f"no encounter directories under {root}", file=sys.stderr)
        return 2

    results = [report(d, not args.quiet) for d in dirs]
    passed = sum(results)
    if len(dirs) > 1:
        print(f"\n{passed}/{len(dirs)} encounters usable as data")
    return 0 if passed == len(dirs) else 1


if __name__ == "__main__":
    # stdout is reconfigured before anything prints because a Windows console
    # is cp1252 and this module's output includes characters it cannot encode.
    # The sibling tools/gen_scenario_map.py is the cautionary tale: it printed
    # before doing this, crashed on Windows, and truncated its output file.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    raise SystemExit(main())
