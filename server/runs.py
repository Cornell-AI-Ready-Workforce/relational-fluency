"""A run: one participant's sequence of four encounters.

Phase 1 has each participant complete one encounter per construct, conflict
management, influence, inspirational leadership, teamwork, in counterbalanced
order. A run holds that assignment so the four encounters are one session from
the participant's point of view, reachable from a single URL.

Variant choice matters for the RCT: attempt 1 uses one form and attempt 2 the
other, so the delta reads as skill change rather than an easier scenario. The
run records which form each construct was served, and `sibling_run` produces the
matching second-attempt sequence.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .scenarios_v3 import available, load_spec
from .storage import DATA_DIR

RUNS_DIR = DATA_DIR / "runs"

CONSTRUCT_ORDER = [
    "conflict_management",
    "influence",
    "inspirational_leadership",
    "teamwork",
]


def _by_construct() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for sid in available():
        spec = load_spec(sid)
        out.setdefault(spec["construct"], []).append(sid)
    for v in out.values():
        v.sort()
    return out


def _path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def create(
    participant_id: Optional[str] = None,
    *,
    variants: Optional[Dict[str, str]] = None,
    seed: Optional[int] = None,
    variant: Optional[str] = None,
    qualtrics_id: Optional[str] = None,
    cohort: str = "study",
) -> dict:
    """Assign four encounters, one per construct, in counterbalanced order.

    `variants` pins the form per construct, used to build a second attempt on
    the other form. Otherwise a form is chosen at random per construct, which
    balances across participants without needing a central counter.
    """
    pool = _by_construct()
    rng = random.Random(seed)

    order = [c for c in CONSTRUCT_ORDER if c in pool]
    rng.shuffle(order)  # counterbalance construct order across participants

    scenarios = []
    chosen: Dict[str, str] = {}
    for construct in order:
        options = pool[construct]
        if variants and construct in variants:
            sid = variants[construct]
        elif variant:
            # Pin every construct to one form. Useful for piloting a single set
            # rather than a random mix across participants.
            wanted = [o for o in options if load_spec(o)["variant"].upper() == variant.upper()]
            sid = wanted[0] if wanted else rng.choice(options)
        else:
            sid = rng.choice(options)
        chosen[construct] = sid
        spec = load_spec(sid)
        scenarios.append({
            "id": sid,
            "construct": construct,
            "variant": spec["variant"],
            "title": spec["title"],
            "parallel_form": spec.get("parallel_form"),
        })

    run = {
        "run_id": uuid.uuid4().hex[:12],
        "participant_id": participant_id,
        # The join keys for analysis. qualtrics_id ties this run to one survey
        # response; cohort separates internal test traffic from study data so a
        # bug-hunting session can never contaminate the dataset.
        "qualtrics_id": qualtrics_id,
        "cohort": cohort,
        "created_at": time.time(),
        "scenarios": scenarios,
        "index": 0,
        "completed": [],
        "variants": chosen,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _path(run["run_id"]).write_text(json.dumps(run, indent=2))
    return run


def find_for_participant(participant_id: str) -> Optional[dict]:
    """The participant's existing run, if any.

    Participants close tabs, lose connection, and come back. Handing them a
    fresh run would restart the sequence and produce a second partial record
    under the same key, so a returning participant resumes where they were.
    """
    if not participant_id or not RUNS_DIR.exists():
        return None
    best = None
    for f in RUNS_DIR.glob("*.json"):
        try:
            run = json.loads(f.read_text())
        except ValueError:
            continue
        if run.get("participant_id") != participant_id:
            continue
        if best is None or run.get("created_at", 0) > best.get("created_at", 0):
            best = run
    return best


def completion_code(run: dict) -> str:
    """Code the participant carries back to the survey as proof of completion.

    Derived from the run id so it can be checked later without a lookup table,
    and prefixed so a partial run is visibly not a finished one.
    """
    import hashlib

    digest = hashlib.sha256(run["run_id"].encode()).hexdigest()[:8].upper()
    finished = run["index"] >= len(run["scenarios"])
    return f"RF-{digest}" if finished else f"RF-PARTIAL-{digest}"


def get(run_id: str) -> Optional[dict]:
    p = _path(run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def save(run: dict) -> None:
    _path(run["run_id"]).write_text(json.dumps(run, indent=2))


def advance(run_id: str, session_id: Optional[str] = None) -> Optional[dict]:
    """Mark the current encounter done and move to the next."""
    run = get(run_id)
    if run is None:
        return None
    if run["index"] < len(run["scenarios"]):
        entry = dict(run["scenarios"][run["index"]])
        entry["session_id"] = session_id
        entry["finished_at"] = time.time()
        run["completed"].append(entry)
        run["index"] += 1
    save(run)
    return run


def view(run: dict) -> dict:
    """Client-facing shape: where we are and what is next."""
    i, total = run["index"], len(run["scenarios"])
    current = run["scenarios"][i] if i < total else None
    nxt = run["scenarios"][i + 1] if i + 1 < total else None
    return {
        "run_id": run["run_id"],
        "participant_id": run.get("participant_id"),
        "qualtrics_id": run.get("qualtrics_id"),
        "cohort": run.get("cohort", "study"),
        "completion_code": completion_code(run),
        "position": min(i + 1, total),
        "total": total,
        "current": current,
        "next": nxt,
        "done": current is None,
        "completed": [c["id"] for c in run.get("completed", [])],
    }


def sibling_run(run_id: str, participant_id: Optional[str] = None) -> Optional[dict]:
    """The second-attempt sequence: same constructs, the other variant each."""
    run = get(run_id)
    if run is None:
        return None
    flipped = {}
    for construct, sid in run["variants"].items():
        spec = load_spec(sid)
        flipped[construct] = spec.get("parallel_form") or sid
    return create(participant_id or run.get("participant_id"), variants=flipped)
