"""Check that an encounter was captured completely.

Run after a session, or over a whole collection wave, to catch the failures
that are cheap to fix on day one and impossible to fix afterwards: an encounter
with no participant audio, a transcript missing one side, a participant
transcription channel that died before the encounter did, triggers that never
fired, or a record that cannot say which gateway produced it.

    python -m server.verify_record <session_id>
    python -m server.verify_record --all
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path
from typing import List, Tuple

from .encounter_record import build
from .storage import SESSIONS_DIR

Check = Tuple[bool, str, str]  # (ok, label, detail)


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def verify(session_dir: Path) -> Tuple[bool, List[Check]]:
    checks: List[Check] = []
    record = build(session_dir)

    # --- the record itself ---
    checks.append((bool(record), "record built", record.get("encounter_id", ", ")))
    prov = record.get("provenance") or {}
    checks.append((
        bool(prov.get("gateway") and prov.get("realtime_model")),
        "provenance recorded",
        f"{prov.get('gateway')} · {prov.get('realtime_model')}",
    ))

    # --- transcript: both sides present ---
    counts = record.get("counts", {})
    p_turns, a_turns = counts.get("participant_turns", 0), counts.get("agent_turns", 0)
    checks.append((p_turns > 0, "participant transcript", f"{p_turns} turns"))
    # The live transcriber sometimes returns a participant turn in the wrong
    # script (English speech transliterated into Devanagari has been seen).
    # Those turns are flagged in the event trail and the record now carries the
    # flag through, so this counts what a reader of the record would see rather
    # than re-walking events.jsonl for a fact the record already holds — and if
    # the carry ever breaks again, this check goes green with it, which is the
    # right coupling: the number reported here is the number a rater is exposed
    # to.
    mismatched = counts.get("script_mismatch_turns", 0)
    # A flagged encounter is repaired by the offline retranscription, which
    # writes transcript_participant_hq.json. Consult that, or the check keeps
    # failing forever after the operator has done exactly what its own message
    # told them to do — and a permanently latched FAIL is one people learn to
    # ignore, which costs more than the check is worth.
    #
    # But the question is whether the repair REACHED anything, not whether a
    # file is on disk. This used to stat the file, and passed while the
    # corrected text reached no reader at all: every consumer rebuilds from
    # events.jsonl, and the next encounter_record.write erased retranscribe's
    # in-place edit of record.json. So the check now asks the BUILT record,
    # which is what the rater packet and /api/sessions/{id}/record are made of.
    # A file that exists and a record that carries it are different claims, and
    # the `present` branch below names the gap between them instead of
    # certifying a repair nobody can read.
    #
    # There are then three ways for the record not to carry a repair, and they
    # are three different jobs for the operator. Reporting all of them as that
    # plumbing gap made this a latched FAIL nobody could clear: retranscribe
    # returns an existing cache verbatim unless --force is passed, so a cache
    # holding an empty transcript survives every plain re-run — no HTTP call, no
    # new text, the same red line — while the message pointed at a subsystem that
    # was working. A permanently red check whose message names the wrong cause is
    # worse than no check, because the operator learns to scroll past it.
    hq = record.get("participant_transcript_hq") or {}
    repaired = bool((hq.get("text") or "").strip())
    # Only asked when the answer can change the line, so a clean wave does not
    # pay a file read per encounter to be told there is nothing to read.
    cache = _hq_cache_state(session_dir) if mismatched and not repaired else "absent"
    if not mismatched:
        script_detail = "all turns"
    elif repaired:
        script_detail = (
            f"{mismatched} turn(s) flagged, retranscribed text carried in the record"
        )
    elif cache == "empty":
        script_detail = (
            f"{mismatched} turn(s) flagged; the re-transcription produced no "
            "text, so nothing was repaired — re-run it with --force (a plain "
            "re-run returns this same empty cache)"
        )
    elif cache == "unreadable":
        script_detail = (
            f"{mismatched} turn(s) flagged; transcript_participant_hq.json is "
            "not a readable transcript (a run killed or a disk filled mid-write)"
            " — re-run retranscribe, which regenerates a cache it cannot read"
        )
    elif cache == "present":
        # A readable cache with real text in it that the built record did not
        # pick up. Nothing produces this today; it is the canary for the seam
        # itself breaking again, which is the failure this check was rewritten
        # for, and it must not be folded into the two branches above.
        script_detail = (
            f"{mismatched} turn(s) flagged; transcript_participant_hq.json holds "
            "a transcript but the record does not carry it — the repair reaches "
            "no reader"
        )
    else:
        script_detail = f"{mismatched} turn(s) in another script, run retranscribe"
    checks.append((
        mismatched == 0 or repaired,
        "participant transcript script",
        script_detail,
    ))
    checks.append((a_turns > 0, "agent transcript", f"{a_turns} turns"))

    # --- the participant transcription channel survived the encounter ---
    #
    # In a group room the scribe session is the only participant transcript
    # channel. When it dies mid-encounter the runner keeps recording perfect
    # participant AUDIO with no transcript, so the artefact shows a run of agent
    # turns with nobody answering — and every other check here passes, because
    # the turns before the loss satisfy "participant transcript > 0". That is
    # the exact failure this tool exists to catch: a recoverable fault (the WAV
    # is intact, retranscribe can repair it) turned into a permanent silence
    # that reads as a participant who disengaged. It can only be repaired by
    # somebody who is told it happened.
    channel = record.get("participant_channel") or {}
    state = channel.get("state") or "ok"
    if state == "ok":
        channel_detail = "intact"
    elif state == "restored":
        # A later room brought a fresh scribe, so the hole is bounded — but it
        # is still a hole, and the turns inside it are still untranscribed.
        channel_detail = (
            f"LOST at {channel.get('lost_at')}s, recovered at "
            f"{channel.get('restored_at')}s "
            f"({channel.get('untranscribed_s')}s untranscribed)"
        )
    elif channel.get("untranscribed_s") is None:
        # The record could not date the end of the encounter: no re-open, no turn
        # after the loss, and no session_end — the trail simply stops, which is
        # what a killed process leaves behind. Saying "the last 0.0s" here (which
        # is what a floored subtraction produced) prints reassurance about an
        # encounter that may have run for minutes with nobody hearing the
        # participant. An unknown has to look like an unknown.
        channel_detail = (
            f"LOST at {channel.get('lost_at')}s and never recovered — how much "
            "of the encounter ran with no participant transcript cannot be "
            "established (the event trail ends at the loss) "
            "(user_audio.wav is intact; run retranscribe)"
        )
    else:
        channel_detail = (
            f"LOST at {channel.get('lost_at')}s and never recovered — the last "
            f"{channel.get('untranscribed_s')}s of the encounter ran with no "
            "participant transcript (user_audio.wav is intact; run retranscribe)"
        )
    checks.append((state == "ok", "participant channel", channel_detail))

    # --- audio: both channels, non-trivial ---
    user_wav = session_dir / "user_audio.wav"
    agent_wavs = sorted(session_dir.glob("assistant_audio*.wav"))
    u_secs = _wav_seconds(user_wav)
    a_secs = sum(_wav_seconds(p) for p in agent_wavs)
    checks.append((u_secs > 1.0, "participant audio", f"{u_secs:.1f}s"))
    checks.append((a_secs > 1.0, "agent audio", f"{a_secs:.1f}s ({len(agent_wavs)} file(s))"))

    # --- steering trail ---
    events = _events(session_dir)
    fired = _net_fired(events)
    directions = record.get("steering_log", [])
    paired = [t for t in record.get("transcript", [])
              if t.get("role") == "agent" and t.get("stage_direction")]
    checks.append((len(directions) > 0, "stage directions logged", f"{len(directions)}"))

    # An agent turn with audio but no text is unscoreable, catch it here rather
    # than in the rating queue.
    missing = [e for e in events if e.get("type") == "transcript_missing"]
    agent_turns = [e for e in events if e.get("type") == "assistant_turn"]
    checks.append((
        not missing,
        "every agent turn transcribed",
        "all" if not missing else f"{len(missing)}/{len(agent_turns)} MISSING TEXT",
    ))
    checks.append((len(paired) > 0, "directions paired to replies", f"{len(paired)}"))

    # --- triggers vs the scenario's plan ---
    expected = _expected_triggers(record.get("scenario"))
    if expected:
        ids = [e.get("trigger_id") for e in fired]
        # Distinct beats reached, against the beats the scenario plans. The old
        # condition was `len(ids) > 0`, which no encounter that fired a single
        # beat could ever fail — so the check that exists to say "this encounter
        # did not reach its scored moments" passed every encounter in a wave.
        # The README's own description of this tool is that an encounter which
        # fired 2 of 4 triggers "never reached half its scored moments", so
        # partial coverage is exactly what it is meant to catch. Full coverage
        # passes; anything short of it fails and names what was missed, which is
        # a judgement a human still has to make but can now see.
        reached = {i for i in ids if i}
        missed = [t for t in expected if t not in reached]
        # A probed beat and a volunteered one both count as delivered stimulus,
        # so both count toward coverage — but they are not the same evidence,
        # and the line has to say which. Every beat in every spec now carries a
        # probe, so a participant who says almost nothing gets walked through
        # the remaining beats by the watchdog, one per PROBE_AFTER_SECONDS, and
        # this line used to read "4/4, all": full coverage of an encounter in
        # which the participant reached nothing on their own. static/evidence
        # .html and the director view already split the two; this is that split,
        # in the one report a researcher runs across a whole wave.
        expected_set = set(expected)
        volunteered = {e.get("trigger_id") for e in fired if not e.get("probing")}
        volunteered.discard(None)
        n_vol = len(volunteered & expected_set)
        checks.append((
            not missed,
            "planted triggers fired",
            f"{len(reached)}/{len(expected)} "
            f"({n_vol} volunteered, {len(reached & expected_set) - n_vol} probed)"
            + (f", missed: {', '.join(missed)}" if missed else ""),
        ))
        esci_seen = {i for e in fired for i in (e.get("esci") or [])}
        checks.append((bool(esci_seen), "ESCI items exercised", f"{len(esci_seen)} distinct"))

    # LAST wins, and only an event that carries bytes counts — the same rule
    # encounter_record.build uses, because the two must not disagree about
    # whether a recording exists. The confirm endpoint writes a video_uploaded
    # event on EVERY attempt now, failures included, and the page retries the
    # confirm up to three times; the ordinary intermittent case (the PUT lands,
    # the first HEAD gets a 503 SlowDown, a retry succeeds) therefore leaves a
    # "failed" event AHEAD of the "ok" one. Taking the first event reported a
    # recording that is safely in the bucket as absent — the same false record
    # this tool exists to catch, committed by the tool itself.
    vid_events = [e for e in events if e.get("type") == "video_uploaded"]
    video_ev = next(
        (e for e in reversed(vid_events) if (e.get("bytes") or 0) > 0), None
    )
    if video_ev:
        video_detail = f"{video_ev['bytes']} bytes"
    elif not vid_events:
        # No confirm ever arrived: nothing was captured, or the tab died before
        # it could say so.
        video_detail = "NOT UPLOADED"
    else:
        # Attempts were made and none confirmed. Two different facts, and the
        # researcher's next move differs: "not_found" is S3 answering plainly
        # that the object is not there, while any other code (SlowDown, 503,
        # AccessDenied) is S3 declining to answer at all — the object may well
        # be in the bucket with only the confirmation lost. Calling the second
        # case NOT UPLOADED writes off a recording that exists.
        last = vid_events[-1]
        why = last.get("error") or last.get("client_error") or "no reason recorded"
        video_detail = (
            "NOT UPLOADED (S3 says the object is absent)"
            if why == "not_found"
            else f"CAPTURED, UPLOAD UNCONFIRMED: {why}"
        )
    checks.append((bool(video_ev), "webcam video in S3", video_detail))

    ok = all(c[0] for c in checks)
    return ok, checks


def _net_fired(events: List[dict]) -> List[dict]:
    """Planted beats that actually reached the participant.

    The event log is append-only, so a beat that was briefed and then not
    delivered — the floor grant failed on a member whose session had died —
    leaves its ``trigger_fired`` line behind and is cancelled by a later
    ``trigger_undelivered`` carrying the same index. Counting the raw firings
    would report a beat nobody spoke as one the participant faced, and that
    count is what decides whether an encounter is scoreable.

    Cancellation is positional, not by key: the runner rolls ``_trigger_idx``
    back when a grant fails, so the retry fires the same beat at the same index
    and must still count. Each retraction therefore cancels the most recent
    surviving firing that matches, and nothing earlier.

    ``trigger_deferred`` is deliberately not a retraction. A deferred beat was
    never briefed, so there is no firing to cancel; treating it as one would
    delete the legitimate firing that happens when the beat's own character
    finally takes the floor.
    """
    out: List[dict] = []
    for e in events:
        etype = e.get("type")
        if etype == "trigger_fired":
            out.append(e)
        elif etype == "trigger_undelivered":
            key = (e.get("trigger_id"), e.get("index"))
            for i in range(len(out) - 1, -1, -1):
                if (out[i].get("trigger_id"), out[i].get("index")) == key:
                    del out[i]
                    break
    return out


def _hq_cache_state(session_dir: Path) -> str:
    """What is actually in retranscribe's output file, in one word.

    "absent" — never run here. "unreadable" — the file is there and is not a
    transcript this reader can parse (killed mid-write, or an older shape).
    "empty" — valid, and its text is empty or whitespace: the re-transcription
    ran and produced nothing. "present" — a real transcript.

    The record cannot answer this, which is the whole point: build() collapses
    all three failures to `participant_transcript_hq: None`, and the operator's
    next move is different for each. "empty" in particular is the one the record
    can never distinguish and the only one that needs --force, because
    retranscribe.retranscribe() short-circuits on any cache it can parse.
    """
    path = session_dir / "transcript_participant_hq.json"
    if not path.is_file():
        return "absent"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(loaded, dict):
        return "unreadable"
    return "present" if (loaded.get("text") or "").strip() else "empty"


def _events(session_dir: Path) -> List[dict]:
    path = session_dir / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _expected_triggers(scenario_id) -> List[str]:
    if not scenario_id:
        return []
    try:
        from .scenarios_v3 import load_spec
        spec = load_spec(scenario_id)
    except Exception:
        return []
    return [t["id"] for i in spec.get("interactions", []) for t in i.get("triggers", [])]


def report(session_dir: Path) -> bool:
    ok, checks = verify(session_dir)
    print(f"\n{session_dir.name}")
    for passed, label, detail in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label:30} {detail}")
    print(f"  {'complete' if ok else 'INCOMPLETE'}")
    return ok


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--all":
        dirs = (sorted(p for p in SESSIONS_DIR.iterdir() if p.is_dir())
                if SESSIONS_DIR.exists() else [])
        if not dirs:
            print("no sessions recorded yet")
            print("\n0/0 encounters complete")
            return 0
        results = [report(d) for d in dirs]
        good = sum(results)
        print(f"\n{good}/{len(results)} encounters complete")
        return 0 if good == len(results) else 1
    d = SESSIONS_DIR / argv[0]
    if not d.exists():
        print(f"no such session: {argv[0]}")
        return 2
    return 0 if report(d) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
