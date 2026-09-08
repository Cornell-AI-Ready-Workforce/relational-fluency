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

import hashlib
import hmac
import json
import os
import random
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .scenarios_v3 import available, load_spec
from .storage import DATA_DIR

RUNS_DIR = DATA_DIR / "runs"

CONSTRUCT_ORDER = [
    "conflict_management",
    "influence",
    "inspirational_leadership",
    "teamwork",
]

# Forms that must not be served alongside another construct, as
# (construct, forbidden variant, construct whose presence forbids it).
#
# S1 variation A ("Taken credit") and both Teamwork forms turn on the same
# situation: somebody else takes credit for the participant's work. A
# participant who handles that well in one will look competent in the other for
# reasons that have nothing to do with Conflict Management and Teamwork being
# distinct constructs, so serving both in one run compromises the discriminant
# validity of the pair. The canonical spec
# (reddit-analysis/scenarios/scenario-specifications.md, "Variation assignment")
# therefore requires S1 B or C wherever S4 is present, and the grounding data
# agrees: blame/public humiliation is attested 1,631 times against 77 for credit
# misattribution (reddit-analysis/situation-taxonomy.md §3).
#
# A run covers all four constructs, so Teamwork is always present and this rule
# always applies; it only changes an assignment on the runs whose draw actually
# landed on S1 A, which is about half of them (94 of 200 seeds measured). The
# table is written generally so the next exclusion is a line of data rather than
# a second special case.
FORM_EXCLUSIONS: List[Tuple[str, str, str]] = [
    ("conflict_management", "A", "teamwork"),
]


def _by_construct() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for sid in available():
        spec = load_spec(sid)
        out.setdefault(spec["construct"], []).append(sid)
    for v in out.values():
        v.sort()
    return out


def _apply_form_exclusions(chosen: Dict[str, str],
                           pinned: Optional[set] = None) -> List[dict]:
    """Apply FORM_EXCLUSIONS to a completed draw, in place.

    `pinned` names constructs whose form the caller chose explicitly. Those are
    reported but never changed: see the call site in create().

    Mutates ``chosen`` and returns one entry per exclusion that was triggered,
    so the run document can record that its assignment was corrected rather
    than drawn. An analyst comparing forms across participants needs to know
    which runs were steered and why, otherwise the B forms look over-sampled
    for no visible reason.

    Uses no randomness: the replacement is the first form in the construct's
    sorted list, so a given seed still yields the same run it did before, only
    corrected. When a construct has no permitted alternative the pairing is
    recorded as unresolved and the draw is left alone — refusing to build the
    run would strand a recruited participant over a validity concern that a
    flag in the data can carry instead.
    """
    applied: List[dict] = []
    pool = _by_construct()
    for construct, forbidden_variant, requires in FORM_EXCLUSIONS:
        sid = chosen.get(construct)
        if sid is None or requires not in chosen:
            continue
        forbidden = forbidden_variant.upper()
        if load_spec(sid)["variant"].upper() != forbidden:
            continue
        alternatives = [
            o for o in pool.get(construct, [])
            if load_spec(o)["variant"].upper() != forbidden
        ]
        entry = {
            "construct": construct,
            "excluded_variant": forbidden_variant,
            "because_run_contains": requires,
            "was": sid,
        }
        if pinned and construct in pinned:
            # Somebody asked for this form deliberately. Honour it and say so.
            entry["resolved"] = False
            entry["now"] = sid
            entry["reason"] = "form was pinned by the caller; exclusion not applied"
            applied.append(entry)
            continue
        if not alternatives:
            entry["resolved"] = False
            entry["now"] = sid
        else:
            chosen[construct] = alternatives[0]
            entry["resolved"] = True
            entry["now"] = alternatives[0]
        applied.append(entry)
    return applied


_RUN_ID_RE = re.compile(r"[0-9a-f]{12}")

# A usable participant key: the CloudResearch/Prolific-style identifier Qualtrics
# pipes into /start. Deliberately narrow, everything a working pipe produces is
# alphanumeric with at most - and _, and everything a broken pipe produces
# (empty, "${e://Field/ParticipantKey}", a stray URL, a sentence) is not.
_PARTICIPANT_KEY_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")

# Values that are shaped like a key but are plainly not one: the name of the
# embedded field, or a placeholder the survey never filled in. Accepting one of
# these is worse than rejecting it, because every participant sends the SAME
# string, so find_for_participant hands the second arrival the first person's
# half-finished run.
_PARTICIPANT_KEY_PLACEHOLDERS = {
    "participantkey", "participant_key", "participantid", "participant_id",
    "prolificpid", "prolific_pid", "responseid", "response_id", "workerid",
    "worker_id", "embedded", "undefined", "unknown", "null", "none", "empty",
    "xxxxxx", "xxxxxxxx",
}


def normalize_participant_key(raw: Optional[str]) -> Tuple[Optional[str], str]:
    """Return (key, status) for a participant key arriving from the survey.

    status is "ok" with the cleaned key, or one of "missing"/"unpiped"/
    "placeholder"/"malformed" with key None. Qualtrics piping fails in known
    ways (see docs/OPERATIONS.md, which even tells the operator to guess the
    embedded field name), and both failure modes corrupt the data silently: an
    undefined field renders as the empty string, so every arrival forks a fresh
    unattributable run; an unreplaced field renders as the literal
    "${e://Field/ParticipantKey}", so every participant collides into one run.
    Callers must decide what to do with a bad key, but must never take it at
    face value.
    """
    key = (raw or "").strip()
    if not key:
        return None, "missing"
    # Unreplaced Qualtrics piping, in any of the spellings it leaks in as.
    if "${" in key or "e://" in key or "}" in key:
        return None, "unpiped"
    if key.lower() in _PARTICIPANT_KEY_PLACEHOLDERS:
        return None, "placeholder"
    if not _PARTICIPANT_KEY_RE.fullmatch(key):
        return None, "malformed"
    return key, "ok"


def _path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def _write_atomic(p: Path, data: str) -> None:
    """Write via a temp file + os.replace so a crash mid-write can never leave a
    truncated run JSON (which would 404 the participant and fork a duplicate)."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, p)


def _run_code_secret() -> bytes:
    """Server-side key for the completion HMAC.

    Kept out of the participant-visible run id so a partial code cannot be
    turned into a finished code by editing a string. Prefers an env override;
    otherwise a per-deployment key persisted next to the data.
    """
    env = os.environ.get("RUN_CODE_SECRET")
    if env:
        return env.encode()
    key_path = DATA_DIR / ".run_code_secret"
    try:
        existing = key_path.read_bytes()
        if existing:
            return existing
    except OSError:
        pass
    secret = os.urandom(32)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(secret)
    except OSError:
        pass
    return secret


def create(
    participant_id: Optional[str] = None,
    *,
    variants: Optional[Dict[str, str]] = None,
    seed: Optional[int] = None,
    variant: Optional[str] = None,
    qualtrics_id: Optional[str] = None,
    cohort: str = "study",
    key_status: Optional[str] = None,
    raw_participant_key: Optional[str] = None,
) -> dict:
    """Assign four encounters, one per construct, in counterbalanced order.

    `variants` pins the form per construct, used to build a second attempt on
    the other form. Otherwise a form is chosen at random per construct, which
    balances across participants without needing a central counter.

    `key_status`/`raw_participant_key` record how the participant key arrived
    (see normalize_participant_key). They matter when the key was unusable and
    the caller substituted a synthetic identifier: the raw value is the only
    thing left to hand-join on, so it is kept rather than discarded.
    """
    pool = _by_construct()
    rng = random.Random(seed)

    order = [c for c in CONSTRUCT_ORDER if c in pool]
    rng.shuffle(order)  # counterbalance construct order across participants

    chosen: Dict[str, str] = {}
    # Constructs whose form the caller pinned rather than leaving to the draw.
    # sibling_run pins every construct to build attempt 2 on the other parallel
    # form; applying the cross-construct exclusion to a pin silently reverted
    # that flip and made attempt 2 repeat attempt 1's encounter, which destroys
    # the pre/post comparison the two forms exist for.
    pinned: set = set()
    for construct in order:
        options = pool[construct]
        if variants and construct in variants:
            sid = variants[construct]
            pinned.add(construct)
        elif variant:
            pinned.add(construct)
            # Pin every construct to one form. Useful for piloting a single set
            # rather than a random mix across participants.
            wanted = [o for o in options if load_spec(o)["variant"].upper() == variant.upper()]
            sid = wanted[0] if wanted else rng.choice(options)
        else:
            sid = rng.choice(options)
        chosen[construct] = sid

    # The draw above is per construct and cannot see the run as a whole, so the
    # cross-construct exclusions are applied once the full set is known. Done
    # after the draws (and using no randomness of its own) so a given seed still
    # produces the same assignment it did before, only corrected.
    #
    # A pinned form is left alone. An explicit pin is somebody's decision — the
    # operator piloting one form, or sibling_run building attempt 2 on the other
    # form — and silently overriding it is worse than the pairing it avoids,
    # because the override is invisible while the consequence (a repeated
    # encounter, or a pilot that did not pilot what was asked for) is not
    # attributable to anything. The conflict is recorded instead, so it shows up
    # in the run document rather than as a puzzling result.
    exclusions = _apply_form_exclusions(chosen, pinned=pinned)

    scenarios = []
    for construct in order:
        sid = chosen[construct]
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
        # How the participant key arrived: "ok", or the failure that made the
        # caller substitute a synthetic identifier, with what actually arrived.
        # Kept so a Qualtrics piping failure is visible in the data instead of
        # only in a log line nobody reads.
        "participant_key_status": key_status,
        "raw_participant_key": raw_participant_key,
        "created_at": time.time(),
        "scenarios": scenarios,
        "index": 0,
        "completed": [],
        "variants": chosen,
        # Empty on the runs whose draw was already legal (about half of them),
        # one entry on the rest. Recorded so an analyst can see that a form was
        # steered rather than drawn — otherwise the B forms simply look
        # over-sampled — and can tell a corrected run from one where no
        # permitted alternative existed.
        "form_exclusions": exclusions,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(_path(run["run_id"]), json.dumps(run, indent=2))
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
            run = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if run.get("participant_id") != participant_id:
            continue
        if best is None or run.get("created_at", 0) > best.get("created_at", 0):
            best = run
    return best


def find_by_participant_record(record_id: str) -> Optional[dict]:
    """The run that minted this participant record, if any.

    The participant websocket carries only the participant *record* id (the
    `p_...` in the URL), not the run id, so without this lookup an encounter has
    no way to know which run, cohort or participant key it belongs to and the
    only link back is the browser's advance POST. Resolving it here keeps that
    join server-side, so an encounter is attributable even if the client never
    reports back.

    Scans the run files, as find_for_participant does. A wave is a few hundred
    runs and this runs once per encounter start, so a directory scan is cheaper
    than maintaining an index that could disagree with the files.
    """
    if not record_id or not RUNS_DIR.exists():
        return None
    best = None
    for f in RUNS_DIR.glob("*.json"):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if run.get("participant_record_id") != record_id:
            continue
        if best is None or run.get("created_at", 0) > best.get("created_at", 0):
            best = run
    return best


def completion_code(run: dict) -> str:
    """Code the participant carries back to the survey as proof of completion.

    The digest is an HMAC over the run id *and* the finished state keyed by a
    server-side secret, so the finished code cannot be forged from the partial
    one (the two now differ by more than the literal "PARTIAL-" substring) and
    cannot be computed from the public run id alone.
    """
    finished = run["index"] >= len(run["scenarios"])
    state = "finished" if finished else "partial"
    msg = f"{run['run_id']}:{state}".encode()
    digest = hmac.new(_run_code_secret(), msg, hashlib.sha256).hexdigest()[:8].upper()
    return f"RF-{digest}" if finished else f"RF-PARTIAL-{digest}"


def get(run_id: str) -> Optional[dict]:
    # Run ids are uuid4().hex[:12]. Reject anything else before it reaches the
    # filesystem so a crafted id (backslashes, drive letters, ../) cannot escape
    # RUNS_DIR and read or write an arbitrary *.json on a Windows host.
    if not run_id or not _RUN_ID_RE.fullmatch(run_id):
        return None
    p = _path(run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def save(run: dict) -> None:
    _write_atomic(_path(run["run_id"]), json.dumps(run, indent=2))


def withdraw(run_id: str, session_id: Optional[str] = None,
             reason: Optional[str] = None) -> Optional[dict]:
    """Record that the participant stopped the study, and stop enrolling them.

    The consent text promises participants they may stop at any time. Honouring
    that needs two things a client-side stop cannot give: the refusal has to
    survive in the data, so an analyst can tell a withdrawal from a dropout and
    an incomplete run from an abandoned tab, and the run has to stop handing out
    encounters, so reopening the study link does not quietly enrol them in the
    remaining ones.

    Idempotent: a second call keeps the first timestamp, because that is when
    they actually withdrew.
    """
    run = get(run_id)
    if run is None:
        return None
    if not run.get("withdrawn"):
        run["withdrawn"] = {
            "at": time.time(),
            "reason": reason or "participant_withdrew",
            # Which encounter they were in when they stopped, and how far the
            # run had got, so a partial record is interpretable later.
            "session_id": session_id or None,
            "index": run.get("index", 0),
            "completed": len(run.get("completed", [])),
        }
        save(run)
    return run


def advance(run_id: str, session_id: Optional[str] = None) -> Optional[dict]:
    """Mark the current encounter done and move to the next."""
    run = get(run_id)
    if run is None:
        return None
    # A withdrawn run never advances. An encounter_complete or a retried POST
    # racing in behind the participant's own decision to stop must not enrol
    # them in the next encounter.
    if run.get("withdrawn"):
        return run
    # Idempotent on session_id: a retried/duplicated advance for an encounter
    # already recorded must not skip the next construct. If this session_id is
    # already in `completed`, return the run untouched.
    if session_id is not None and any(
        c.get("session_id") == session_id for c in run.get("completed", [])
    ):
        return run
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
        # Present only on a run the participant stopped. The page reads it on
        # boot: without it, someone who withdrew and later reopened the study
        # link would be handed the next encounter as though nothing had
        # happened, which is the opposite of what they asked for and would
        # record them again after they had withdrawn consent to continue.
        "withdrawn": run.get("withdrawn"),
    }


def sibling_run(run_id: str, participant_id: Optional[str] = None) -> Optional[dict]:
    """The second-attempt sequence: same constructs, the other variant each.

    The flip is a request, not a guarantee. create() still applies
    FORM_EXCLUSIONS to the pinned set, so where the parallel form is one this
    run may not contain — S1 A, whose partner S1 B was itself served because
    Teamwork is present — the construct is served on the same form as attempt 1
    and the run records the pin under form_exclusions. That costs the A/B delta
    for Conflict Management, which is the lesser harm against serving a pair the
    instrument forbids, and it is visible in the run document rather than silent.
    It resolves the moment an S1 C exists. Nothing calls this yet.

    The cohort carries over: a second attempt built from an internal or
    unattributed run is not study data either, and defaulting it to "study"
    would put test traffic into the dataset at exactly the point where nobody is
    watching.
    """
    run = get(run_id)
    if run is None:
        return None
    flipped = {}
    for construct, sid in run["variants"].items():
        spec = load_spec(sid)
        flipped[construct] = spec.get("parallel_form") or sid
    return create(
        participant_id or run.get("participant_id"),
        variants=flipped,
        cohort=run.get("cohort", "study"),
        key_status=run.get("participant_key_status"),
        raw_participant_key=run.get("raw_participant_key"),
    )
