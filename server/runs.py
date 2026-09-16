"""A run: one participant's sequence of four encounters.

Phase 1 has each participant complete one encounter per construct, conflict
management, influence, inspirational leadership, teamwork, in counterbalanced
order. A run holds that assignment so the four encounters are one session from
the participant's point of view, reachable from a single URL.

Variant choice matters for the RCT: attempt 1 uses one form and attempt 2 the
other, so the delta reads as skill change rather than an easier scenario. The
run records which form each construct was served, and `sibling_run` produces the
matching second-attempt sequence.

A run may also be restricted to part of the construct set — the study hands out
one link per arm (see ARMS below), and the arm is the only thing that differs
between them. The restriction is recorded on the run document as
`construct_pool`, next to `form_exclusions` and for the same reason: an analyst
must be able to read which arm a participant was in off the run itself, rather
than infer it from which scenarios happen to be in it.
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

from . import scenarios_v3
from .scenarios_v3 import available, load_spec
from .storage import DATA_DIR, record_withdrawal, replace_with_retry

RUNS_DIR = DATA_DIR / "runs"


class RunScanIncomplete(RuntimeError):
    """A directory pass could not read every run it needed to read.

    Raised instead of answering "no withdrawal found", because those two are not
    the same sentence and the caller acts on the difference: a gate that treats
    an unreadable file as a clean negative turns a withdrawal back into consent.
    The capture gate catches this and refuses (server/app.py's _withdrawn).
    """


# The cohorts a run may be in. Every consumer — ?cohort= on /api/runs and
# /api/sessions, the rating split, the analysis set — compares with ==, so a
# cohort that is not one of these three is not "a different group", it is a run
# that no filter can name: it falls out of ?cohort=study AND ?cohort=internal at
# once, so it is neither excluded from the dataset nor present in it. Round two
# gated who may set the parameter and left the string alone, and 'Internal',
# 'INTERNAL', ' internal ' and a newline-bearing one all landed on runs.
COHORTS: Tuple[str, ...] = ("study", "internal", "unattributed")


def known_cohorts() -> Tuple[str, ...]:
    """The cohort names a run may carry."""
    return COHORTS


def normalize_cohort(raw: Optional[str]) -> str:
    """The canonical spelling of a cohort name.

    Raises ValueError for anything else, which is the same refusal an unknown
    arm and an unknown variant letter already get, and for the same reason: a
    link that cannot build the run it claims to must fail where it is handed
    out. Storing 'banana' does not produce a differently-grouped run, it
    produces an invisible one, and nobody finds out until an analyst counts the
    wave and it is short.

    None and empty mean "not stated" and become the default, "study" — which is
    what every caller that omits the argument already meant.
    """
    s = " ".join(str(raw or "").split()).strip().lower()
    if not s:
        return "study"
    if s not in COHORTS:
        raise ValueError(
            f"unknown cohort: {raw!r} (a run may be in {', '.join(COHORTS)})")
    return s

CONSTRUCT_ORDER = [
    "conflict_management",
    "influence",
    "inspirational_leadership",
    "teamwork",
]

# How many encounters a participant is asked for before the completion code is a
# finished one. Four is the study's own number (one per construct, which is what
# CONSTRUCT_ORDER is for) and it does not follow from the size of the construct
# list once a run may be restricted to part of it, so it is named here rather
# than being len(CONSTRUCT_ORDER) by coincidence.
ENCOUNTERS_PER_RUN = 4


def _interaction_modes(scenario_id: str) -> List[str]:
    """The `mode` of each planned interaction in one scenario spec.

    The specs are the authority on what an encounter actually is: S3 opens in a
    group room and then runs a series of one-to-ones, S4 is group throughout,
    S1 and S2 are two-person from start to finish. Reading it off the spec means
    a new form (an S1 C, say) joins the right arm by being written, not by being
    added to a list here that somebody has to remember to update.
    """
    spec = load_spec(scenario_id)
    return [str(i.get("mode") or "") for i in (spec.get("interactions") or [])]


def _all_one_to_one(modes: List[str]) -> bool:
    return bool(modes) and all(m == "one_to_one" for m in modes)


def _has_group(modes: List[str]) -> bool:
    return any(m == "group" for m in modes)


# The arms the study's entry links hand out. An arm is a restriction on the
# construct pool and nothing else: the same run, the same four encounters, the
# same completion code, drawn from fewer constructs.
#
# The predicate is applied to every form of a construct, and a construct joins
# the arm only when ALL of its forms qualify. That is deliberate. The parallel
# forms exist so attempt 2 can be the other one, and an arm that contained S3 A
# but not S3 B would be an arm in which half the participants cannot have a
# second attempt — so a construct is either in an arm whole or not at all.
#
# Measured against scenarios/v3 as it stands: one_to_one keeps conflict
# management and influence; group keeps inspirational leadership and teamwork.
# Two constructs per arm, four encounters per run — see _slots_for for what that
# costs and how the run records it.
ARMS: Dict[str, dict] = {
    "full": {
        "predicate": None,
        "description": "every construct: one encounter each, counterbalanced",
    },
    "one_to_one": {
        "predicate": _all_one_to_one,
        "description": "constructs whose every interaction is a two-person "
                       "conversation",
    },
    "group": {
        "predicate": _has_group,
        "description": "constructs whose encounters open in a group room",
    },
}

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
# landed on S1 A, which is about a THIRD of them. Re-measured on this bank, in
# an isolated DATA_DIR: 68 of 200 seeds (0.340), 673 of 2000 (0.337), 3357 of
# 10000 (0.336, 95% CI [0.326, 0.345]) — what three forms per construct
# predicts, and the same 673/2000 that _apply_form_exclusions' own docstring
# reports 200 lines below. The figure this comment used to carry, "about half
# of them (94 of 200 seeds measured)", was measured when Conflict Management
# had two forms; it overstated the steered share by about 40%, and it is a
# figure a researcher sizes a wave against, so it is corrected rather than
# left to be read off a stale line.
# The table is written generally so the next exclusion is a line of data rather
# than a second special case.
FORM_EXCLUSIONS: List[Tuple[str, str, str]] = [
    ("conflict_management", "A", "teamwork"),
]


def known_variants() -> List[str]:
    """The form letters the scenario set actually carries, sorted.

    Computed from the specs rather than listed here, for the reason ARMS reads
    its predicates off them: an S1 C joins the study by being written.
    """
    return sorted({load_spec(sid)["variant"].upper() for sid in available()})


def normalize_variant(raw: Optional[str]) -> Optional[str]:
    """The form letter a caller pinned, upper-cased. None means "no pin".

    Raises ValueError for a letter no form carries. An unknown ARM is already
    refused at create(), and an unknown variant is the same operator mistake
    with a worse consequence, because it did not fail: create() added the
    construct to `pinned` before knowing whether any form matched the letter, so
    _apply_form_exclusions declined to correct S1 A and stamped the run "form
    was pinned by the caller; exclusion not applied" — the run document
    explaining away the very discriminant-validity pairing FORM_EXCLUSIONS
    exists to prevent. Measured over 200 seeds: no variant, 0 bad pairings;
    variant=Z, 94. Every run of a wave, silently, off one wrong letter in the
    Qualtrics redirect.

    An EMPTY value is not that mistake and is not refused. `&variant=` with
    nothing after it is what a half-filled link renders, and it pins nothing —
    it never reached the branch above and never could, so there is no wave to
    save by turning it away. What refusing it did cost was the arrival itself:
    a participant carrying that link got a 400 at the door, mid-study, with no
    way forward and no completion code. This entry path is built never to do
    that — an unusable participant KEY is waved through as `unattributed`
    rather than refused — and an absent form letter is a smaller thing than an
    absent key. Measured the same way: variant="", 0 bad pairings over 200
    seeds, identical to no variant at all.
    """
    if raw is None:
        return None
    v = str(raw).strip().upper()
    if not v:
        return None
    known = known_variants()
    if v not in known:
        raise ValueError(
            f"unknown variant: {raw!r} (the scenario set carries "
            f"{', '.join(known)})"
        )
    return v


def _by_construct() -> Dict[str, List[str]]:
    """{construct: [its forms, sorted]}, from the one place that decides it.

    Delegates to scenarios_v3.forms_by_construct() rather than rebuilding the
    map here. The specs name that function as the routing authority in their own
    comments (S1C, S3C), and two modules deriving "which forms are parallel"
    from the same data by two separate loops is how the two answers eventually
    differ. It also stops this module deep-copying every ~40 KB spec (load_spec
    does) once per slot of every run, for two scalar fields.
    """
    return scenarios_v3.forms_by_construct()


def _pin_sequences(variants: Optional[Dict[str, object]]) -> Dict[str, List[str]]:
    """`variants` as a PER-SLOT sequence per construct.

    A scalar is one form and still means what it always meant. A list is the
    forms to serve in slot order, first entry to the construct's first slot,
    and it is the piece that was missing: `variants` was one form per construct
    for the whole run, so an arm that gives a construct two slots served the
    same form in both — the same conversation twice, 200 runs in 200 on both
    arms, measured. One form per construct cannot express "the unseen one, then
    whichever of the seen ones", which is what a second attempt on a restricted
    arm has to say.

    Empty and None entries are dropped rather than rejected: a caller building a
    sequence from a filter can legitimately come up empty for a construct, and
    that means "nothing pinned here", which the draw already handles.
    """
    out: Dict[str, List[str]] = {}
    for construct, wanted in (variants or {}).items():
        if wanted is None:
            continue
        if isinstance(wanted, str):
            seq = [wanted] if wanted else []
        else:
            seq = [str(w) for w in wanted if w]
        if seq:
            out[str(construct)] = seq
    return out


def arm_constructs(arm: str) -> Tuple[List[str], List[dict]]:
    """(constructs in this arm, why each of the others is out).

    Both halves are returned because the second half is the part that has to
    reach the run document. "This run drew from two constructs" is not a finding
    an analyst can do anything with; "teamwork is out of the one_to_one arm
    because both of its forms are group encounters" is.
    """
    spec = ARMS.get(arm)
    if spec is None:
        raise ValueError(f"unknown arm: {arm!r}")
    pool = _by_construct()
    predicate = spec["predicate"]
    keep: List[str] = []
    dropped: List[dict] = []
    for construct in CONSTRUCT_ORDER:
        forms = pool.get(construct) or []
        if not forms:
            continue
        if predicate is None:
            keep.append(construct)
            continue
        failing = [f for f in forms if not predicate(_interaction_modes(f))]
        if not failing:
            keep.append(construct)
        else:
            dropped.append({
                "construct": construct,
                "forms": failing,
                # The modes are what actually decided it, so they travel with the
                # verdict rather than leaving a reader to re-open the specs.
                "interactions": {f: _interaction_modes(f) for f in failing},
            })
    return keep, dropped


def _resolve_pool(arm: Optional[str],
                  constructs: Optional[List[str]]) -> Tuple[List[str], dict]:
    """Which constructs this run may draw from, and the record of that decision.

    Raises ValueError for an arm nobody defined, or for a restriction that
    leaves nothing to draw. Both are an operator handing out a link that cannot
    build a run, and failing at creation names the problem; quietly widening the
    pool back to everything would hand the participant the wrong arm and record
    it as though it were the right one.
    """
    arm_name = (arm or "full").strip().lower() or "full"
    allowed, dropped = arm_constructs(arm_name)
    requested = None
    if constructs is not None:
        requested = [c for c in constructs]
        wanted = {c.strip() for c in requested if isinstance(c, str) and c.strip()}
        unknown = sorted(wanted - set(CONSTRUCT_ORDER))
        if unknown:
            raise ValueError(f"unknown construct(s): {', '.join(unknown)}")
        allowed = [c for c in allowed if c in wanted]
    if not allowed:
        raise ValueError(
            f"arm {arm_name!r} with constructs={constructs!r} leaves no "
            f"construct to draw from"
        )
    return allowed, {
        "arm": arm_name,
        "description": ARMS[arm_name]["description"],
        "constructs": list(allowed),
        # Null on the ordinary run. Present when a caller narrowed the pool by
        # hand, because then the arm name alone no longer describes the run.
        "requested_constructs": requested,
        "excluded_constructs": dropped,
        "encounters": ENCOUNTERS_PER_RUN,
    }


def _slots_for(order: List[str], n: int) -> List[str]:
    """n encounter slots from however many constructs the arm left.

    The unrestricted run has four constructs and four encounters, so this is the
    identity and the study's design is untouched: one encounter per construct,
    in the shuffled order.

    A restricted arm has fewer. Two of the four constructs are pure two-person
    encounters and two are group ones (see ARMS), so each arm has two constructs
    and four encounters to fill, and the pool is cycled: A, B, A, B. Cycling
    rather than blocking (A, A, B, B) keeps the two forms of a construct apart,
    which matters because they are the same situation twice and a participant
    who has just done one recognises the other immediately.

    This is the cost the arm links carry and it is not hidden: a restricted run
    spends TWO forms of each construct it draws and measures two of the four
    ESCI constructs rather than all four. Whether that leaves a construct with
    nothing for a second attempt depends on how many forms it has, and is
    counted rather than assumed — a two-form construct is spent, a three-form
    one keeps a reserve. create() records both facts under `construct_pool`
    (`repeated_constructs`, `forms_in_reserve`).
    """
    if not order:
        raise ValueError("no constructs to draw from")
    slots: List[str] = []
    while len(slots) < n:
        slots.extend(order)
    return slots[:n]


def _apply_form_exclusions(assignment: List[dict]) -> List[dict]:
    """Apply FORM_EXCLUSIONS to a completed draw, in place.

    `assignment` is the run's encounter slots in order, each a dict carrying at
    least "construct" and "id"; the exclusions are a statement about what may
    share a run, so they can only be applied once every slot is filled.

    A slot carrying "pinned" is one whose form the caller chose explicitly and
    got. Those are reported but never changed: see the call site in create().
    Marked per SLOT rather than per construct, because a restricted arm gives
    one construct two slots and a pin can only be honoured in the first of them
    — calling the second one pinned too would let a form nobody asked for
    inherit the exemption that only a deliberate choice earns.

    Mutates the slots and returns one entry per exclusion that was triggered, so
    the run document can record that its assignment was corrected rather than
    drawn. An analyst comparing forms across participants needs to know which
    runs were steered and why, otherwise the B forms look over-sampled for no
    visible reason.

    THE REPLACEMENT IS ROTATED ACROSS THE PERMITTED FORMS, NOT ALWAYS THE FIRST.

    Uses no randomness — it must not, because a recorded seed has to rebuild the
    run it recorded — but "no randomness" was implemented as "take
    alternatives[0]", and that is a counterbalancing fault the moment a
    construct has more than two forms. Measured on the unrestricted arm over
    2000 seeds: the pre-exclusion draw was clean (S1A 673 / S1B 690 / S1C 637),
    and every one of the 673 S1A draws was corrected to S1B and not once to
    S1C, so the arm that carries the whole study served Conflict Management
    68.1% S1B against 31.9% S1C. Downstream that made 7 attempt1→attempt2 pairs
    in 10 the same ordered pair, which confounds the pre/post delta on this
    construct with form order for most of the sample. The other three
    constructs, which no exclusion touches, ran at ~17% per ordered pair.

    So the choice among permitted forms is ROTATED on a digest of the draw this
    run actually made (every slot's construct and id, plus the construct being
    corrected). That is a pure function of the draw, so a given seed still
    rebuilds its own run exactly — test_a_seed_reproduces_its_run_exactly and
    test_the_pin_path_consumes_no_randomness_while_it_can_fill_the_slot both
    still hold — while ACROSS seeds the corrections spread evenly over the
    forms that are allowed to absorb them, because the other slots of the run
    vary. Same measurement after, same 2000 seeds: S1B 1014 / S1C 986, i.e.
    50.7% against 49.3%, and the ordered pairs 53.5/46.5 rather than 69.5/30.5.
    (Conflict Management still uses only two of its six ordered pairs on the
    full arm, and must: S1A is barred wherever Teamwork is, and Teamwork is in
    every full run. That is the exclusion doing its job, not a skew.)

    A form the run is already serving in another slot is passed over first — a
    restricted arm can give one construct two slots, and correcting one of them
    into a duplicate of the other would cost the participant an encounter rather
    than fix anything. When a construct has no permitted alternative the pairing
    is recorded as unresolved and the draw is left alone — refusing to build the
    run would strand a recruited participant over a validity concern that a flag
    in the data can carry instead.
    """
    applied: List[dict] = []
    pool = _by_construct()
    present = {slot["construct"] for slot in assignment}
    for construct, forbidden_variant, requires in FORM_EXCLUSIONS:
        if requires not in present:
            continue
        forbidden = forbidden_variant.upper()
        for slot in assignment:
            if slot["construct"] != construct:
                continue
            sid = slot["id"]
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
            if slot.get("pinned"):
                # Somebody asked for this form deliberately. Honour it and say so.
                entry["resolved"] = False
                entry["now"] = sid
                entry["reason"] = "form was pinned by the caller; exclusion not applied"
                applied.append(entry)
                continue
            elsewhere = {s["id"] for s in assignment if s is not slot}
            unused = [o for o in alternatives if o not in elsewhere]
            if not alternatives:
                entry["resolved"] = False
                entry["now"] = sid
            else:
                choices = unused or alternatives
                # Keyed on the whole draw, not on the slot alone: the other
                # slots are what differ between seeds, so this is what makes
                # the corrections land evenly across the permitted forms
                # instead of all on the first one. See the docstring.
                digest = hashlib.sha256(
                    ("|".join(f"{s['construct']}:{s['id']}" for s in assignment)
                     + f"|{construct}").encode()).hexdigest()[:8]
                slot["id"] = choices[int(digest, 16) % len(choices)]
                entry["resolved"] = True
                entry["now"] = slot["id"]
                if not unused:
                    entry["reason"] = ("the only permitted form is already served "
                                       "elsewhere in this run")
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
    # Validate where the filename is minted as well as where it is read (get()
    # already checks). save() takes the id straight off a run dict, so an id
    # that was never uuid4().hex[:12] would otherwise reach the filesystem on
    # the write side only — and on Windows a component like "nul" is a device
    # that accepts the bytes and discards them, which loses a participant's run
    # while every call returns cleanly.
    if not run_id or not _RUN_ID_RE.fullmatch(str(run_id)):
        raise ValueError(f"bad run_id: {run_id!r}")
    return RUNS_DIR / f"{run_id}.json"


def _write_atomic(p: Path, data: str) -> None:
    """Write via a temp file + os.replace so a crash mid-write can never leave a
    truncated run JSON (which would 404 the participant and fork a duplicate)."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    # Retried, not bare: on Windows this rename fails outright while any handle
    # is open on either side, and a run advance racing a participant's poll of
    # the same file is exactly that. See storage.replace_with_retry.
    replace_with_retry(tmp, p)


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
    variants: Optional[Dict[str, object]] = None,
    seed: Optional[int] = None,
    variant: Optional[str] = None,
    qualtrics_id: Optional[str] = None,
    cohort: str = "study",
    key_status: Optional[str] = None,
    raw_participant_key: Optional[str] = None,
    arm: Optional[str] = None,
    constructs: Optional[List[str]] = None,
) -> dict:
    """Assign four encounters, one per construct, in counterbalanced order.

    `variants` pins forms PER SLOT, used to build a second attempt on forms the
    participant has not met. A value may be one form id, which means the
    construct's first slot and is what this argument has always accepted, or a
    LIST of them in slot order — the construct's first slot takes the first
    entry, its second slot the second, and so on. Otherwise a form is chosen at
    random per slot, which balances across participants without needing a
    central counter.

    The list is the piece that made a reserve form reachable. One form per
    construct is fine while a run gives every construct one slot, and is not an
    answer at all on a restricted arm, which gives each of its two constructs
    two: the single pinned form went into both slots, so 200 runs of 200 on
    both arms served the participant one conversation twice. Nothing about that
    could be fixed in the caller, because one form per construct cannot say
    "the unseen one first, then whichever of the seen ones".

    A pin is honoured WITHOUT REPLACEMENT. A form already served in this run is
    skipped rather than served again, and a construct whose sequence runs out
    before its slots do falls through to the ordinary unseen draw for the rest,
    recorded under construct_pool.pinned_forms_unfilled. An unseen encounter is
    worth more to the participant and to the data than a literally honoured pin,
    and a silently repeated conversation is worth nothing to either.

    `arm` and `constructs` restrict which constructs the run may draw from (see
    ARMS). The restriction and everything it excluded is recorded on the run
    under `construct_pool`, so the arm a participant was in is a field rather
    than something to be reconstructed from their scenario list. Both default to
    the whole set, which is the unrestricted study run and is unchanged: four
    constructs, four encounters, one each.

    `key_status`/`raw_participant_key` record how the participant key arrived
    (see normalize_participant_key). They matter when the key was unusable and
    the caller substituted a synthetic identifier: the raw value is the only
    thing left to hand-join on, so it is kept rather than discarded.
    """
    pool = _by_construct()
    rng = random.Random(seed)
    # Phase 1 runs one form only: variant A for every construct, with the
    # order counterbalanced. DEFAULT_RUN_VARIANT=B pins the other form;
    # DEFAULT_RUN_VARIANT=random restores the per-construct coin flip. A
    # variant passed explicitly (URL, second attempt) still wins.
    variant_pin_source = "caller" if (variant or variants) else None
    if not variant and not variants:
        default = os.getenv("DEFAULT_RUN_VARIANT", "A").strip()
        if default and default.lower() != "random":
            variant = default
            variant_pin_source = "default_run_variant"
    # WHO PINNED IT, and why the run has to say. _apply_form_exclusions has one
    # escape hatch -- a form "pinned by the caller" is honoured and the run is
    # stamped "exclusion not applied" -- and DEFAULT_RUN_VARIANT reaches that
    # hatch through the same `variant` argument a URL does. With the default at
    # A, EVERY run pins S1A and every run containing teamwork takes the hatch,
    # so the S1A/Teamwork exclusion is off study-wide and each run carries a
    # sentence blaming a caller that does not exist. The mechanism is
    # deliberate (see docs/OPERATIONS.md, "Which scenarios a participant gets")
    # and is the PI's call, not this function's; what is NOT acceptable is a
    # record that cannot tell the two apart afterwards. So the run says which.

    # Refused here, beside the unknown arm, and for the same reason: a link that
    # cannot build the run it claims to must fail where it is handed out, not
    # quietly build a different one. See normalize_variant.
    variant_pin = normalize_variant(variant)

    # Refused here too, and for the same reason. See normalize_cohort: an
    # unknown cohort does not make a differently-grouped run, it makes one that
    # drops out of every filter at once.
    cohort = normalize_cohort(cohort)

    allowed, pool_record = _resolve_pool(arm, constructs)

    order = [c for c in CONSTRUCT_ORDER if c in pool and c in allowed]
    rng.shuffle(order)  # counterbalance construct order across participants

    # Always four encounters, however many constructs the arm left standing. The
    # participant is promised four and paid for four, and the completion code is
    # an assertion about that number, so the run length is the study's, not the
    # pool's.
    slot_constructs = _slots_for(order, ENCOUNTERS_PER_RUN)

    # One entry per encounter, not one per construct: a restricted arm can give
    # the same construct two slots, and a construct-keyed dict cannot hold that.
    assignment: List[dict] = []
    served: Dict[str, List[str]] = {}
    # Slots where `variant` was asked for and the pool had no such form left.
    pin_unfilled: List[dict] = []
    # Slots where `variants` named fewer forms than the construct has slots.
    form_pin_unfilled: List[dict] = []
    # `variants` as one sequence per construct, and how far into each sequence
    # the draw has got. Per SLOT, not per construct: that is the whole change.
    pins = _pin_sequences(variants)
    pin_next: Dict[str, int] = {}
    # Slots whose form the caller pinned rather than leaving to the draw.
    # sibling_run pins every construct to build attempt 2 on the other parallel
    # form; applying the cross-construct exclusion to a pin silently reverted
    # that flip and made attempt 2 repeat attempt 1's encounter, which destroys
    # the pre/post comparison the two forms exist for.
    for construct in slot_constructs:
        options = pool[construct]
        slot_pinned = False
        sid: Optional[str] = None
        if construct in pins:
            # Walk this construct's pinned sequence, one entry per slot, and
            # skip anything this run is already serving. The skip is what makes
            # a scalar pin safe on an arm: it used to be handed to both slots,
            # and the participant got one conversation twice.
            seq = pins[construct]
            served_here = served.get(construct, [])
            i = pin_next.get(construct, 0)
            while i < len(seq) and seq[i] in served_here:
                i += 1
            pin_next[construct] = i + 1 if i < len(seq) else i
            if i < len(seq):
                sid = seq[i]
                slot_pinned = True
            else:
                # The sequence ran out before the slots did. Fall through to the
                # ordinary unseen draw below and SAY SO on the run: a caller who
                # asked for specific forms and silently got a drawn one has no
                # way to tell which slots were theirs. Not marked pinned, so the
                # exclusion pass is free to steer this slot.
                unserved = [o for o in options if o not in served_here]
                sid = rng.choice(unserved or options)
                form_pin_unfilled.append({
                    "construct": construct,
                    "requested_forms": list(seq),
                    "served": sid,
                    "reason": ("the run gives this construct more slots than "
                               "the caller named forms for; an unserved form "
                               "was drawn rather than a repeat"),
                })
        elif variant_pin:
            # Pin every construct to one form. Useful for piloting a single set
            # rather than a random mix across participants.
            #
            # `slot_pinned` records that the caller's choice was HONOURED in
            # THIS slot, which is the only thing _apply_form_exclusions may
            # safely treat as a deliberate decision. Marking a slot pinned and
            # then drawing at random anyway (which is what a letter no form of
            # this construct carries used to do) turns the exclusion off while
            # the run document says a human asked for the form — a claim nobody
            # made.
            #
            # Without replacement, exactly as the random branch below is. This
            # took wanted[0] for every slot, and a restricted arm gives each of
            # its two constructs TWO slots — so `?variant=A` on an arm link
            # served S1 A, S2 A, S1 A, S2 A: the same two conversations twice.
            # A participant handed the same encounter again notices immediately,
            # and the second copy is worthless as data whoever asked for it.
            # Measured over 200 seeds: 200 runs in 200, on both arms, for both
            # letters. Where the pin cannot fill a second slot the run serves the
            # construct's OTHER form rather than a repeat — an unseen encounter
            # is worth more than an honoured pin, and the slot is not counted as
            # pinned, so the exclusion pass is free to steer it.
            served_here = served.get(construct, [])
            wanted = [o for o in options
                      if load_spec(o)["variant"].upper() == variant_pin
                      and o not in served_here]
            if wanted:
                slot_pinned = True
                sid = wanted[0]
            else:
                unserved = [o for o in options if o not in served_here]
                sid = rng.choice(unserved or options)
                # Recorded, not just done. An operator piloting form A on an arm
                # link gets two A encounters and two B ones, and a pilot that
                # silently ran half the other set is a pilot whose result means
                # nothing. This is the one field that says so; see
                # construct_pool["variant_pin_unfilled"].
                pin_unfilled.append({
                    "construct": construct,
                    "requested_variant": variant_pin,
                    "served": sid,
                    "reason": ("the arm gives this construct more slots than it "
                               "has forms carrying the requested letter; the "
                               "unserved form was served rather than a repeat"),
                })
        else:
            # Draw without replacement within a construct. On the unrestricted
            # run every construct is drawn once and this is exactly the old
            # `rng.choice(options)` over the same list, so a given seed still
            # produces the run it always did. On an arm that fills two slots
            # from one construct it is what stops the participant being handed
            # the same conversation twice.
            unserved = [o for o in options if o not in served.get(construct, [])]
            sid = rng.choice(unserved or options)
        served.setdefault(construct, []).append(sid)
        assignment.append({"construct": construct, "id": sid,
                           "pinned": slot_pinned})

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
    exclusions = _apply_form_exclusions(assignment)

    scenarios = []
    # Rebuilt from the slots rather than carried alongside them, so a form the
    # exclusion pass replaced cannot survive here as the one that was drawn.
    chosen: Dict[str, str] = {}
    for slot in assignment:
        sid = slot["id"]
        spec = load_spec(sid)
        # `variants` is one form per construct and predates a run being able to
        # serve a construct twice. It keeps the first, and `scenarios` below is
        # the authority on what was actually served.
        chosen.setdefault(slot["construct"], sid)
        scenarios.append({
            "id": sid,
            "construct": slot["construct"],
            "variant": spec["variant"],
            "title": spec["title"],
            "parallel_form": spec.get("parallel_form"),
        })

    # What the restriction actually cost this run, computed from the run that
    # was built rather than asserted from the arm name: which constructs had to
    # fill more than one encounter, and whether any form had to be served twice
    # (it should not — no arm gives one construct more slots than it has forms —
    # but a repeated conversation has to be visible if it ever happens).
    repeated = sorted({c for c in slot_constructs if slot_constructs.count(c) > 1})
    pool_record["repeated_constructs"] = repeated
    # Recomputed from the finished assignment rather than from the draw's own
    # bookkeeping, because _apply_form_exclusions rewrites slots after the draw:
    # `served` above can still name a form this run no longer carries.
    served_now: Dict[str, List[str]] = {}
    for row in scenarios:
        served_now.setdefault(row["construct"], []).append(row["id"])
    pool_record["repeated_forms"] = sorted(
        {sid for sids in served_now.values() for sid in sids if sids.count(sid) > 1}
    )
    # WHICH FORMS THIS RUN LEFT UNSEEN, PER CONSTRUCT IT SERVED.
    #
    # COUNTED FROM WHAT WAS SERVED, NOT FROM SLOT COUNTS. This was scoped to
    # `repeated` — the constructs an arm gave two slots — because while every
    # construct carried exactly two forms, "this construct filled two slots" and
    # "this construct has no form left" were the same statement. Neither half of
    # that survives a third form:
    #
    #   - An arm serves two of a three-form construct, so two are spent and one
    #     is in reserve. A run reporting "both parallel forms spent" tells the
    #     researcher no parallel-form retest is possible for that construct when
    #     one is, which is worse than saying nothing.
    #   - An UNRESTRICTED run gives every construct one slot, so `repeated` is
    #     empty, so this answered {} on every full run in the study while the
    #     bank held five unseen forms for it. That emptiness is copied onto the
    #     second attempt as attempt1_forms_in_reserve, so the pair of runs said
    #     "nothing was held back" about the one arm where the most was.
    #
    # So: every construct with a slot in this run, and every form of it this run
    # did not serve. A form the run's own composition forbids is not in reserve —
    # it could not be served to this participant on a second attempt either,
    # because sibling_run carries the arm over and _apply_form_exclusions would
    # correct it straight back out again.
    bank = _by_construct()
    present_constructs = {row["construct"] for row in scenarios}
    barred: Dict[str, set] = {}
    for _construct, _variant, _requires in FORM_EXCLUSIONS:
        if _requires in present_constructs:
            barred.setdefault(_construct, set()).add(_variant.upper())
    reserve: Dict[str, List[str]] = {}
    for construct in sorted(served_now):
        left = [
            form for form in (bank.get(construct) or [])
            if form not in served_now.get(construct, [])
            and load_spec(form)["variant"].upper() not in barred.get(construct, set())
        ]
        if left:
            reserve[construct] = sorted(left)
    pool_record["forms_in_reserve"] = reserve
    # TRUE means this run has emptied the shelf: no construct it served has a
    # form left that it would be allowed to serve, so no second attempt of any
    # shape can be a parallel-form retest. Derived from `forms_in_reserve`
    # above, which is counted from the forms actually served — it used to be
    # `bool(repeated) and not reserve`, which asked a slot-count question
    # ("did an arm double up?") in place of the stock question it claims to
    # answer. forms_in_reserve is the per-construct truth the single bool
    # cannot carry, and is the field to read when this one is False.
    pool_record["parallel_forms_spent"] = not reserve
    # What a `?variant=` pin actually got, in the same place. Null on every run
    # nobody pinned. On an arm link the two are in direct conflict — the arm
    # gives each of its two constructs two of the four slots, and a construct
    # has exactly one form carrying a given letter — so the letter can fill half
    # the run and no more. The run says which half.
    pool_record["variant_pin"] = variant_pin
    pool_record["variant_pin_unfilled"] = pin_unfilled
    # "caller" (a URL or a second attempt asked for this letter),
    # "default_run_variant" (nobody asked; DEFAULT_RUN_VARIANT supplied it), or
    # None (no pin at all). See the block in create() above.
    pool_record["variant_pin_source"] = variant_pin_source if variant_pin else None
    # The same accounting for a `variants=` pin, which names forms rather than a
    # letter. Empty on every run nobody pinned and on every pin that covered its
    # construct's slots. Non-empty means some slot the caller meant to choose
    # was drawn instead, which a caller cannot otherwise tell from the run.
    pool_record["pinned_forms_unfilled"] = form_pin_unfilled

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
        # Which constructs this run was allowed to draw from, under which arm,
        # and what the arm left out. On the ordinary study run this says "full"
        # and lists all four; on a run started from one of the arm links it is
        # the only record of which arm the participant was in, and inferring
        # that from the scenario list would stop working the moment a form is
        # added. Recorded for the same reason form_exclusions is: an assignment
        # that was steered has to look steered in the data.
        "construct_pool": pool_record,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(_path(run["run_id"]), json.dumps(run, indent=2))
    return run


def run_arm(run: dict) -> str:
    """Which arm's link built this run. "full" for the unrestricted study run.

    Read off `construct_pool` rather than inferred from the scenario list, for
    the reason arm_constructs records it there: inferring stops working the
    moment a form is added.
    """
    return str((run.get("construct_pool") or {}).get("arm") or "full")


def find_for_participant(participant_id: str,
                         arm: Optional[str] = None) -> Optional[dict]:
    """The participant's existing run, if any. With `arm`, in that arm only.

    Participants close tabs, lose connection, and come back. Handing them a
    fresh run would restart the sequence and produce a second partial record
    under the same key, so a returning participant resumes where they were.

    `arm` is not a filter for tidiness. Without it this answered with the
    participant's NEWEST run whatever arm it belonged to, so a person who had
    finished the 1:1 arm and then touched the group link could no longer reach
    their own finished run from the 1:1 link they were given: the group run came
    back, failed the caller's arm comparison, and the caller built a THIRD run —
    done:false, partial code, four encounters they had already done. A
    same-arm return was being mistaken for a cross-arm arrival. Asking the
    question per arm is what tells the two apart.
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
        if arm is not None and run_arm(run) != arm:
            continue
        if best is None or run.get("created_at", 0) > best.get("created_at", 0):
            best = run
    return best


def participant_withdrawal(participant_id: Optional[str]) -> Optional[dict]:
    """The withdrawal this PERSON recorded, on whichever run carries it.

    A withdrawal is a statement about the participant, not about one run
    document, and asking a single run is how it stopped being one: a person who
    stopped on the 1:1 arm and then touched the group link was handed a fresh
    run with withdrawn null and four encounters queued, so /api/runs showed the
    same key withdrawn and live at once and a withdrawal report read "withdrew
    and then carried on".

    The earliest stamp wins, because that is when they actually stopped.

    This is a POPULATE path, not the gate: its callers are minting a record or
    building an entry redirect either way, so an unreadable file answers None
    here and the gate below is where that becomes a refusal.
    """
    if not participant_id or not RUNS_DIR.exists():
        return None
    best = None
    for f in RUNS_DIR.glob("*.json"):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if run.get("participant_id") != participant_id:
            continue
        w = run.get("withdrawn")
        if w and (best is None or (w.get("at") or 0) < (best.get("at") or 0)):
            best = w
    return best


def withdrawal_for_record(record_id: Optional[str]) -> Optional[dict]:
    """The withdrawal binding this participant *record*, or None.

    The backstop behind storage.participant_withdrawal, which reads the stop off
    the record itself. This one is what finds a withdrawal recorded before the
    record carried one, or recorded on another run of the same person, and the
    caller writes what it finds back onto the record so the question is answered
    from one file next time.

    One directory pass, not two. This runs on every socket open, which is the
    same budget find_by_participant_record already spends, and a gate that
    honours the consent text's promise should not be the reason a socket is
    slow to refuse.

    FAILS CLOSED, narrowly. Skipping a file it could not parse and then
    answering None said "nobody withdrew" on the strength of evidence it had not
    read — so a single corrupt or half-written run file turned a withdrawal back
    into consent and the capture socket opened. It raises RunScanIncomplete when
    it could not identify ANY run belonging to this person AND something in the
    directory was unreadable, because then the unreadable file may be the run
    that carries the stop. It does NOT raise once a run of theirs has been read:
    an unrelated corrupt file is somebody else's problem, and a gate that
    refuses the whole wave over one bad file in the directory has stopped being
    a withdrawal gate.

    "A run of theirs" is asked three ways, and the second and third are the fix
    for a refusal that hit live participants. Asking only "which run names this
    record" answered "I cannot tell" for every record whose run does not point
    back at it — the documented state where /start's mint succeeded and its
    write-back onto the run did not — so ONE stray half-written file anywhere in
    the directory refused them: capture, uploads and their encounter, with the
    only remedy being a hand repair nobody would know to make. The record itself
    knows the run it was minted for and the participant key it was minted under,
    and either one identifies the person well enough to answer.
    """
    if not record_id or not RUNS_DIR.exists():
        return None
    owner: Optional[dict] = None
    unreadable: List[str] = []
    # participant key -> earliest withdrawal seen under it
    stopped: Dict[str, dict] = {}
    # run id -> run, so the record's own run_id can be resolved without a second
    # pass over the directory.
    by_run_id: Dict[str, dict] = {}
    keys_seen: set = set()
    for f in RUNS_DIR.glob("*.json"):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            unreadable.append(f.name)
            continue
        if run.get("participant_record_id") == record_id:
            if owner is None or run.get("created_at", 0) > owner.get("created_at", 0):
                owner = run
        rid = run.get("run_id")
        if rid:
            by_run_id[str(rid)] = run
        w = run.get("withdrawn")
        key = run.get("participant_id")
        if key:
            keys_seen.add(key)
        if w and key:
            prev = stopped.get(key)
            if prev is None or (w.get("at") or 0) < (prev.get("at") or 0):
                stopped[key] = w
    if owner is None:
        # Ask the record before refusing the person behind it.
        rec = _record_or_none(record_id)
        if rec:
            owner = by_run_id.get(str(rec.get("run_id") or ""))
            code = rec.get("code")
            if owner is None and code and code in keys_seen:
                # No run names this record and the run it was minted for is not
                # on the record either, but a run under their participant key
                # was read — which is the same person, and is what the stop is
                # recorded against.
                return stopped.get(code)
    if owner is None:
        if unreadable:
            raise RunScanIncomplete(
                f"could not resolve the run for participant record {record_id}: "
                f"{len(unreadable)} run file(s) would not parse "
                f"({', '.join(sorted(unreadable)[:5])})")
        return None
    return owner.get("withdrawn") or stopped.get(owner.get("participant_id"))


def _record_or_none(record_id: str) -> Optional[dict]:
    """The participant record, or None if it cannot be read.

    Imported inside the function for the reason storage._withdrawal_for_code
    imports this module inside one: the two modules need each other and a
    module-scope import both ways is a cycle. An unreadable record answers None,
    which leaves withdrawal_for_record on its fail-closed path rather than
    letting a storage error read as "nobody withdrew".
    """
    try:
        from . import storage

        return storage.get_participant(record_id)
    except Exception:  # noqa: BLE001, the caller's fail-closed rule takes over
        return None


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


#: What a Qualtrics ResponseID looks like. Deliberately loose about the prefix
#: (a panel, a preview and a test survey spell it differently) and strict about
#: the alphabet, so nothing carrying a path separator, a space or a percent
#: escape can become the value two arrivals are merged on.
_SURVEY_RESPONSE_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")


def is_joinable_survey_response(qid: Optional[str]) -> bool:
    """Whether this ?qid= names ONE survey response and may be joined on.

    Qualtrics mints a ResponseID per response, so a real one identifies exactly
    one person's pass through the survey. An unreplaced ${e://Field/ResponseID}
    identifies nobody and is THE SAME STRING FOR EVERYBODY — the precise shape
    of the collision the unattributed path exists to prevent, in which arrival
    two lands inside arrival one's half-finished run. So the template text is no
    more a response id here than it is a participant key, and any caller about
    to treat two arrivals as one person has to ask this first.
    """
    q = (qid or "").strip()
    if not q or "${" in q or "e://" in q or "}" in q:
        return False
    return bool(_SURVEY_RESPONSE_RE.fullmatch(q))


def find_unattributed_for_survey_response(
        qid: Optional[str], arm: Optional[str] = None) -> Optional[dict]:
    """The run an earlier arrival already created for this survey response when
    its participant key did not pipe, if any. With `arm`, in that arm only.

    The resume path for the one participant who has no key to resume on. A
    broken Qualtrics pipe costs them their participant key, and the key is what
    find_for_participant matches, so without this every fresh arrival of theirs
    builds another run: opening the link twice, a double-click, or a
    back-button and a second press, and one person becomes two rows, two
    participant records, two half-finished sequences and two partial completion
    codes. That was survivable while such an arrival was a rare piping fault. It
    stops being survivable the moment every unusable-key arrival is routed
    through a button that can be pressed twice.

    Matched on the SURVEY RESPONSE, which is the only identity these arrivals
    still carry: one Qualtrics ResponseID is one person's pass through the
    survey, so two arrivals bearing the same real one are the same person coming
    back. is_joinable_survey_response is what keeps that true, because an
    unreplaced ${e://Field/ResponseID} is one string shared by everybody and
    merging on it would rebuild the exact collision this path exists to prevent.

    Only ever matches a run that is ALREADY unattributed. A run whose key piped
    belongs to that participant and is resumable from their key; it must not
    become reachable by presenting their survey id beside a broken key.
    """
    if not is_joinable_survey_response(qid) or not RUNS_DIR.exists():
        return None
    wanted = (qid or "").strip()
    best = None
    for f in RUNS_DIR.glob("*.json"):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if str(run.get("qualtrics_id") or "").strip() != wanted:
            continue
        if run.get("participant_key_status") == "ok":
            continue
        if arm is not None and run_arm(run) != arm:
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

    Stamped on every run the participant key has, not only the one they pressed
    stop in. Someone who withdrew on the 1:1 arm and then touched the group link
    was handed a run that said withdrawn:null, and the export then showed one
    person withdrawn and live at the same time — a withdrawal report that reads
    "withdrew and then carried on" is worse than no report. The person stopped;
    every run of theirs has to say so.

    And stamped on every participant RECORD those runs name, which is the half
    that was missing and the reason eight routes stayed open. The run documents
    are the analyst's copy — which run, which encounter — but nothing except the
    capture gate ever reads them, and a record id is what a participant actually
    carries. Writing both here is what lets every reader ask one file and get
    the same answer. See the "Withdrawal" section of server/storage.py.
    """
    run = get(run_id)
    if run is None:
        return None
    stamp = run.get("withdrawn")
    if not stamp:
        stamp = {
            "at": time.time(),
            "reason": reason or "participant_withdrew",
            # Which encounter they were in when they stopped, and how far the
            # run had got, so a partial record is interpretable later.
            "session_id": session_id or None,
            "index": run.get("index", 0),
            "completed": len(run.get("completed", [])),
        }
        run["withdrawn"] = stamp
        save(run)

    pkey = run.get("participant_id")
    touched = [run]
    if pkey:
        for other in _others_for_participant(pkey, run["run_id"]):
            touched.append(other)
            if other.get("withdrawn"):
                continue
            # The moment and the reason are the person's and carry over; where
            # they had got to is this run's own, or the copy would claim they
            # stopped at an encounter of a different run.
            other["withdrawn"] = {
                "at": stamp["at"],
                "reason": stamp["reason"],
                "session_id": stamp.get("session_id"),
                "index": other.get("index", 0),
                "completed": len(other.get("completed", [])),
                # Named so an analyst reading this run can find the one they
                # actually pressed stop in.
                "withdrawn_on_run": run["run_id"],
            }
            try:
                save(other)
            except Exception as e:  # noqa: BLE001, one unwritable run must not
                # leave the others enrolled. The entry path checks the person's
                # withdrawal rather than this field alone, so a gap here costs
                # the export a row, not the participant their stop.
                print(f"  WARNING: could not carry the withdrawal of run "
                      f"{run['run_id']} onto run {other.get('run_id')}: "
                      f"{type(e).__name__}: {e}")

    # Onto the person, not just their runs. Every record any of these runs names
    # gets the stop, so the question "did this participant withdraw?" is a field
    # on the one file a participant id resolves to rather than a directory scan
    # each caller has to remember to run.
    for other in touched:
        pid_record = other.get("participant_record_id")
        if not pid_record:
            continue
        try:
            record_withdrawal(pid_record, {
                "at": stamp["at"],
                "reason": stamp["reason"],
                "session_id": stamp.get("session_id"),
                "withdrawn_on_run": run["run_id"],
            })
        except Exception as e:  # noqa: BLE001, one unwritable record must not
            # leave the others enrolled. Loud, because the capture gate falls
            # back to the run scan when the record says nothing, and an operator
            # should not have to discover that from a latency.
            print(f"  WARNING: could not write the withdrawal of run "
                  f"{run['run_id']} onto participant record {pid_record}: "
                  f"{type(e).__name__}: {e}")
    return run


def _others_for_participant(participant_id: str, except_run_id: str) -> List[dict]:
    """Every other run under this participant key."""
    out: List[dict] = []
    if not RUNS_DIR.exists():
        return out
    for f in RUNS_DIR.glob("*.json"):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if run.get("participant_id") != participant_id:
            continue
        if run.get("run_id") == except_run_id:
            continue
        out.append(run)
    return out


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
        # Which entry link built this run. The page shows it nowhere, but it is
        # what lets an operator (or a test) confirm that the link they pasted
        # into Qualtrics is the arm the participant actually got, without
        # opening the run file on the server.
        "arm": (run.get("construct_pool") or {}).get("arm", "full"),
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

    THE FLIP TARGET IS CHOSEN HERE, NOT READ OFF `parallel_form`.

    While every construct had two forms, `spec["parallel_form"]` named the only
    other one and reading it was the whole of the decision. Two things were
    wrong with that and a third form makes both of them bite.

    The first is a pairing the instrument forbids. A full run always contains
    Teamwork, so S1 A is excluded from it, so attempt 1 served S1 B — and S1 B's
    `parallel_form` is S1 A. The flip is passed to create() as a PIN, and
    _apply_form_exclusions leaves a pinned slot alone by design, so attempt 2
    was served S1 A alongside Teamwork: measured 120 of 120 sibling runs before
    this. The docstring here claimed the construct fell back to attempt 1's own
    form; it did not, it took the barred one. Both of those are worse than the
    A/B delta they were weighed against, and both resolve now that a third form
    exists, which is what this function was told to wait for.

    The second is arithmetic. `parallel_form` is a scalar, and a scalar that
    names one of three reads as naming the only one. The target is therefore
    computed from the construct's whole bank (scenarios_v3.parallel_forms is
    the authority): every OTHER form of it, minus any form this run's
    composition bars, in an order fixed by attempt 1's run id so a given
    attempt 1 always yields the same attempt 2. `parallel_form` no longer
    survives even as the fallback: a construct with nothing else to offer
    repeats attempt 1's own form, visibly, rather than taking the scalar — see
    the comment at that branch for why the scalar was a barred form waiting to
    be served.

    STABLE MEANS STABLE, ORDER INCLUDED. That property covers the whole run now,
    not only which forms it serves: create() is called with a seed derived from
    attempt 1's run id, so two calls for the same participant produce the same
    four encounters in the same order. Before that, `rng.shuffle(order)` drew
    from system entropy and eight calls on one attempt-1 run gave one form set
    and two orderings. Two calls still MINT TWO RUN DOCUMENTS — this function
    creates, it does not find-or-create — so a caller that wants one attempt 2
    per participant must check for one first; nothing calls this yet, which is
    why that is a note rather than a change.

    Nothing calls this yet.

    The cohort carries over: a second attempt built from an internal or
    unattributed run is not study data either, and defaulting it to "study"
    would put test traffic into the dataset at exactly the point where nobody is
    watching.

    THE RESERVE IS NOW REACHED, BY PINNING PER SLOT.

    The arm carries over, and an arm gives each of its two constructs two of the
    four slots (see _slots_for). A pin used to be ONE form per construct, so
    pinning a repeated construct handed both of its slots the same id — one
    conversation twice — and the only way out was to pin nothing at all and
    re-draw the whole run at random. That re-draw is why the reserve was out of
    reach: it landed on attempt 1's unseen form by luck in 38 of 60 sibling runs
    on the 1:1 arm and 34 of 60 on the group arm, and nothing on either run said
    whether it had. A construct's third form existed and no participant could be
    reliably given it.

    create() now takes a SEQUENCE per construct, one entry per slot, so this
    function can say what it actually means: serve the forms this participant
    has not met FIRST, in sorted order, then — for the slots left over, because
    an arm has more slots than a construct has unseen forms — the forms they
    have met, most recent first, so the repeat is the one furthest from the end
    of attempt 1. Both halves are still filtered through the run's own
    exclusions, so a per-slot pin cannot walk a barred form in through a side
    door: the pass leaves pinned slots alone by design, which is exactly why
    nothing barred may be pinned here.

    WHAT THE SIBLING THEN RECORDS, counted from the two sequences rather than
    asserted from the arm name:
      - attempt2_unseen_forms: per construct, the forms attempt 2 serves that
        attempt 1 did not. This is the reserve being reached, named.
      - attempt2_repeated_forms: per construct, the ones it had to repeat.
      - attempt2_forms_available: TRUE only when EVERY slot of attempt 2 is a
        form attempt 1 did not serve — i.e. the whole second attempt is a
        parallel-form retest. A full run clears that; a restricted arm does not
        and should not claim to, because a construct with three forms and two
        slots has one unseen form for two slots. Partial reach is real and is
        reported as the two maps above, not by loosening this bool: an analyst
        reading True must be able to take the whole run as unseen.
    """
    run = get(run_id)
    if run is None:
        return None
    prior = run.get("construct_pool") or {}
    # What attempt 1 actually served, per construct, in slot order. Read off
    # `scenarios` and not `variants`: `variants` keeps one form per construct
    # (it predates a run serving a construct twice), so on an arm run it names
    # half of what the participant met and the other half would be "unseen".
    seen_by_construct: Dict[str, List[str]] = {}
    for row in run.get("scenarios") or []:
        seen_by_construct.setdefault(row["construct"], []).append(row["id"])
    bank = _by_construct()
    present = set(seen_by_construct)
    barred: Dict[str, set] = {}
    for _c, _v, _requires in FORM_EXCLUSIONS:
        if _requires in present:
            barred.setdefault(_c, set()).add(_v.upper())
    flipped: Dict[str, List[str]] = {}
    for construct, seen in seen_by_construct.items():
        permitted = [
            form for form in (bank.get(construct) or [])
            if load_spec(form)["variant"].upper() not in barred.get(construct, set())
        ]
        unseen = sorted(f for f in permitted if f not in seen)
        # ROTATED, NOT SORTED, when there are more unseen forms than slots.
        #
        # Taking the sorted first was the old rule and it is a counterbalancing
        # fault as soon as a construct has three forms and a slot has one. On
        # the unrestricted arm, where every construct gets exactly one slot,
        # attempt 1 served Inspirational Leadership S3A/S3B/S3C 66/83/51 over
        # 200 seeds and attempt 2 served 134/66/0: a third form that no second
        # attempt could ever reach, because whichever form attempt 1 drew, the
        # alphabetically first of the other two won. S3C was written to be
        # measured, and a form served to nobody is not.
        #
        # The rotation is keyed on attempt 1's run id, so it is even across
        # participants and fixed for any one of them: the documented property
        # is that a GIVEN ATTEMPT 1 always yields the same attempt 2, and a run
        # id is exactly as stable as the run. (A seed cannot serve here —
        # sibling_run does not take one, and attempt 2's construct order has
        # always been drawn unseeded.)
        if len(unseen) > 1:
            turn = int(hashlib.sha256(
                f"{run_id}:{construct}".encode()).hexdigest()[:8], 16)
            turn %= len(unseen)
            unseen = unseen[turn:] + unseen[:turn]
        # Reverse so a construct with more slots than unseen forms repeats the
        # one the participant met EARLIEST in attempt 1 last, rather than
        # replaying the encounter they have most recently finished.
        repeats = [f for f in reversed(seen) if f in permitted]
        seq = unseen + repeats
        if not seq:
            # Every form of this construct is barred in this composition. The
            # last resort is attempt 1's OWN form, repeated and visible as a
            # repeat, not the `parallel_form` scalar.
            #
            # This branch used to read `load_spec(first).get("parallel_form")`,
            # and that would have walked a BARRED form back in through the one
            # door the exclusion pass leaves open. S1B's scalar is S1A, S1A is
            # the form this branch exists because it is barred, a pinned slot is
            # left alone by _apply_form_exclusions by design — so the fallback
            # would have reinstated the exact 120-of-120 defect named at the top
            # of this docstring, in the one case nobody would look at. It cannot
            # fire today (one exclusion row and three forms per construct means
            # `permitted` is never empty), and FORM_EXCLUSIONS' own comment says
            # the next exclusion is meant to be a line of data, so it must not
            # be a trap waiting for that line.
            first = seen[0]
            seq = [first]
        flipped[construct] = seq
    sibling = create(
        participant_id or run.get("participant_id"),
        variants=flipped,
        cohort=run.get("cohort", "study"),
        key_status=run.get("participant_key_status"),
        raw_participant_key=run.get("raw_participant_key"),
        arm=prior.get("arm"),
        constructs=prior.get("requested_constructs"),
        # SEEDED ON ATTEMPT 1'S RUN ID, so the whole of attempt 2 is a function
        # of attempt 1 and not of system entropy. The docstring's headline claim
        # is that a given attempt 1 always yields the same attempt 2; without a
        # seed that was true of the FORMS only, because create() shuffles
        # construct order from an unseeded Random. Measured: eight calls on one
        # attempt-1 run gave one form set and two different orderings. The
        # counterbalancing is unharmed — the seed varies with the run id, so it
        # still varies across participants — and it is now reproducible for any
        # one of them, which is what an analyst rebuilding an assignment needs.
        seed=int(hashlib.sha256(f"sibling:{run['run_id']}".encode())
                 .hexdigest()[:8], 16),
    )
    # Counted from the two sequences that exist, not from the arm's slot
    # arithmetic. `not repeated` answered this before: it said False on every
    # restricted arm because a pin could not reach a second slot, which was a
    # statement about create()'s signature rather than about the run.
    now_by_construct: Dict[str, List[str]] = {}
    for row in sibling.get("scenarios") or []:
        now_by_construct.setdefault(row["construct"], []).append(row["id"])
    unseen_now: Dict[str, List[str]] = {}
    repeated_now: Dict[str, List[str]] = {}
    for construct, ids_now in now_by_construct.items():
        before = set(seen_by_construct.get(construct) or [])
        fresh = sorted({sid for sid in ids_now if sid not in before})
        again = sorted({sid for sid in ids_now if sid in before})
        if fresh:
            unseen_now[construct] = fresh
        if again:
            repeated_now[construct] = again
    pool = sibling.setdefault("construct_pool", {})
    # TRUE only when the whole second attempt is unseen. See the docstring: an
    # arm run reaching one reserve form out of four slots is real and is
    # reported as attempt2_unseen_forms, not by weakening this claim.
    pool["attempt2_forms_available"] = not repeated_now
    pool["attempt2_unseen_forms"] = unseen_now
    pool["attempt2_repeated_forms"] = repeated_now
    pool["attempt1_run_id"] = run["run_id"]
    # What attempt 1 left on the shelf, carried onto attempt 2 so the pair of
    # runs answers "was there an unseen form?" without the analyst having to
    # re-open attempt 1. It is deliberately NOT the same claim as
    # attempt2_forms_available: attempt 2 can reach part of a reserve and still
    # not be a whole parallel-form retest, which is precisely the restricted
    # arm's situation.
    reserve_before = prior.get("forms_in_reserve") or {}
    pool["attempt1_forms_in_reserve"] = reserve_before
    # The direct answer to "did the second attempt reach what the first held
    # back", in ids. Empty when attempt 1 held nothing back.
    held = {sid for forms in reserve_before.values() for sid in forms}
    pool["attempt1_reserve_served"] = sorted(
        held & {row["id"] for row in (sibling.get("scenarios") or [])})
    save(sibling)
    return sibling
