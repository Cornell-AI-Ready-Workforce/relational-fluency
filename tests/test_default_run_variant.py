"""THE TWO STUDY DESIGNS, AND WHICH ONE IS IN CHARGE AFTER THE MERGE.

Two mechanisms for "which form of each construct does a participant get" came
out of the same week's work, and they are not compatible:

  origin/main (jl3369, cabc1dd)  Phase 1 runs VARIANT A ONLY. Every study run is
                                 S1A, S2A, S3A, S4A, with the construct order
                                 counterbalanced per participant.
                                 `DEFAULT_RUN_VARIANT=A` is the code default.

  the researcher's tree          TWELVE FORMS, three per construct, drawn per
                                 slot, two of each construct's three used and
                                 the third held back as a reserve so a second
                                 attempt has material the participant has not
                                 met; FORM_EXCLUSIONS applied to the completed
                                 draw with a digest-rotated replacement.

The merge keeps BOTH and lets `DEFAULT_RUN_VARIANT` choose, with the default
left at `A` -- so the default behaviour of the merged platform is origin/main's
and the twelve-form draw is reachable by configuration. That is a deliberate
merge decision. WHICH DESIGN PHASE 1 SHOULD ACTUALLY RUN IS NOT A MERGE
DECISION AND IS NOT SETTLED HERE: it is the PI's.

What this module does is make both halves of the choice visible and checked,
including the part that is easy to miss, which is what an A-only Phase 1 does
to the S1A/Teamwork exclusion. See docs/OPERATIONS.md, "Which scenarios a
participant gets", and docs/migration-plan.md.
"""

import os

import pytest

from server import runs
from server.runs import FORM_EXCLUSIONS


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs", raising=False)
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)


def _ids(run):
    return [s["id"] for s in run["scenarios"]]


def _variants(run):
    return {s["variant"].upper() for s in run["scenarios"]}


# ---------------------------------------------------------------------------
# 1. The shipped default is HERS.
# ---------------------------------------------------------------------------

def test_the_code_default_is_variant_a(monkeypatch):
    """No DEFAULT_RUN_VARIANT set anywhere: the platform still runs A only.

    This is the assertion that says the merge did not quietly restore the other
    design by leaving the variable unset in one deployment and set in another.
    """
    monkeypatch.delenv("DEFAULT_RUN_VARIANT", raising=False)
    run = runs.create("RF_DEFAULT_A", qualtrics_id="R_DA")
    assert _variants(run) == {"A"}, _ids(run)


def test_b_pins_the_other_form_and_random_restores_the_draw(monkeypatch):
    """The two other settings origin/main's OPERATIONS.md section promises."""
    monkeypatch.setenv("DEFAULT_RUN_VARIANT", "B")
    assert _variants(runs.create("RF_DEFAULT_B", qualtrics_id="R_DB")) == {"B"}

    monkeypatch.setenv("DEFAULT_RUN_VARIANT", "random")
    seen = set()
    for i in range(40):
        seen |= _variants(runs.create(f"RF_DEFAULT_R{i}", qualtrics_id=f"R_R{i}"))
    assert len(seen) > 1, (
        f"DEFAULT_RUN_VARIANT=random drew only {seen}; the per-slot draw is "
        f"the other half of this merge and it has to still be reachable")


def test_the_construct_order_is_still_counterbalanced(monkeypatch):
    """A-only pins the FORM, not the ORDER. Losing the counterbalancing would
    be a real loss hiding inside a variant pin."""
    monkeypatch.delenv("DEFAULT_RUN_VARIANT", raising=False)
    orders = {tuple(s["construct"] for s in
                    runs.create(f"RF_ORD{i}", qualtrics_id=f"R_O{i}")["scenarios"])
              for i in range(40)}
    assert len(orders) > 1, "every participant got the same construct order"


# ---------------------------------------------------------------------------
# 2. What it costs, said out loud.
# ---------------------------------------------------------------------------

def test_the_a_only_default_switches_the_s1a_teamwork_exclusion_off(monkeypatch):
    """THE CONSEQUENCE THE PI HAS TO RULE ON, as a test rather than as prose.

    FORM_EXCLUSIONS bars S1A from any run that also contains Teamwork: the two
    overlap on grounded content (1,631 shared groundings against 77 for the
    alternative), which is a discriminant-validity problem, and the rule exists
    to keep them apart. Its ONE escape hatch is a form the caller pinned --
    honoured as asked, and the run stamped "form was pinned by the caller;
    exclusion not applied".

    `DEFAULT_RUN_VARIANT=A` pins S1A on EVERY run, so every run containing
    Teamwork takes that hatch and the exclusion does not apply anywhere in
    Phase 1. Nothing errors and nothing looks wrong; the runs say the exclusion
    was deliberately not applied, which is exactly what the stamp is for.

    This test does not judge that. It fails if it ever stops being true, so the
    decision cannot be un-made by accident in either direction.
    """
    construct, forbidden, requires = FORM_EXCLUSIONS[0]
    monkeypatch.delenv("DEFAULT_RUN_VARIANT", raising=False)

    saw_the_pairing = False
    for i in range(20):
        run = runs.create(f"RF_EXC{i}", qualtrics_id=f"R_E{i}")
        constructs = {s["construct"] for s in run["scenarios"]}
        if requires not in constructs or construct not in constructs:
            continue
        slot = next(s for s in run["scenarios"] if s["construct"] == construct)
        assert slot["variant"].upper() == forbidden, _ids(run)
        saw_the_pairing = True
        stamped = [e for e in run["form_exclusions"]
                   if e.get("reason", "").startswith("form was pinned")]
        assert stamped, (
            "the run serves the forbidden pairing and does not say so; the "
            "stamp is the only thing that makes this visible in the data")
        assert all(e.get("resolved") is False for e in stamped)
    assert saw_the_pairing, (
        "no run in twenty contained both constructs; this test measured "
        "nothing")


def test_the_run_says_who_pinned_the_form(monkeypatch):
    """"The caller" is now sometimes a server default, and the record has to be
    able to tell the two apart.

    Without this, every Phase 1 run carries a sentence blaming a caller who
    does not exist, and an analyst counting how many pairings were a deliberate
    operator pilot cannot answer.
    """
    monkeypatch.delenv("DEFAULT_RUN_VARIANT", raising=False)
    by_default = runs.create("RF_SRC_D", qualtrics_id="R_SD")
    assert by_default["construct_pool"]["variant_pin"] == "A"
    assert by_default["construct_pool"]["variant_pin_source"] == "default_run_variant"

    by_caller = runs.create("RF_SRC_C", qualtrics_id="R_SC", variant="A")
    assert by_caller["construct_pool"]["variant_pin_source"] == "caller"

    monkeypatch.setenv("DEFAULT_RUN_VARIANT", "random")
    unpinned = runs.create("RF_SRC_N", qualtrics_id="R_SN")
    assert unpinned["construct_pool"]["variant_pin"] is None
    assert unpinned["construct_pool"]["variant_pin_source"] is None


def test_an_explicit_variant_still_beats_the_default(monkeypatch):
    """The operator pilot origin/main's own note promises still works, and a
    second attempt's flip is not overridden by the default either."""
    monkeypatch.setenv("DEFAULT_RUN_VARIANT", "A")
    run = runs.create("RF_EXPLICIT", qualtrics_id="R_EX", variant="B")
    assert _variants(run) == {"B"}, _ids(run)


# ---------------------------------------------------------------------------
# 3. The twelve-form bank is still there under the default.
# ---------------------------------------------------------------------------

def test_the_twelve_form_bank_still_exists_whatever_the_default_is():
    """A-only SELECTS one form per construct. It must not have deleted the
    other two, or `DEFAULT_RUN_VARIANT=random` would be a switch with nothing
    behind it."""
    from server import scenarios_v3 as v3

    by_construct = {}
    for sid in v3.available():
        spec = v3.load_spec(sid)
        by_construct.setdefault(spec["construct"], set()).add(
            spec["variant"].upper())
    assert len(by_construct) == 4, sorted(by_construct)
    for construct, letters in by_construct.items():
        assert letters >= {"A", "B", "C"}, (construct, sorted(letters))
