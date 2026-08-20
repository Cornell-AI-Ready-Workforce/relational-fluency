"""Check that an encounter was captured completely.

Run after a session — or over a whole collection wave — to catch the failures
that are cheap to fix on day one and impossible to fix afterwards: an encounter
with no participant audio, a transcript missing one side, triggers that never
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
    checks.append((bool(record), "record built", record.get("encounter_id", "—")))
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
    checks.append((a_turns > 0, "agent transcript", f"{a_turns} turns"))

    # --- audio: both channels, non-trivial ---
    user_wav = session_dir / "user_audio.wav"
    agent_wavs = sorted(session_dir.glob("assistant_audio*.wav"))
    u_secs = _wav_seconds(user_wav)
    a_secs = sum(_wav_seconds(p) for p in agent_wavs)
    checks.append((u_secs > 1.0, "participant audio", f"{u_secs:.1f}s"))
    checks.append((a_secs > 1.0, "agent audio", f"{a_secs:.1f}s ({len(agent_wavs)} file(s))"))

    # --- steering trail ---
    events = _events(session_dir)
    fired = [e for e in events if e.get("type") == "trigger_fired"]
    directions = record.get("steering_log", [])
    paired = [t for t in record.get("transcript", [])
              if t.get("role") == "agent" and t.get("stage_direction")]
    checks.append((len(directions) > 0, "stage directions logged", f"{len(directions)}"))

    # An agent turn with audio but no text is unscoreable — catch it here rather
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
        checks.append((
            len(ids) > 0,
            "planted triggers fired",
            f"{len(ids)}/{len(expected)} — {', '.join(ids) or 'none'}",
        ))
        esci_seen = {i for e in fired for i in (e.get("esci") or [])}
        checks.append((bool(esci_seen), "ESCI items exercised", f"{len(esci_seen)} distinct"))

    ok = all(c[0] for c in checks)
    return ok, checks


def _events(session_dir: Path) -> List[dict]:
    path = session_dir / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
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
    print(f"  {'— complete' if ok else '— INCOMPLETE'}")
    return ok


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--all":
        dirs = sorted(p for p in SESSIONS_DIR.iterdir() if p.is_dir())
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
