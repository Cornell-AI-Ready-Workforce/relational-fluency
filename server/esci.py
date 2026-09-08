"""The ESCI Construct 4 item bank, as code.

Phase 2 turns recorded encounters into gold labels: 2-3 independent raters score
each encounter on all 22 ESCI Relationship Management items and reliability is
computed before anything is modelled. Everything in that chain, the rating
console, the packet builder, the submission path, the reliability report and the
Qualtrics ingest, needs the same answer to "what are the items, what order are
they in, which of them are reverse-scored". Until now the only answers were a CSV
nobody parsed and a Markdown table nobody could import, so each consumer would
have grown its own copy. This module is the single answer.

The source of truth is studies/study1/qualtrics/esci_construct4_items.csv, read
at import. The CSV stays authoritative rather than being inlined here because it
is also what gets imported into Qualtrics: two copies of an instrument drift, and
an instrument that drifts silently invalidates the labels collected under the old
one.

**Proprietary instrument.** The items are the ESCI item bank (Boyatzis, Goleman &
Korn Ferry), reproduced for research reference only; licensing must be confirmed
before fielding. That warning travels with the items: `item_bank()` is the way to
hand the bank to a rater console or an export, and it carries `NOTICE` by
construction so the warning cannot be dropped by a caller who forgot it.

Reverse scoring is the reason this module is worth having on its own. Three items
(11 "Does not cooperate with others", 15 "Allows conflict to fester", 24 "Does not
inspire followers") are worded against the construct, so a raw 1 means the
participant is *good* at it. Reverse-code with `score_value`; getting it backwards
inverts three of the 22 items in every ICC, every competency mean and every
downstream model, and nothing in the numbers looks wrong when it happens. The
instrument deliberately keeps those three worded negatively as straight-lining
checks (rating-instrument.md, administration note 2), so they are not going away.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).parent.parent
ITEMS_CSV = ROOT / "studies" / "study1" / "qualtrics" / "esci_construct4_items.csv"

# Reproduced verbatim in spirit from the header of
# studies/study1/qualtrics/rating-instrument.md. Anything that shows an item to a
# human or writes one to a file shows this next to it.
NOTICE = (
    "ESCI items (Boyatzis, Goleman & Korn Ferry) are a proprietary instrument, "
    "reproduced here for research reference only. Confirm licensing/permission "
    "before fielding."
)

CONSTRUCTS = [
    "conflict_management",
    "influence",
    "inspirational_leadership",
    "teamwork",
]

CONSTRUCT_LABELS = {
    "conflict_management": "Conflict management",
    "influence": "Influence",
    "inspirational_leadership": "Inspirational leadership",
    "teamwork": "Teamwork",
}

SCALE_MIN = 1
SCALE_MAX = 5

# "Not enough information to judge" is stored as null, never as a number. The
# instrument requires the option (a single conversation cannot exhibit every
# behaviour; items 3 and 49 are expected to be N/A most of the time in S2's
# dyadic setting) and administration note 3 excludes N/A pairwise, so it has to
# stay distinguishable from a low score all the way through. Encoding it as 0,
# or as 3, or as the scale midpoint would quietly turn "the rater could not tell"
# into evidence, which is the single easiest way to fake agreement.
NA = None

# The 1-5 anchors, from the instrument. Kept here so the console and any export
# label the scale the same way; a rater console that invented its own wording
# would be administering a different instrument from the Qualtrics route.
SCALE_LABELS = {
    1: "Never",
    2: "Rarely",
    3: "Sometimes",
    4: "Often",
    5: "Consistently",
}
NA_LABEL = "Not enough information to judge"


# --- The crosswalk to the scenario specs' ESCI slugs -------------------------
#
# The v3 scenario specs tag every planted trigger with slugs, not item numbers:
#
#     - id: t3_defensive_spike
#       esci: [de_escalate, talk_openly]
#
# and each spec declares its own slug -> text map in an `esci_items:` block. That
# is the only place the correspondence between a trigger tag and a rating item
# has ever existed, and it existed as English text, per file, eight times over.
# Anything that wants to say "this encounter planted three beats that bear on
# item 26" has had to guess. Written down once, here.
#
# HONESTY ABOUT COVERAGE. All 22 slugs used across scenarios/v3/*.yaml map to an
# item, and every item has exactly one slug: there is nothing unmapped. That is
# not luck, it is checkable, and it is checked, because the declared text in each
# spec's `esci_items:` block is character-for-character the item text in the CSV
# (reverse items add a trailing " (R)"); tests/test_esci.py re-derives this whole
# table from the YAML and fails if a spec ever introduces a slug this table does
# not know, or moves one onto different text.
#
# Two limits worth naming rather than discovering later:
#
#   * The older specs under reddit-analysis/scenarios/ do not use slugs at all;
#     they carry `focal_esci_items: 8, 14, 15 (R), 26, 46`, bare item numbers.
#     Those parse through `item()`, which accepts a plain number, so no second
#     crosswalk is needed for them.
#   * If the Construct 3 (empathy / organizational awareness) block that
#     rating-instrument.md floats is ever added to the CSV, those items will have
#     no slug until a scenario plants a trigger for them. `slug_for` returns None
#     there rather than pretending.
SLUG_TO_ITEM = {
    # Conflict management
    "resolve_not_fester": "ESCI-08",
    "de_escalate": "ESCI-14",
    "fester_r": "ESCI-15",
    "talk_openly": "ESCI-26",
    "bring_into_open": "ESCI-46",
    # Influence
    "key_people": "ESCI-03",
    "multiple_approaches": "ESCI-17",
    "self_interest": "ESCI-20",
    "anticipates": "ESCI-38",
    "behind_scenes": "ESCI-49",
    "through_discussion": "ESCI-68",
    # Inspirational leadership
    "builds_pride": "ESCI-05",
    "inspires": "ESCI-07",
    "not_inspire_r": "ESCI-24",
    "brings_out_best": "ESCI-27",
    "compelling_vision": "ESCI-61",
    # Teamwork
    "not_cooperate_r": "ESCI-11",
    "supportive": "ESCI-12",
    "encourages_cooperation": "ESCI-25",
    "solicits_input": "ESCI-33",
    "respectful": "ESCI-37",
    "encourages_participation": "ESCI-56",
}

ITEM_TO_SLUG = {v: k for k, v in SLUG_TO_ITEM.items()}


# --- Loading -----------------------------------------------------------------

def canonical_id(number: int) -> str:
    """Canonical id for an item number: 8 -> "ESCI-08", 68 -> "ESCI-68".

    Zero-padded to two digits so ids sort the way the numbers do in any context
    that sorts them as strings (a CSV column header, a Qualtrics question id, a
    JSON object printed by a researcher). The bank's own order is the CSV's, not
    a sort, but exports get sorted by other people's tools.
    """
    return f"ESCI-{int(number):02d}"


def _truthy(raw: str, field: str, line: int) -> bool:
    v = (raw or "").strip().lower()
    if v in ("true", "t", "yes", "y", "1"):
        return True
    if v in ("false", "f", "no", "n", "0", ""):
        return False
    raise RuntimeError(
        f"{ITEMS_CSV.name} line {line}: {field}={raw!r} is not a boolean"
    )


def _load(path: Path) -> List[Dict[str, Any]]:
    """Parse the item bank, refusing anything ambiguous.

    Every failure here raises rather than degrading, and it raises at import.
    A half-loaded item bank is worse than no item bank: raters would be shown a
    short instrument, `validate` would accept a submission missing the items that
    failed to load, and the resulting labels would look like every other label in
    the dataset. Better to fail on the way up, loudly, next to the file that
    caused it.
    """
    if not path.exists():
        raise RuntimeError(
            f"ESCI item bank not found at {path}. It is the source of truth for "
            "the Phase 2 rating instrument and the server cannot serve ratings "
            "without it."
        )
    items: List[Dict[str, Any]] = []
    seen: Dict[int, int] = {}
    # newline="" per the csv docs; the file is CRLF and Python's own dialect
    # sniffing is the only thing that should be interpreting those bytes.
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"item_no", "competency", "item_text", "reverse_scored"} - set(
            reader.fieldnames or []
        )
        if missing:
            raise RuntimeError(
                f"{path.name} is missing column(s) {sorted(missing)}; "
                f"found {reader.fieldnames}"
            )
        for line, row in enumerate(reader, start=2):
            raw_no = (row.get("item_no") or "").strip()
            if not raw_no:
                continue  # trailing blank line
            try:
                number = int(raw_no)
            except ValueError:
                raise RuntimeError(
                    f"{path.name} line {line}: item_no={raw_no!r} is not a number"
                )
            if number in seen:
                raise RuntimeError(
                    f"{path.name} line {line}: item {number} already defined on "
                    f"line {seen[number]}"
                )
            seen[number] = line
            construct = (row.get("competency") or "").strip()
            if construct not in CONSTRUCTS:
                # Deliberately fatal rather than tolerant. rating-instrument.md
                # contemplates adding a Construct 3 block; when somebody does,
                # CONSTRUCTS, the crosswalk and the reliability report all have
                # to be extended together, and a construct silently appearing in
                # the data is how that gets half-done.
                raise RuntimeError(
                    f"{path.name} line {line}: competency={construct!r} is not "
                    f"one of {CONSTRUCTS}. Adding a construct means extending "
                    "server/esci.py CONSTRUCTS and the reliability report too."
                )
            text = (row.get("item_text") or "").strip()
            if not text:
                raise RuntimeError(f"{path.name} line {line}: item_text is empty")
            items.append({
                "id": canonical_id(number),
                "number": number,
                "text": text,
                "construct": construct,
                "reverse": _truthy(row.get("reverse_scored", ""),
                                   "reverse_scored", line),
            })
    if not items:
        raise RuntimeError(f"{path.name} contains no items")
    return items


# Load once, at import: the bank is a constant of the study, not per-request
# state, and re-reading it mid-wave would let an edit to the CSV change the
# instrument underneath raters who are part-way through a packet.
ITEMS: List[Dict[str, Any]] = _load(ITEMS_CSV)

_BY_ID: Dict[str, Dict[str, Any]] = {it["id"]: it for it in ITEMS}
_BY_NUMBER: Dict[int, Dict[str, Any]] = {it["number"]: it for it in ITEMS}

REVERSE_ITEMS: List[str] = [it["id"] for it in ITEMS if it["reverse"]]


# --- Accessors ---------------------------------------------------------------
#
# Everything hands back copies. The bank is process-wide and long-lived; one
# consumer that appends a "response" key to the dict it was given would corrupt
# the instrument for every later rater in the same process.

def all_items() -> List[Dict[str, Any]]:
    """All items, in the CSV's order.

    The order is stable and it is the instrument's order (grouped by construct,
    ascending item number within a construct). Note that this is the *storage*
    order, not the presentation order: administration note 1 randomises item
    order within a competency group and group order across raters, which is the
    console's job to do per rater, not this module's to bake in.
    """
    return [dict(it) for it in ITEMS]


def items_for(construct: str) -> List[Dict[str, Any]]:
    """The items belonging to one construct, in bank order."""
    if construct not in CONSTRUCTS:
        # A typo'd construct returning [] would render an empty rating block and
        # look like a UI bug three screens away from the cause.
        raise ValueError(
            f"unknown construct {construct!r}; expected one of {CONSTRUCTS}"
        )
    return [dict(it) for it in ITEMS if it["construct"] == construct]


def _resolve(raw: Any) -> Optional[Dict[str, Any]]:
    """Find an item from whatever form of its id a caller happens to hold.

    Canonical is "ESCI-08". Also accepted: the bare number as int or string (the
    reddit-analysis specs, a Qualtrics column named `8`), and any case/spacing of
    the canonical form. Leniency here is safe because it is only ever a lookup;
    `validate` layers strictness on top by insisting each item is named exactly
    once, so "8" and "ESCI-08" in the same submission is reported as a duplicate
    rather than silently collapsing into one answer.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return _BY_NUMBER.get(raw)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s in _BY_ID:  # the canonical form, which is the overwhelming common case
        return _BY_ID[s]
    if s.isdigit():
        return _BY_NUMBER.get(int(s))
    s = s.upper().replace(" ", "").replace("_", "-")
    if s.startswith("ESCI-"):
        tail = s[5:]
        if tail.isdigit():
            return _BY_NUMBER.get(int(tail))
    return None


def item(item_id_or_number: Any) -> Optional[Dict[str, Any]]:
    """One item, or None when nothing answers to that id."""
    found = _resolve(item_id_or_number)
    return dict(found) if found else None


def slug_for(item_id_or_number: Any) -> Optional[str]:
    """The scenario specs' trigger-tag slug for an item, or None if it has none."""
    found = _resolve(item_id_or_number)
    return ITEM_TO_SLUG.get(found["id"]) if found else None


def item_for_slug(slug: str) -> Optional[Dict[str, Any]]:
    """The item a scenario spec's `esci:` trigger tag refers to."""
    if not isinstance(slug, str):
        return None
    return item(SLUG_TO_ITEM.get(slug.strip()))


def items_for_slugs(slugs: Any) -> List[Dict[str, Any]]:
    """Resolve a trigger's `esci: [...]` list, dropping slugs with no item.

    Dropping rather than raising: this runs over recorded encounters, and a spec
    that has moved on since an encounter was recorded should not make that
    encounter unreadable. Callers who care about the gap can compare lengths.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for s in (slugs or []):
        found = item_for_slug(s)
        if found and found["id"] not in seen:
            seen.add(found["id"])
            out.append(found)
    return out


# --- Scoring -----------------------------------------------------------------

def score_value(item_id: Any, raw: Any) -> Optional[int]:
    """The construct-aligned value of a raw rater response.

    Raw responses are on the instrument's own 1-5 scale, worded as the rater saw
    them. For the three reverse items the wording runs against the construct, so
    the analysable value is 6 - raw: a rater who says the participant "never"
    (1) allows conflict to fester is reporting the *strongest* conflict
    management on that item, which is a 5.

    None in, None out: "not enough information to judge" survives scoring as
    null and is excluded pairwise downstream.

    Raises on an unknown item or an out-of-range value rather than returning
    None. None already means N/A, and a bad id that scored as N/A would be
    indistinguishable from a rater who honestly could not tell, which is exactly
    the failure this module exists to prevent.
    """
    found = _resolve(item_id)
    if found is None:
        raise ValueError(f"unknown ESCI item id: {item_id!r}")
    if raw is None:
        return None
    n = _as_scale_int(raw)
    if n is None:
        raise ValueError(
            f"{found['id']}: {raw!r} is not a whole number in "
            f"{SCALE_MIN}..{SCALE_MAX} (or null for '{NA_LABEL}')"
        )
    return (SCALE_MAX + SCALE_MIN) - n if found["reverse"] else n


def _as_scale_int(raw: Any) -> Optional[int]:
    """Coerce a response to an in-range integer, or None if it is not one.

    Strings are accepted because the Qualtrics ingest path is real and a CSV
    export hands back "4", not 4. Floats are accepted only when they are exactly
    integral: 4.0 is a JSON round-trip of 4, but 4.5 is a response nobody on a
    1-5 scale could have given and is a bug somewhere upstream. bool is rejected
    outright even though it is an int subclass, because True quietly scoring as
    "Never" is precisely the kind of silence this module is written against.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        n = raw
    elif isinstance(raw, float):
        if raw != int(raw):
            return None
        n = int(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        if f != int(f):
            return None
        n = int(f)
    else:
        return None
    return n if SCALE_MIN <= n <= SCALE_MAX else None


def scored(scores: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Reverse-code a whole submission at once, keyed by canonical item id.

    The one place reverse coding should happen. Reliability, the competency means
    and any export all go through here rather than re-deriving 6 - raw, so there
    is exactly one line in the codebase that could get the direction wrong and it
    has a test aimed straight at it.

    Keys are re-emitted canonical, so a submission that came in through the
    Qualtrics route keyed by bare number comes out keyed the same way as one from
    the console. Unknown ids raise, as in `score_value`; run `validate` first if
    the input is untrusted.
    """
    out: Dict[str, Optional[int]] = {}
    for key, value in (scores or {}).items():
        found = _resolve(key)
        if found is None:
            raise ValueError(f"unknown ESCI item id: {key!r}")
        out[found["id"]] = score_value(found["id"], value)
    return out


# --- Validation --------------------------------------------------------------

def validate(scores: Any) -> List[str]:
    """Human-readable problems with a submission; empty list when it is valid.

    Strict on everything that would corrupt a label, permissive on nothing else:

      * every item in the bank must be present, because a partial submission and
        a submission full of honest N/As are different claims and the console
        should force the rater to say which one this is;
      * values must be a whole number in 1..5, or null for N/A;
      * ids must be items, and each item may be named exactly once (so "8" and
        "ESCI-08" in the same payload is a duplicate, not an overwrite).

    Returns messages rather than raising because the caller is an HTTP handler
    reporting back to a rater who is looking at the form: they need all of the
    problems at once, phrased for a person.
    """
    if not isinstance(scores, dict):
        return ["scores must be an object mapping item id to a 1-5 rating or null"]

    problems: List[str] = []
    resolved: Dict[str, List[Any]] = {}

    for key, value in scores.items():
        found = _resolve(key)
        if found is None:
            problems.append(f"unknown item id: {key!r}")
            continue
        resolved.setdefault(found["id"], []).append(key)
        if value is None:
            continue  # "not enough information to judge", explicitly allowed
        if _as_scale_int(value) is None:
            problems.append(
                f"{found['id']}: {value!r} is not a whole number in "
                f"{SCALE_MIN}-{SCALE_MAX} (use null for '{NA_LABEL}')"
            )

    for iid, keys in sorted(resolved.items()):
        if len(keys) > 1:
            problems.append(
                f"{iid} rated more than once, as " +
                ", ".join(repr(k) for k in keys)
            )

    missing = [it["id"] for it in ITEMS if it["id"] not in resolved]
    if missing:
        problems.append(
            f"missing {len(missing)} of {len(ITEMS)} items: " + ", ".join(missing)
        )

    return problems


# --- Handing the bank out ----------------------------------------------------

def item_bank(construct: Optional[str] = None,
              include_text: bool = True) -> Dict[str, Any]:
    """The bank in the shape a rating console, packet or export wants it.

    This is the supported way to put ESCI items in front of a human or into a
    file, and the reason it exists rather than callers assembling `all_items()`
    themselves is `notice`: the items are licensed material and the warning has
    to arrive attached to them, not in a README somebody read once. Every payload
    from here carries it.

    `include_text=False` returns the structure without the item wording, for the
    places that need to enumerate or key by item (an id list, a reliability
    report's row labels) but have no business republishing the instrument.
    """
    items = all_items() if construct is None else items_for(construct)
    out_items = []
    for it in items:
        row = {
            "id": it["id"],
            "number": it["number"],
            "construct": it["construct"],
            "reverse": it["reverse"],
            "slug": ITEM_TO_SLUG.get(it["id"]),
        }
        if include_text:
            row["text"] = it["text"]
        out_items.append(row)
    return {
        "notice": NOTICE,
        "source": "ESCI item bank (Boyatzis, Goleman & Korn Ferry)",
        "scale": {
            "min": SCALE_MIN,
            "max": SCALE_MAX,
            "labels": dict(SCALE_LABELS),
            "na_label": NA_LABEL,
            "na_value": NA,
        },
        "constructs": [
            {"key": c, "label": CONSTRUCT_LABELS[c]}
            for c in CONSTRUCTS
            if construct is None or c == construct
        ],
        "items": out_items,
    }


def _main(argv: List[str]) -> int:
    """`python -m server.esci [--json]` — print the bank and the crosswalk.

    A researcher's way to see what the server thinks the instrument is without
    starting it, and the quickest check that a CSV edit landed.
    """
    if "--json" in argv:
        print(json.dumps(item_bank(), indent=2))
        return 0
    print(NOTICE)
    print()
    for c in CONSTRUCTS:
        rows = items_for(c)
        print(f"{CONSTRUCT_LABELS[c]} ({len(rows)} items)")
        for it in rows:
            flag = " (R)" if it["reverse"] else ""
            slug = ITEM_TO_SLUG.get(it["id"], "-")
            print(f"  {it['id']}  {slug:<26} {it['text']}{flag}")
        print()
    print(f"{len(ITEMS)} items, {len(REVERSE_ITEMS)} reverse-scored: "
          f"{', '.join(REVERSE_ITEMS)}")
    unmapped = [it["id"] for it in ITEMS if it["id"] not in ITEM_TO_SLUG]
    print("items with no scenario slug: " +
          (", ".join(unmapped) if unmapped else "none"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
