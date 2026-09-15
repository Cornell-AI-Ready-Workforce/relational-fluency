"""Per-slot form selection, and the second attempt that reaches the reserve.

A run is four encounters. An unrestricted run gives each of the four constructs
one of them; an arm link restricts the pool to two constructs, so each of those
gets two slots. That second slot is where every claim in this file lives.

The defect these tests were written against, measured before the change:

  - `variants=` pinned ONE form per construct for the whole run, so a construct
    with two slots was served the same form twice: 200 runs of 200 on both arms.
    A participant handed the same conversation again notices on the first line,
    and the second copy is worth nothing as data.
  - Because of that, sibling_run could not pin a restricted arm at all. It fell
    back to re-drawing the four encounters at random, which landed on the form
    attempt 1 had held back in 38 of 60 runs on the 1:1 arm and 34 of 60 on the
    group arm — by luck, with nothing on either run saying whether it had.
  - construct_pool answered from SLOT COUNTS rather than from forms served.
    `forms_in_reserve` was computed only for constructs an arm doubled up, so
    every unrestricted run in the study reported `{}` — no form held back —
    while the bank held five it had not served. That {} is copied onto the
    second attempt as attempt1_forms_in_reserve, so the pair of runs stated the
    opposite of the fact an analyst reads it for.

Everything below is counted by driving runs.create and runs.sibling_run, not by
reading the implementation back. Nothing here hard-codes the number of forms a
construct has: the bank grows (S1C and S3C were the third of their construct,
S2C and S4C follow), and a test that says "two" or "three" is a test that has to
be rewritten by whoever writes the next form. Where a specific count is needed
it is taken from the bank at run time, and one test builds a synthetic bank of
three, four and five forms per construct to show the rule holds at any width.
"""

from __future__ import annotations

import collections

import pytest

import server.scenarios_v3 as v3
from server import runs as runs_mod_real

ARMS = (None, "one_to_one", "group")
RESTRICTED = ("one_to_one", "group")
SEEDS = 200


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    """server.runs writing into a temp directory.

    Both globals, for the reason tests/test_links.py repoints both: RUNS_DIR is
    where the run files go and DATA_DIR is where the completion-code secret is
    persisted, and leaving the second pointing at the repository would mint a
    .run_code_secret next to real collection data.
    """
    monkeypatch.setattr(runs_mod_real, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs_mod_real, "RUNS_DIR", tmp_path / "runs")
    return runs_mod_real


def _ids(run: dict) -> list:
    return [s["id"] for s in run["scenarios"]]


def _by_construct(run: dict) -> dict:
    """{construct: [ids in slot order]} for one run."""
    out: dict = {}
    for row in run["scenarios"]:
        out.setdefault(row["construct"], []).append(row["id"])
    return out


def _bank() -> dict:
    return v3.forms_by_construct()


# --- 1. the routing authority ------------------------------------------------

def test_parallel_forms_is_derived_from_the_construct_and_not_from_the_scalar():
    """Three specs name `scenarios_v3.parallel_forms()` as the routing
    authority in their own comments. It has to exist and it has to answer from
    `construct`, because a `parallel_form:` scalar that names one of three reads
    as naming the only one — which is how a second attempt was handed the exact
    pairing the first was built to avoid."""
    bank = _bank()
    for construct, forms in bank.items():
        for sid in forms:
            others = v3.parallel_forms(sid)
            assert others == [f for f in forms if f != sid], (sid, others)
            assert sid not in others


def test_every_specs_parallel_form_scalar_is_one_of_its_derived_siblings():
    """The scalar stays as provenance, so it must not contradict the derivation.
    A scalar naming a form of another construct would mean the two disagree
    about what is parallel to what, and only one of them routes."""
    for sid in v3.available():
        scalar = v3.load_spec(sid).get("parallel_form")
        if scalar:
            assert scalar in v3.parallel_forms(sid), (sid, scalar)


def test_runs_and_scenarios_v3_agree_on_which_forms_are_parallel(runs_mod):
    """One authority, not two loops over the same directory that can drift."""
    assert runs_mod._by_construct() == v3.forms_by_construct()


# --- 2. a pin is per slot ----------------------------------------------------

def test_a_scalar_pin_no_longer_serves_one_conversation_twice(runs_mod):
    """The measured defect, pinned shut.

    A scalar `variants=` entry is one form and a restricted arm gives its
    construct two slots. Both slots took that one id: 200 of 200 on each arm.
    The first slot still gets it; the second gets an unseen form of the same
    construct rather than a repeat.
    """
    bank = _bank()
    for arm in RESTRICTED:
        constructs = runs_mod.arm_constructs(arm)[0]
        pin = {c: bank[c][0] for c in constructs}
        repeats = 0
        honoured_first = 0
        for seed in range(SEEDS):
            run = runs_mod.create(f"RD_SCALAR_{arm}{seed}", seed=seed, arm=arm,
                                  variants=pin)
            ids = _ids(run)
            if len(set(ids)) != len(ids):
                repeats += 1
            per = _by_construct(run)
            if all(per[c][0] == pin[c] for c in constructs):
                honoured_first += 1
        assert repeats == 0, (
            f"{repeats}/{SEEDS} runs on the {arm} arm served the same "
            f"conversation twice under a scalar pin")
        assert honoured_first == SEEDS, (arm, honoured_first)


def test_a_per_slot_pin_is_honoured_slot_by_slot(runs_mod):
    """The new shape. A list is the forms in slot order, and it is followed."""
    bank = _bank()
    for arm in RESTRICTED:
        constructs = runs_mod.arm_constructs(arm)[0]
        pin = {c: list(bank[c]) for c in constructs}
        for seed in range(SEEDS):
            run = runs_mod.create(f"RD_SLOT_{arm}{seed}", seed=seed, arm=arm,
                                  variants=pin)
            per = _by_construct(run)
            for c, got in per.items():
                assert got == pin[c][:len(got)], (arm, seed, c, got, pin[c])
            assert run["construct_pool"]["pinned_forms_unfilled"] == []


def test_a_pin_shorter_than_the_slots_falls_through_and_says_so(runs_mod):
    """A caller who named one form for a two-slot construct gets an unseen form
    in the other slot and a record of it. A silently drawn slot the caller
    believes they chose is worse than a shortfall they can read."""
    bank = _bank()
    for arm in RESTRICTED:
        constructs = runs_mod.arm_constructs(arm)[0]
        short = constructs[0]
        pin = {short: bank[short][0]}
        run = runs_mod.create(f"RD_SHORT_{arm}", seed=7, arm=arm, variants=pin)
        unfilled = run["construct_pool"]["pinned_forms_unfilled"]
        assert len(unfilled) == 1, unfilled
        entry = unfilled[0]
        assert entry["construct"] == short
        assert entry["requested_forms"] == [bank[short][0]]
        assert entry["served"] in _ids(run)
        assert entry["served"] != bank[short][0]


def test_an_empty_or_absent_pin_entry_is_not_a_pin(runs_mod):
    """A caller building a sequence from a filter can come up empty for a
    construct. That means "nothing pinned", which the draw already handles, and
    must not become an exception or a slot nobody chose marked as chosen."""
    run = runs_mod.create("RD_EMPTY", seed=2, arm="one_to_one",
                          variants={"conflict_management": [], "influence": None})
    assert len(set(_ids(run))) == 4
    assert not any(s.get("pinned") for s in run["scenarios"])


# --- 3. the second attempt reaches the reserve -------------------------------

def test_a_second_attempt_serves_every_form_the_first_held_back(runs_mod):
    """THE POINT OF THE CHANGE.

    Attempt 1 on a restricted arm spends two forms of each of its two
    constructs. Any construct with a third form therefore has one left, and
    attempt 2 must serve it — not sometimes, which is what a random re-draw
    gave (38/60 and 34/60), but every time there is one to serve and a slot to
    serve it in.
    """
    for arm in ARMS:
        for seed in range(60):
            first = runs_mod.create(f"RD_RES_{arm}{seed}", seed=seed, arm=arm)
            second = runs_mod.sibling_run(first["run_id"])
            held = first["construct_pool"]["forms_in_reserve"]
            per_now = _by_construct(second)
            for construct, forms in held.items():
                got = set(per_now.get(construct, []))
                slots = len(per_now.get(construct, []))
                # As many of the held-back forms as there are slots to put them
                # in. WHICH of them is rotated on attempt 1's run id rather
                # than fixed by sorted order (see sibling_run: sorted order
                # meant one form of a three-form construct was reachable by no
                # second attempt at all), so the claim is the count, not the
                # identity.
                assert len(got & set(forms)) == min(slots, len(forms)), (
                    arm, seed, construct, forms, per_now)
            served = second["construct_pool"]["attempt1_reserve_served"]
            expected = sorted(
                {f for fs in held.values() for f in fs} & set(_ids(second)))
            assert served == expected, (arm, seed, served, expected)
            if held:
                assert served, (arm, seed, held, _ids(second))


def test_a_second_attempt_is_still_four_separate_conversations(runs_mod):
    for arm in ARMS:
        for seed in range(60):
            first = runs_mod.create(f"RD_DIST_{arm}{seed}", seed=seed, arm=arm)
            ids = _ids(runs_mod.sibling_run(first["run_id"]))
            assert len(ids) == 4 and len(set(ids)) == 4, (arm, seed, ids)


def test_the_sibling_records_what_was_unseen_and_what_was_repeated(runs_mod):
    """Counted from the two sequences. `attempt2_forms_available` is the strict
    claim — EVERY slot of attempt 2 is a form attempt 1 did not serve — because
    an analyst reading True must be able to take the whole run as unseen. A
    partial reach is real and is reported as attempt2_unseen_forms, not by
    loosening the bool until it is true of runs it is not true of."""
    for arm in ARMS:
        for seed in range(40):
            first = runs_mod.create(f"RD_REC_{arm}{seed}", seed=seed, arm=arm)
            second = runs_mod.sibling_run(first["run_id"])
            pool = second["construct_pool"]
            before = _by_construct(first)
            now = _by_construct(second)
            unseen = {c: sorted({s for s in v if s not in set(before.get(c, []))})
                      for c, v in now.items()}
            repeat = {c: sorted({s for s in v if s in set(before.get(c, []))})
                      for c, v in now.items()}
            assert pool["attempt2_unseen_forms"] == {
                c: v for c, v in unseen.items() if v}
            assert pool["attempt2_repeated_forms"] == {
                c: v for c, v in repeat.items() if v}
            assert pool["attempt2_forms_available"] is (
                not pool["attempt2_repeated_forms"])
            assert pool["attempt1_run_id"] == first["run_id"]


def test_the_unrestricted_second_attempt_is_wholly_unseen(runs_mod):
    """The pre/post path the study actually runs on, unchanged: every construct
    changes form, so the strict flag is True."""
    for seed in range(60):
        first = runs_mod.create(f"RD_FULL_{seed}", seed=seed)
        second = runs_mod.sibling_run(first["run_id"])
        assert second["construct_pool"]["attempt2_forms_available"] is True
        assert not (set(_ids(first)) & set(_ids(second))), (
            seed, _ids(first), _ids(second))


# --- 4. the exclusion binds on every slot ------------------------------------

def test_form_exclusions_bind_on_every_slot_of_every_run_and_every_sibling(runs_mod):
    """The rule the per-slot pin must not open a side door in.

    FORM_EXCLUSIONS bars conflict management form A from any run containing
    teamwork. A pinned slot is exempt from the correction pass by design (an
    operator's `?variant=A` is a decision, and the run records the conflict), so
    the guarantee has to come from nothing barred ever being PINNED — which is
    what sibling_run filters for. Measured over every arm, attempt 1 and
    attempt 2, 200 seeds each.
    """
    bank = _bank()
    barred_forms = {
        construct: [f for f in bank.get(construct, [])
                    if v3.load_spec(f)["variant"].upper() == variant.upper()]
        for construct, variant, _ in runs_mod.FORM_EXCLUSIONS
    }
    checked = 0
    for construct, variant, requires in runs_mod.FORM_EXCLUSIONS:
        forbidden = set(barred_forms[construct])
        assert forbidden, (construct, variant)
        for arm in ARMS:
            for seed in range(SEEDS):
                first = runs_mod.create(f"RD_EXC_{arm}{seed}", seed=seed, arm=arm)
                second = runs_mod.sibling_run(first["run_id"])
                for label, run in (("attempt1", first), ("attempt2", second)):
                    checked += 1
                    constructs = {r["construct"] for r in run["scenarios"]}
                    if requires not in constructs:
                        continue
                    clash = forbidden & set(_ids(run))
                    assert not clash, (label, arm, seed, clash, _ids(run))
    assert checked >= 2 * SEEDS, checked


# --- 5. counterbalancing ------------------------------------------------------

def test_no_form_is_squeezed_out_of_the_arm_that_carries_it(runs_mod):
    """The draw's distribution, measured rather than asserted.

    A previous round reported a working draw that was running 2:1 between two
    forms and called the ratio a success. So this counts: every form of every
    construct an arm serves must reach a real share of participants, and none
    may be effectively universal, or the construct is back to being measured on
    one instrument.

    The bound is per slot-share, not per form count, so it does not have to be
    rewritten when a construct gains a form: with F forms and S slots the even
    share is S/F of runs, and a form drawing under a third of its even share is
    a form nobody will collect enough of.

    THE ARM THIS TEST DID NOT LOOK AT IS THE ONE THAT BROKE. It looped over
    RESTRICTED — precisely the two arms where FORM_EXCLUSIONS never fires,
    because neither of them contains both Conflict Management and Teamwork — so
    it passed at full green while the FULL arm, which carries the whole study,
    ran Conflict Management 68.1% S1B against 31.9% S1C over 2000 seeds: every
    one of the 673 draws that landed on the barred S1A was corrected to S1B and
    not once to S1C. That is the same 2:1 the docstring above says this test
    exists to catch, on the arm it was not measuring.

    The full arm needs its own bound, which is why it is a second loop rather
    than a third entry in RESTRICTED: S1A is LEGITIMATELY served zero times
    there (every full run contains Teamwork and the exclusion bars it), so the
    even share has to be taken over the forms the composition PERMITS, not over
    the bank. See test_the_one_to_one_arm_is_the_only_place_s1a_is_served_at_all
    in tests/test_links.py for the other half of that fact.

    The upper bound is stated as the RATIO between the most- and least-served
    permitted form, because that is the quantity the defect was reported in and
    an absolute share cannot separate a two-form construct from a four-form one.
    Measured at these 200 seeds after the fix: conflict management 1.20,
    influence 1.06, inspirational leadership 1.36, teamwork 1.31 on the full
    arm, and 1.15-1.23 on both restricted arms. The defect this catches ran
    2.13. 1.75 sits above the sampling spread of a three-form construct at this
    n and well below the skew.
    """
    report = {}
    bank_full = _bank()
    full_counts: collections.Counter = collections.Counter()
    full_slots: collections.Counter = collections.Counter()
    for seed in range(SEEDS):
        run = runs_mod.create(f"RD_CBFULL{seed}", seed=seed)
        for row in run["scenarios"]:
            full_counts[row["id"]] += 1
            full_slots[row["construct"]] += 1
    for construct, forms in bank_full.items():
        # The forms this arm is allowed to serve at all. A form barred by
        # FORM_EXCLUSIONS is expected at zero and must not drag the even share
        # down for the ones that have to absorb its draws.
        permitted = [f for f in forms if full_counts.get(f, 0) > 0]
        barred = {f for c, v, _req in runs_mod.FORM_EXCLUSIONS if c == construct
                  for f in forms if v3.load_spec(f)["variant"].upper() == v.upper()}
        assert set(forms) - set(permitted) <= barred, (
            construct, sorted(set(forms) - set(permitted)), sorted(barred))
        even = full_slots[construct] / len(permitted)
        got = {f: full_counts.get(f, 0) for f in permitted}
        report[("full", construct)] = got
        for form, n in got.items():
            assert n >= even / 3, ("full", construct, got, even)
        assert max(got.values()) <= 1.75 * min(got.values()), (
            "full", construct, got,
            "one permitted form is crowding out another on the arm that "
            "carries the whole study")

    for arm in RESTRICTED:
        counts: collections.Counter = collections.Counter()
        per_construct_slots: collections.Counter = collections.Counter()
        for seed in range(SEEDS):
            run = runs_mod.create(f"RD_CB_{arm}{seed}", seed=seed, arm=arm)
            for row in run["scenarios"]:
                counts[row["id"]] += 1
                per_construct_slots[row["construct"]] += 1
        bank = _bank()
        for construct in runs_mod.arm_constructs(arm)[0]:
            forms = bank[construct]
            even = per_construct_slots[construct] / len(forms)
            got = {f: counts.get(f, 0) for f in forms}
            report[(arm, construct)] = got
            for form, n in got.items():
                assert n >= even / 3, (arm, construct, got, even)
                assert n <= SEEDS, (arm, construct, got)
    assert report, "nothing was measured"


def test_the_second_attempt_reaches_every_form_it_is_allowed_to_reach(runs_mod):
    """No form of any construct may be unreachable by a second attempt.

    The unrestricted arm is where this went wrong and where it matters most: it
    gives every construct ONE slot, so a construct with three forms has two
    unseen ones and only one place to put them. Taking the sorted first meant
    that whichever form attempt 1 drew, the alphabetically earlier of the other
    two won — Inspirational Leadership went out 134/66/0 over 200 second
    attempts, so S3C was reachable by no participant on a retest at all. The
    order is now rotated on attempt 1's run id.

    The exception, asserted rather than skipped: a form this composition BARS
    is unreachable on purpose. Every full run contains Teamwork, so conflict
    management form A must stay at zero there.
    """
    bank = _bank()
    barred = {f for f in bank.get("conflict_management", [])
              if v3.load_spec(f)["variant"].upper() == "A"}
    for arm in ARMS:
        seen: collections.Counter = collections.Counter()
        for seed in range(120):
            first = runs_mod.create(f"RD_CB2_{arm}{seed}", seed=seed, arm=arm)
            second = runs_mod.sibling_run(first["run_id"])
            for sid in _ids(second):
                seen[sid] += 1
            if any(r["construct"] == "teamwork" for r in second["scenarios"]):
                assert not (barred & set(_ids(second))), (arm, seed, _ids(second))
        for construct in runs_mod.arm_constructs(arm or "full")[0]:
            forms = bank[construct]
            reachable = [f for f in forms
                         if not (arm in (None, "full") and f in barred)]
            got = {f: seen.get(f, 0) for f in reachable}
            assert all(n > 0 for n in got.values()), (arm, construct, got)


# --- 6. construct_pool tells the truth ---------------------------------------

def test_forms_in_reserve_is_every_permitted_form_the_run_did_not_serve(runs_mod):
    """Counted against the bank, on every arm including the unrestricted one.

    This answered {} on every full run, because it was scoped to constructs an
    arm had doubled up. A full run gives every construct one slot, so on the arm
    that carries the whole study the field said "nothing held back" while the
    bank held one or two unseen forms for every construct in it.
    """
    bank = _bank()
    for arm in ARMS:
        for seed in range(60):
            run = runs_mod.create(f"RD_TRUTH_{arm}{seed}", seed=seed, arm=arm)
            served = _by_construct(run)
            constructs = set(served)
            forbidden = {}
            for construct, variant, requires in runs_mod.FORM_EXCLUSIONS:
                if requires in constructs:
                    forbidden.setdefault(construct, set()).add(variant.upper())
            truth = {}
            for construct, got in served.items():
                left = sorted(
                    f for f in bank.get(construct, [])
                    if f not in got
                    and v3.load_spec(f)["variant"].upper()
                    not in forbidden.get(construct, set()))
                if left:
                    truth[construct] = left
            pool = run["construct_pool"]
            assert pool["forms_in_reserve"] == truth, (arm, seed, _ids(run))
            assert pool["parallel_forms_spent"] is (not truth), (arm, seed)


def test_a_run_that_really_has_spent_everything_says_so(runs_mod):
    """The flag's other half, so it is not merely stuck on False. Narrow an arm
    to a single construct and the run fills all four slots from it, which spends
    every form it is allowed to serve however many there are."""
    only = runs_mod.create("RD_SPENT", seed=5, arm="one_to_one",
                           constructs=["influence"])
    pool = only["construct_pool"]
    assert pool["forms_in_reserve"] == {}
    assert pool["parallel_forms_spent"] is True


def test_the_reserve_never_names_a_form_the_run_may_not_serve(runs_mod):
    """A form this composition bars is not in reserve: a second attempt carries
    the arm over, so it could not be served that form either."""
    bank = _bank()
    barred = [f for f in bank.get("conflict_management", [])
              if v3.load_spec(f)["variant"].upper() == "A"]
    for arm in ARMS:
        for seed in range(60):
            run = runs_mod.create(f"RD_NORES_{arm}{seed}", seed=seed, arm=arm)
            reserve = run["construct_pool"]["forms_in_reserve"]
            ids = set(_ids(run))
            for forms in reserve.values():
                assert not (set(forms) & ids), (arm, seed, reserve, ids)
            if any(r["construct"] == "teamwork" for r in run["scenarios"]):
                assert not (set(barred)
                            & set(reserve.get("conflict_management", []))), (
                    arm, seed, reserve)


# --- 7. slot order ------------------------------------------------------------

def test_two_forms_of_one_construct_never_sit_next_to_each_other(runs_mod):
    """Why _slots_for cycles rather than blocks: the two forms of a construct
    are the same situation twice, and a participant who has just finished one
    recognises the next immediately. True of the second attempt too, which is
    built through the same slot machinery."""
    for arm in ARMS:
        if len(runs_mod.arm_constructs(arm or "full")[0]) < 2:
            continue
        for seed in range(60):
            first = runs_mod.create(f"RD_ADJ_{arm}{seed}", seed=seed, arm=arm)
            second = runs_mod.sibling_run(first["run_id"])
            for run in (first, second):
                cs = [r["construct"] for r in run["scenarios"]]
                assert all(a != b for a, b in zip(cs, cs[1:])), (arm, seed, cs)


# --- 8. a seeded run is reproducible -----------------------------------------

def test_the_same_attempt_one_always_yields_the_same_attempt_two(runs_mod):
    """sibling_run's headline property, taken to include the ORDER.

    The docstring says "a given attempt 1 always yields the same attempt 2", and
    that was true of which FORMS it served and of nothing else: create() was
    called without a seed, so `rng.shuffle(order)` drew construct order from
    system entropy. Eight calls on one attempt-1 run gave one form set and TWO
    distinct orderings — and construct order is counterbalancing, so a pair of
    runs that disagreed about it is a pair an analyst cannot reconstruct.
    create() is now seeded on attempt 1's run id.
    """
    for arm in ARMS:
        first = runs_mod.create(f"RD_STABLE{arm}", seed=7, arm=arm)
        seqs = {tuple(_ids(runs_mod.sibling_run(first["run_id"])))
                for _ in range(6)}
        assert len(seqs) == 1, (arm, seqs)


def test_the_second_attempt_is_still_counterbalanced_across_participants(runs_mod):
    """The other side of seeding it: fixed for one participant, not fixed for
    the study. The seed varies with the run id, so construct order still moves
    across participants — otherwise the fix above would have traded a
    reproducibility bug for an order confound."""
    orders = collections.Counter()
    for seed in range(120):
        first = runs_mod.create(f"RD_CB2_{seed}", seed=seed)
        orders[tuple(r["construct"]
                     for r in runs_mod.sibling_run(first["run_id"])["scenarios"])] += 1
    assert len(orders) >= 12, dict(orders)
    assert max(orders.values()) <= 0.25 * sum(orders.values()), dict(orders)


def test_a_construct_with_nothing_permitted_repeats_rather_than_serving_a_barred_form(
        runs_mod, monkeypatch):
    """The last-resort branch, which used to reinstate the defect.

    When every form of a construct is barred in this composition, sibling_run
    falls back. That fallback read the spec's `parallel_form` scalar — and S1B's
    scalar is S1A, which is the barred form, and the fallback is passed to
    create() as a PIN, and _apply_form_exclusions leaves a pinned slot alone by
    design. So the branch would have served exactly the pairing the function
    exists to avoid, in the one case nobody inspects. It cannot fire on the
    shipped bank (one exclusion row, three forms per construct), so it is driven
    here by barring every Conflict Management letter.
    """
    bank = _bank()
    letters = sorted({v3.load_spec(f)["variant"].upper()
                      for f in bank["conflict_management"]})
    monkeypatch.setattr(
        runs_mod, "FORM_EXCLUSIONS",
        [("conflict_management", letter, "teamwork") for letter in letters])
    first = runs_mod.create("RD_FALLBACK", seed=3)
    second = runs_mod.sibling_run(first["run_id"])
    served = [r["id"] for r in second["scenarios"]
              if r["construct"] == "conflict_management"]
    assert served, second["scenarios"]
    # Whatever it served is a form attempt 1 actually served, not a scalar
    # pointing at something the composition bars.
    assert set(served) <= set(_ids(first)), (served, _ids(first))


def test_a_seed_reproduces_its_run_exactly(runs_mod):
    """Seed parity. An analysis that recorded a seed must be able to rebuild the
    assignment it recorded, on every arm and through the pinned path as well as
    the drawn one."""
    bank = _bank()
    for arm in ARMS:
        for seed in range(40):
            a = runs_mod.create(f"RD_SEED_A{arm}{seed}", seed=seed, arm=arm)
            b = runs_mod.create(f"RD_SEED_B{arm}{seed}", seed=seed, arm=arm)
            assert _ids(a) == _ids(b), (arm, seed, _ids(a), _ids(b))
    pin = {c: list(f) for c, f in bank.items()}
    for seed in range(20):
        a = runs_mod.create(f"RD_SEEDP_A{seed}", seed=seed, arm="group",
                            variants=pin)
        b = runs_mod.create(f"RD_SEEDP_B{seed}", seed=seed, arm="group",
                            variants=pin)
        assert _ids(a) == _ids(b), (seed, _ids(a), _ids(b))


def test_the_pin_path_consumes_no_randomness_while_it_can_fill_the_slot(runs_mod):
    """Seed parity has a second half: a pin that covers its slots must not shift
    the random stream the other constructs draw from, or pinning one construct
    would silently re-roll the rest of the run."""
    bank = _bank()
    for seed in range(40):
        plain = runs_mod.create(f"RD_RNG_A{seed}", seed=seed, arm="group")
        per = _by_construct(plain)
        pinned = runs_mod.create(f"RD_RNG_B{seed}", seed=seed, arm="group",
                                 variants={c: list(v) for c, v in per.items()})
        assert _ids(pinned) == _ids(plain), (seed, _ids(plain), _ids(pinned))
        assert bank  # the bank was read, so the assertion above is about forms


# --- 9. it holds at any number of forms per construct ------------------------

_SYNTHETIC = {
    # construct -> (letters, mode). Three widths at once, so nothing here
    # depends on the shipped bank being any particular size.
    "conflict_management": ("ABCD", "one_to_one"),
    "influence": ("ABC", "one_to_one"),
    "inspirational_leadership": ("ABCDE", "group"),
    "teamwork": ("ABC", "group"),
}

_PREFIX = {"conflict_management": "S1", "influence": "S2",
           "inspirational_leadership": "S3", "teamwork": "S4"}


@pytest.fixture()
def wide_bank(tmp_path, monkeypatch):
    """A synthetic scenario directory with 3, 4 and 5 forms per construct.

    The shipped bank is what it is on the day the suite runs — S1C and S3C were
    the third of their construct and more are being written — so the rules are
    checked here against widths nobody has shipped. Written as real spec files
    and read through the real loader, so the thing under test is the same code
    path a participant gets.
    """
    import yaml

    d = tmp_path / "v3"
    d.mkdir()
    for construct, (letters, mode) in _SYNTHETIC.items():
        for letter in letters:
            sid = f"{_PREFIX[construct]}{letter}"
            spec = {
                "id": sid,
                "construct": construct,
                "variant": letter,
                "title": f"{construct} {letter}",
                "parallel_form": f"{_PREFIX[construct]}{letters[0]}",
                "agents": {"other": {"name": "Sam", "role": "counterpart"}},
                "interactions": [{"mode": mode}, {"mode": mode}],
            }
            (d / f"{sid}.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    monkeypatch.setattr(v3, "V3_DIR", d)
    monkeypatch.setattr(runs_mod_real, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs_mod_real, "RUNS_DIR", tmp_path / "runs")
    return runs_mod_real


def test_the_rules_hold_at_three_four_and_five_forms_per_construct(wide_bank):
    """Every claim above, re-measured on a bank no shipped spec set has.

    Four forms of conflict management and two 1:1 slots means attempt 1 leaves
    TWO in reserve and attempt 2 can take both — which is the case the old
    one-form-per-construct pin could not express at all, and the case a bank of
    twelve forms is heading towards.
    """
    runs = wide_bank
    assert {c: len(f) for c, f in v3.forms_by_construct().items()} == {
        "conflict_management": 4, "influence": 3,
        "inspirational_leadership": 5, "teamwork": 3}
    for arm in ARMS:
        for seed in range(40):
            first = runs.create(f"RD_WIDE_{arm}{seed}", seed=seed, arm=arm)
            second = runs.sibling_run(first["run_id"])
            assert len(set(_ids(first))) == 4, _ids(first)
            assert len(set(_ids(second))) == 4, _ids(second)
            # No S1A alongside teamwork, in either attempt.
            for run in (first, second):
                ids = set(_ids(run))
                if any(r["construct"] == "teamwork" for r in run["scenarios"]):
                    assert "S1A" not in ids, (arm, seed, ids)
            # Every form attempt 1 held back that attempt 2 had a slot for.
            held = first["construct_pool"]["forms_in_reserve"]
            now = _by_construct(second)
            for construct, forms in held.items():
                got = set(now.get(construct, []))
                slots = len(now.get(construct, []))
                assert len(got & set(forms)) == min(slots, len(forms)), (
                    arm, seed, construct, forms, now)
            # Adjacency, on a pool wide enough to cycle.
            for run in (first, second):
                cs = [r["construct"] for r in run["scenarios"]]
                assert all(a != b for a, b in zip(cs, cs[1:])), (arm, seed, cs)


def test_a_wide_construct_gives_a_second_attempt_two_unseen_forms(wide_bank):
    """With four forms and two slots, attempt 2 can be wholly unseen for that
    construct. The old pin could name one form for the construct and no more, so
    this was unreachable however many forms were written."""
    runs = wide_bank
    reached_both = 0
    for seed in range(40):
        first = runs.create(f"RD_WIDE2_{seed}", seed=seed, arm="one_to_one")
        second = runs.sibling_run(first["run_id"])
        before = set(_by_construct(first).get("conflict_management", []))
        now = _by_construct(second).get("conflict_management", [])
        assert len(now) == 2, now
        if not (set(now) & before):
            reached_both += 1
    assert reached_both == 40, reached_both
