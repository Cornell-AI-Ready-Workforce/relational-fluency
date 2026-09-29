"""What one simulated encounter did, as numbers, and whether they are acceptable.

summarize() takes the session's events.jsonl (the server's account) and the
driver's timeline (what the simulated participant actually said, and when) and
returns the metrics the pre-deploy check compares:

  lines_said        stimulus lines the driver sent
  lines_heard       of those, how many the server turned into a participant
                    turn: a user_turn whose transcript holds at least half the
                    line's words, arriving within HEARD_WINDOW_S of it, or a
                    line split across several turns that together do
  lines_answered    of the heard lines, how many had a character reply start
                    playing after the line's turn and before the next line
  phantom_turns     participant turns that match no line said in the
                    preceding PHANTOM_LOOKBACK_S: the "Thank you" / "Bye-bye"
                    turns nobody said (#21), which characters then answer
  refused_creates   gateway refusals of an extra response.create (voice_error
                    "the extra response.create was refused"): benign one at a
                    time, a symptom of a double-fire when they pile up
  voice_error       voice_error events that are neither of the two known
                    benign kinds (a refused create; a cancel with nothing to
                    cancel, response_cancel_not_active)
  reply_missing     turns the gateway never answered and the runner re-asked
  error_frames      `error` frames sent to the page, each one a "Something went
                    wrong" the participant would have read
  triggers_fired    planted triggers that fired (trigger_fired events)
  spoke_first       character replies that started playing while the
                    participant had not yet opened the conversation (between
                    awaiting_participant and participant_opened): none is
                    allowed since pipeline 2026-09-28b, whatever the baseline
  speech_end_to_first_played_s   p50 / p90 from the end of the participant's
                    speech to the first sample of the FIRST reply to it
                    playing, the delay a participant actually waits. One value
                    per stretch of speech: a second reply to the same speech (a
                    silence probe, reply_index 1) or a second character in a
                    room is not a wait anybody sat through

compare() holds those against tools/sim/baseline.json within its tolerances.
Both are pure functions of their inputs, so tests/test_sim_analysis.py can pin
them on small synthetic event lists without a gateway.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence

HEARD_WINDOW_S = 45.0        # a line's transcript can land well after it ends in a busy room
PHANTOM_LOOKBACK_S = 60.0
LINE_CONTAINMENT = 0.5       # a turn hears a line when it holds this share of the line's words
TURN_FROM_LINES = 0.5        # a turn is real when this share of its words were said
ANSWER_GRACE_S = 30.0        # the last line's reply window

REFUSED_CREATE = ("extra response.create was refused", "already_has_active_response")
BENIGN_CANCEL = ("response_cancel_not_active",)

_WORD = re.compile(r"[a-z0-9]+")


def words(text: Optional[str]) -> List[str]:
    """Lower-case words with apostrophes folded in ("I'm" -> "im"), the
    same on both sides so a transcript's punctuation does not matter."""
    return _WORD.findall((text or "").lower().replace("'", "").replace("’", ""))


def containment(part: Iterable[str], whole: Iterable[str]) -> float:
    """Share of `part`'s distinct words that appear in `whole`."""
    p, w = set(part), set(whole)
    return len(p & w) / len(p) if p else 0.0


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (numpy's default), None when empty."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    v = xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
    return round(v, 3)


def _wall(e: dict) -> Optional[float]:
    w = e.get("wall")
    return float(w) if isinstance(w, (int, float)) else None


def summarize(events: List[dict], timeline: dict) -> dict:
    """Metrics for one encounter (see the module docstring)."""
    lines = sorted(timeline.get("lines") or [], key=lambda ln: ln["start"])
    turns = [e for e in events if e.get("type") == "user_turn" and _wall(e) is not None]
    plays = sorted(_wall(e) for e in events
                   if e.get("type") == "play_start" and _wall(e) is not None)

    heard: List[Optional[float]] = []           # per line: wall of its first turn, or None
    for i, ln in enumerate(lines):
        lw = words(ln.get("text"))
        window = [t for t in turns if ln["start"] <= _wall(t) <= ln["end"] + HEARD_WINDOW_S]
        hits = [_wall(t) for t in window
                if containment(lw, words(t.get("text"))) >= LINE_CONTAINMENT]
        if not hits:
            # A long, hesitant line the server ends at a pause comes back as
            # two turns, neither holding half of it. Taken together they do;
            # only turns that are mostly THIS line are pooled, and only up to
            # shortly after the next line starts, so a neighbour cannot lend
            # its words.
            nxt = lines[i + 1]["start"] + 10.0 if i + 1 < len(lines) else float("inf")
            parts = [t for t in window if _wall(t) <= nxt
                     and containment(words(t.get("text")), lw) >= TURN_FROM_LINES]
            pooled = [w for t in parts for w in words(t.get("text"))]
            if parts and containment(lw, pooled) >= LINE_CONTAINMENT:
                hits = [_wall(t) for t in parts]
        heard.append(min(hits) if hits else None)

    answered = 0
    for i, (ln, turn_at) in enumerate(zip(lines, heard)):
        if turn_at is None:
            continue
        until = lines[i + 1]["start"] if i + 1 < len(lines) else ln["end"] + ANSWER_GRACE_S
        if any(turn_at <= p <= until for p in plays):
            answered += 1

    phantoms = []
    for t in turns:
        tw, at = words(t.get("text")), _wall(t)
        recent = set()
        for ln in lines:
            if ln["start"] <= at and ln["end"] >= at - PHANTOM_LOOKBACK_S:
                recent.update(words(ln.get("text")))
        if not tw or containment(tw, recent) < TURN_FROM_LINES:
            phantoms.append({"t": t.get("t"), "text": t.get("text")})

    refused = benign = other = 0
    other_msgs: List[str] = []
    for e in events:
        if e.get("type") != "voice_error":
            continue
        msg = str(e.get("message") or e.get("detail") or "")
        if any(k in msg for k in REFUSED_CREATE):
            refused += 1
        elif any(k in msg for k in BENIGN_CANCEL):
            benign += 1
        else:
            other += 1
            other_msgs.append(f"{e.get('where')}: {msg}"[:200])

    first_played: Dict[float, float] = {}       # speech end -> first reply played
    for e in events:
        if e.get("type") != "turn_timing":
            continue
        a, b = e.get("vad_speech_end"), e.get("first_audio_played")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b >= a:
            first_played[a] = min(b, first_played.get(a, b))
    latencies = [b - a for a, b in first_played.items()]

    spoke_first, waiting = 0, False
    for e in events:
        if e.get("type") == "awaiting_participant":
            waiting = True
        elif e.get("type") == "participant_opened":
            waiting = False
        elif e.get("type") == "play_start" and waiting:
            spoke_first += 1

    types = Counter(e.get("type") for e in events)
    started = next((e for e in events if e.get("type") == "realtime_session_started"), {})
    return {
        "session_id": timeline.get("session_id"),
        "completed": bool(timeline.get("completed")),
        "lines_said": len(lines),
        "lines_heard": sum(1 for h in heard if h is not None),
        "lines_answered": answered,
        "unheard_lines": [ln["name"] for ln, h in zip(lines, heard) if h is None],
        "participant_turns": len(turns),
        "turns_suppressed": dict(Counter(e.get("reason") for e in events
                                         if e.get("type") == "user_turn_suppressed")),
        "phantom_turns": len(phantoms),
        "phantom_turn_texts": phantoms,
        "refused_creates": refused,
        "voice_error": other,
        "voice_error_messages": other_msgs,
        "voice_error_benign_cancel": benign,
        "reply_missing": types.get("reply_missing", 0),
        "error_frames": len(timeline.get("error_frames") or []),
        "triggers_fired": types.get("trigger_fired", 0),
        "spoke_first": spoke_first,
        "trigger_ids": [e.get("trigger_id") for e in events if e.get("type") == "trigger_fired"],
        "assistant_turns": types.get("assistant_turn", 0),
        "speech_end_to_first_played_s": {
            "p50": percentile(latencies, 50), "p90": percentile(latencies, 90),
            "n": len(latencies)},
        "provenance": {k: started.get(k) for k in (
            "realtime_model", "pipeline_version", "room_pacing_version", "build",
            "director_model")},
    }


def rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 3) if den else None


# What each tolerance means. A check is (metric, how to read it, tolerance
# keys); the numbers live in baseline.json so changing one is a reviewed diff.
DEFAULT_TOLERANCES = {
    "heard_rate_drop": 0.15,         # lines_heard / lines_said may fall this much
    "answered_rate_drop": 0.20,      # lines_answered / lines_heard may fall this much
    "phantom_turns_extra": 1,
    "refused_creates_extra": 3,
    "voice_error_extra": 0,
    "reply_missing_extra": 1,
    "error_frames_extra": 0,
    "triggers_fired_drop": 1,
    "latency_p50_ratio": 1.5, "latency_p50_slack_s": 0.5,
    "latency_p90_ratio": 1.5, "latency_p90_slack_s": 1.0,
}


def compare(metrics: dict, base: Optional[dict], tolerances: Optional[dict] = None) -> dict:
    """{"passed": bool, "checks": [...]} for one scenario against its baseline.

    Only regressions fail: doing better than the baseline is never a failure.
    A scenario with no baseline cannot pass, because a check that compares
    against nothing would say "pass" about anything.
    """
    tol = dict(DEFAULT_TOLERANCES)
    tol.update(tolerances or {})
    if not base:
        return {"passed": False, "checks": [{
            "metric": "baseline", "ok": False,
            "note": "no baseline for this scenario in tools/sim/baseline.json"}]}
    tol.update(base.get("tolerances") or {})
    checks = []

    def check(metric, value, baseline, limit, ok, note=""):
        checks.append({"metric": metric, "value": value, "baseline": baseline,
                       "limit": limit, "ok": bool(ok), **({"note": note} if note else {})})

    hr = rate(metrics["lines_heard"], metrics["lines_said"])
    bhr = rate(base["lines_heard"], base["lines_said"])
    lim = round(bhr - tol["heard_rate_drop"], 3) if bhr is not None else None
    check("heard_rate", hr, bhr, lim, hr is not None and (lim is None or hr >= lim),
          "no lines said" if hr is None else "")

    ar = rate(metrics["lines_answered"], metrics["lines_heard"])
    bar = rate(base["lines_answered"], base["lines_heard"])
    lim = round(bar - tol["answered_rate_drop"], 3) if bar is not None else None
    check("answered_rate", ar, bar, lim, ar is not None and (lim is None or ar >= lim),
          "no lines heard" if ar is None else "")

    for metric, key in (("phantom_turns", "phantom_turns_extra"),
                        ("refused_creates", "refused_creates_extra"),
                        ("voice_error", "voice_error_extra"),
                        ("reply_missing", "reply_missing_extra"),
                        ("error_frames", "error_frames_extra")):
        if metric not in base:
            continue
        lim = base[metric] + tol[key]
        check(metric, metrics[metric], base[metric], lim, metrics[metric] <= lim)

    # Not against the baseline: the rule allows none (pipeline 2026-09-28b).
    check("spoke_first", metrics.get("spoke_first", 0), 0, 0, not metrics.get("spoke_first"))

    lim = base["triggers_fired"] - tol["triggers_fired_drop"]
    check("triggers_fired", metrics["triggers_fired"], base["triggers_fired"], lim,
          metrics["triggers_fired"] >= lim)

    got = metrics["speech_end_to_first_played_s"]
    want = base.get("speech_end_to_first_played_s") or {}
    for q in ("p50", "p90"):
        b = want.get(q)
        v = got.get(q)
        if b is None:
            continue
        lim = round(b * tol[f"latency_{q}_ratio"] + tol[f"latency_{q}_slack_s"], 3)
        check(f"speech_end_to_first_played_{q}_s", v, b, lim, v is not None and v <= lim,
              "no timed turns" if v is None else "")

    return {"passed": all(c["ok"] for c in checks), "checks": checks}


# The metrics a baseline keeps: what compare() reads, and nothing that is only
# there to explain a failure (texts, ids, session ids).
BASELINE_KEYS = ("lines_said", "lines_heard", "lines_answered", "phantom_turns",
                 "refused_creates", "voice_error", "reply_missing", "error_frames",
                 "triggers_fired", "speech_end_to_first_played_s")


def baseline_entry(metrics: dict) -> dict:
    return {k: metrics[k] for k in BASELINE_KEYS}
