"""The three links that go into Qualtrics, and the run each one builds.

Qualtrics is handed three URLs and nothing else: one for the two-person arm, one
for the group arm, and one for the rating console. Everything a participant or
a rater experiences follows from what those URLs do on arrival, and the failures
they can have are all silent ones — a run assigned to the wrong arm, a key that
piped as the literal ${e://Field/...}, a returning participant forked into a
second half-finished run, a rater token shared by every rater in the panel.

So these tests are about the entrances, not about the conversation behind them:
what the link accepts, what it records, and what it refuses.
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from server import app as appmod


# ---------------------------------------------------------------------------
# WHICH STUDY DESIGN THESE TESTS ARE ABOUT
#
# A FEW of the tests below measure the PER-SLOT DRAW: twelve forms, two of each
# construct's three used and the third held in reserve, FORM_EXCLUSIONS applied
# to the completed draw. `DEFAULT_RUN_VARIANT=random` is the setting that
# selects it, and those tests -- and only those -- carry
# `@pytest.mark.usefixtures("per_slot_draw")`.
#
# The merged DEFAULT is `A` -- origin/main's Phase 1 design, which pins
# S1A/S2A/S3A/S4A on every run. That is deliberate and it is the PI's call, not
# this file's. A pinned run has nothing to say about the draw: _apply_form_
# exclusions leaves a pinned slot alone BY DESIGN, so under the default the
# exclusion never fires and the reserve is never drawn, and an assertion about
# either would be testing a mechanism that is switched off rather than one that
# is broken. Those are the marked tests.
#
# EVERYTHING ELSE RUNS ON THE SHIPPED DEFAULT, which is the point of this note.
# The fixture was autouse in this module and four others until 2026-09-15, and
# that pinned 270 tests onto `random` when 20 of them are about the draw:
# stripped and re-run under the default, 15 failed and 255 passed. Entry links,
# arms, gates and cohort integrity are not about form selection, and pinning
# them meant the configuration Phase 1 will actually run had almost no coverage
# in this suite at all.
#
# The shipped default itself, and the fact that it turns the S1A/Teamwork
# exclusion off for the whole of Phase 1, is asserted head-on in
# tests/test_default_run_variant.py.
# ---------------------------------------------------------------------------

@pytest.fixture
def per_slot_draw(monkeypatch):
    """Ask for the per-slot draw, for the handful of tests that are ABOUT it.

    NOT autouse. It was, in all five of these modules, and that pinned 270
    tests off the configuration Phase 1 actually runs when only 20 of them
    need it: measured on 2026-09-15 by stripping the fixture and running the
    five modules under the shipped default -- 15 failed, 255 passed. Tests
    about entry links, arms, gates and cohort integrity are not about form
    selection and now run on the default a participant will meet.
    """
    monkeypatch.setenv("DEFAULT_RUN_VARIANT", "random")


PARTICIPANT_LINKS = ["/start", "/start/one-to-one", "/start/group"]
ARM_LINKS = {"/start": "full", "/start/one-to-one": "one_to_one",
             "/start/group": "group"}

VALID_TOKEN = "rt_" + "a" * 32
UNKNOWN_TOKEN = "rt_" + "b" * 32


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    """server.runs writing into a temp directory.

    Both globals are repointed, not one: RUNS_DIR is where the run files go, and
    DATA_DIR is where the completion-code secret is persisted. Leaving the
    second pointing at the repository's data/ would mint a .run_code_secret next
    to real collection data on whichever machine ran the suite.
    """
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def client(tmp_path, monkeypatch, runs_mod):
    """A TestClient whose storage is entirely inside tmp_path."""
    from server import storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture()
def rater_roster(monkeypatch):
    """One live token and nothing else, standing in for the rater store.

    Only rater_for_token is faked: the shape check, the 401 and the redirect are
    the code under test.
    """
    fake = types.ModuleType("server.raters")
    fake.rater_for_token = lambda tok: (
        {"rater_id": "rr_000000000001", "name": "Rater A", "kind": "trained"}
        if tok == VALID_TOKEN else None
    )
    monkeypatch.setitem(sys.modules, "server.raters", fake)
    import server

    monkeypatch.setattr(server, "raters", fake, raising=False)
    return fake


def _run_id_from(response) -> str:
    # 307 for a GET arrival, 303 for the entry check page's Continue POST — the
    # POST must NOT be preserved, because a preserved POST lands on /v2, which
    # serves GET only. Both are arrivals; which one a test gets depends only on
    # whether the link carried a key this server could use.
    assert response.status_code in (307, 303), response.text
    loc = response.headers["location"]
    assert loc.startswith("/v2?run="), loc
    return loc.split("run=")[1].split("&")[0]


def _ids(run: dict) -> list:
    return [s["id"] for s in run["scenarios"]]


# --- 1. the construct pool is on the run -------------------------------------

def test_the_unrestricted_run_is_exactly_what_it_was(runs_mod):
    """The arms are an addition, not a redefinition. A run with no arm asked for
    is still one encounter per construct in counterbalanced order."""
    run = runs_mod.create("P_FULL", seed=11)
    constructs = [s["construct"] for s in run["scenarios"]]
    assert sorted(constructs) == sorted(runs_mod.CONSTRUCT_ORDER)
    assert run["construct_pool"]["arm"] == "full"
    assert run["construct_pool"]["repeated_constructs"] == []
    assert run["construct_pool"]["parallel_forms_spent"] is False


def test_the_arm_is_recorded_on_the_run_rather_than_inferred(runs_mod):
    """An analyst must be able to read the arm off the run.

    Inferring it from the scenario list works only for as long as the arms
    happen to partition the scenarios, which stops being true the first time a
    form is added — and by then the runs it would misread are already collected.
    """
    run = runs_mod.create("P_ARM", seed=3, arm="one_to_one")
    pool = run["construct_pool"]
    assert pool["arm"] == "one_to_one"
    assert pool["constructs"] == ["conflict_management", "influence"]
    assert pool["encounters"] == 4
    dropped = {d["construct"] for d in pool["excluded_constructs"]}
    assert dropped == {"inspirational_leadership", "teamwork"}
    # And why, not just that: the interaction modes that decided it travel with
    # the verdict, so the record answers the question without the specs to hand.
    for entry in pool["excluded_constructs"]:
        assert entry["interactions"], entry
        assert any("group" in modes for modes in entry["interactions"].values())


def test_the_one_to_one_arm_only_ever_serves_two_person_encounters(runs_mod):
    """The arm's whole claim. Checked against the specs, over many draws."""
    for seed in range(40):
        run = runs_mod.create(f"P_1_{seed}", seed=seed, arm="one_to_one")
        for sc in run["scenarios"]:
            modes = runs_mod._interaction_modes(sc["id"])
            assert modes and all(m == "one_to_one" for m in modes), \
                f"{sc['id']} has {modes}"


def test_the_group_arm_only_ever_serves_encounters_that_open_in_a_group_room(runs_mod):
    for seed in range(40):
        run = runs_mod.create(f"P_G_{seed}", seed=seed, arm="group")
        for sc in run["scenarios"]:
            assert "group" in runs_mod._interaction_modes(sc["id"]), sc["id"]


def test_an_arm_run_is_still_four_distinct_encounters(runs_mod):
    """Four, because that is what the participant is promised and paid for, and
    distinct, because a restricted arm fills four slots from two constructs and
    the obvious way to do that would hand somebody the same conversation twice."""
    for arm in ("one_to_one", "group"):
        for seed in range(40):
            run = runs_mod.create(f"P_D_{arm}{seed}", seed=seed, arm=arm)
            ids = _ids(run)
            assert len(ids) == 4
            assert len(set(ids)) == 4, ids


def test_a_restricted_run_says_which_forms_it_spent_and_which_it_did_not(runs_mod):
    """The cost, recorded on the run that paid it — and no longer overstated.

    Two constructs over four encounters means an arm spends two forms of each in
    attempt 1. While every construct had exactly two forms that was the same
    statement as "no unseen form is left", and parallel_forms_spent was derived
    from `repeated_constructs` alone.

    S3 C made that derivation false. The group arm serves Inspirational
    Leadership twice out of three forms, so one form IS left and the flag said
    otherwise — a run document telling a researcher that a second attempt cannot
    be a parallel-form retest for a construct where it can. The flag is now
    measured against the bank, and the per-construct truth the single bool
    cannot carry is in forms_in_reserve.

    S4 C is the same sentence about the other construct in this arm, and it is
    why this test no longer names Inspirational Leadership alone. Teamwork was
    the two-form construct whose forms the group arm really did spend; it now
    has three and keeps a reserve too. So the assertion is written against the
    BANK — every construct this arm serves twice has exactly the forms it did
    not serve left — rather than against a list of construct names that stops
    being true the next time a form is written.
    """
    run = runs_mod.create("P_SPENT", seed=5, arm="group")
    pool = run["construct_pool"]
    assert pool["repeated_constructs"] == ["inspirational_leadership", "teamwork"]
    # Both constructs in this arm now carry three forms and the arm spends two
    # of each, so each is named with the one form it did not serve.
    from server import scenarios_v3

    bank = scenarios_v3.forms_by_construct()
    reserve = pool["forms_in_reserve"]
    assert set(reserve) == {"inspirational_leadership", "teamwork"}, reserve
    for construct, left in reserve.items():
        expected = sorted(set(bank[construct]) - set(_ids(run)))
        assert sorted(left) == expected, (construct, left, expected)
        assert len(left) == 1, (construct, left)
    assert pool["parallel_forms_spent"] is False, (
        "the run claims every form of every repeated construct is spent while "
        f"still holding {reserve} in reserve")
    # No conversation is repeated, though: repeated construct, different form.
    assert pool["repeated_forms"] == []


def test_a_construct_whose_forms_are_all_spent_says_so(runs_mod):
    """The other half of the same flag, so it is not merely stuck on False.

    There is no longer a two-form construct anywhere in the bank — S2 C gave
    Influence its third, which is what this test used to lean on — so the
    ordinary 1:1 arm now holds a reserve for BOTH of its constructs, and the
    only way to spend a construct out is to make the run fill more slots from it
    than it has forms. Narrowing the arm to one construct does exactly that:
    four slots, three forms, so every form is served and nothing is left.
    """
    one = runs_mod.create("P_SPENT2", seed=5, arm="one_to_one")
    assert set(one["construct_pool"]["forms_in_reserve"]) == {
        "conflict_management", "influence"}
    # Narrow the 1:1 arm to a single construct: four slots from three forms, so
    # all three go out, nothing is held back, and the flag is True again. The
    # price is a repeated conversation, which the run says out loud rather than
    # hiding — see repeated_forms.
    only = runs_mod.create("P_SPENT3", seed=5, arm="one_to_one",
                           constructs=["influence"])
    pool = only["construct_pool"]
    assert pool["repeated_constructs"] == ["influence"]
    assert pool["forms_in_reserve"] == {}
    assert pool["parallel_forms_spent"] is True
    assert pool["repeated_forms"], (
        "four slots drawn from three forms must repeat one, and must say so")


def test_the_reserve_never_offers_a_form_this_run_is_not_allowed_to_serve(runs_mod):
    """A form the composition bars is not in reserve, because a second attempt
    carries the arm over and could not be served it either. The full arm always
    contains Teamwork, so S1 A is barred there — and it must never be named as
    something a second attempt could still draw."""
    for seed in range(40):
        for arm in (None, "one_to_one", "group"):
            run = runs_mod.create(f"P_RSV{arm}{seed}", seed=seed, arm=arm)
            reserve = run["construct_pool"]["forms_in_reserve"]
            ids = set(_ids(run))
            for construct, forms in reserve.items():
                assert not (set(forms) & ids), (construct, forms, ids)
                if "S4A" in ids or "S4B" in ids:
                    assert "S1A" not in forms, (seed, arm, reserve, ids)


def test_a_second_attempt_stays_in_the_arm_and_reaches_what_the_first_held_back(
        runs_mod):
    """The arm carries over, and the reserve is no longer out of reach.

    This test used to be called "...admits the flip is gone" and asserted that
    the reserve existed and could not be served, because create() pinned ONE
    form per construct while a restricted arm gives that construct two slots —
    so the pin went into both slots or neither. create() now takes a per-slot
    SEQUENCE, so attempt 2 serves the unseen form first and a seen one only in
    the slot left over.

    attempt2_forms_available stays False here and that is correct rather than
    stale: it means "every slot of attempt 2 is unseen", and a construct with
    three forms and two slots has one unseen form for two slots, so it cannot
    be. The reach that DID happen is the two fields below.
    """
    first = runs_mod.create("P_SIB", seed=9, arm="one_to_one")
    second = runs_mod.sibling_run(first["run_id"])
    pool = second["construct_pool"]
    assert pool["arm"] == "one_to_one"
    assert pool["attempt1_run_id"] == first["run_id"]
    # Not every slot is unseen — two slots per construct, one unseen form each —
    # and the bool says so rather than being loosened into a half-truth.
    assert pool["attempt2_forms_available"] is False
    # Still four separate conversations, not one repeated.
    assert len(set(_ids(second))) == 4
    # The reserve attempt 1 left is carried here rather than left to be
    # rediscovered: both constructs of this arm have three forms and the arm
    # served two of each.
    from server import scenarios_v3

    bank = scenarios_v3.forms_by_construct()
    expected = {c: sorted(set(bank[c]) - set(_ids(first)))
                for c in ("conflict_management", "influence")}
    assert pool["attempt1_forms_in_reserve"] == expected
    # And it was actually SERVED. This is the whole point of the per-slot pin:
    # every form attempt 1 held back is in attempt 2.
    held = sorted({f for forms in expected.values() for f in forms})
    assert pool["attempt1_reserve_served"] == held, (
        pool["attempt1_reserve_served"], held)
    assert pool["attempt2_unseen_forms"] == expected, pool["attempt2_unseen_forms"]


def test_a_second_attempt_on_a_full_run_still_flips_the_forms(runs_mod):
    """The unrestricted pre/post path is untouched by any of this."""
    first = runs_mod.create("P_SIB2", seed=4)
    second = runs_mod.sibling_run(first["run_id"])
    assert second["construct_pool"]["attempt2_forms_available"] is True
    assert set(first["variants"]) == set(second["variants"])
    # Every construct genuinely changes form. `parallel_form` is a scalar and
    # a scalar that names one of three reads as naming the only one, so the
    # target is computed from the bank; this is what says it worked.
    assert all(second["variants"][c] != sid
               for c, sid in first["variants"].items()), (first["variants"],
                                                          second["variants"])


def test_a_second_attempt_never_flips_into_the_pairing_the_first_one_avoided(runs_mod):
    """The defect a third form was written to close, pinned so it cannot come back.

    A full run always contains Teamwork, so S1 A is excluded from attempt 1 and
    attempt 1 is served S1 B. S1 B's `parallel_form` field is S1 A. The flip was
    read off that field and passed to create() as a PIN, and
    _apply_form_exclusions leaves a pinned slot alone by design — so attempt 2
    was handed exactly the pairing attempt 1 was built to avoid, in 120 of 120
    sibling runs measured before this. sibling_run's own docstring said the
    construct fell back to attempt 1's form instead; it did not.
    """
    from server import scenarios_v3

    teamwork = set(scenarios_v3.forms_by_construct()["teamwork"])
    for seed in range(60):
        first = runs_mod.create(f"P_FLIP{seed}", seed=seed)
        second = runs_mod.sibling_run(first["run_id"])
        ids = _ids(second)
        # A full run always has Teamwork. Read off the bank, not spelled out:
        # this line was `"S4A" in ids or "S4B" in ids` and went stale the day
        # S4 C was written, which is the same hardcoding the exclusion table
        # itself was written to avoid.
        assert teamwork & set(ids), (ids, sorted(teamwork))
        assert "S1A" not in ids, (seed, _ids(first), ids)
        # And it is a real flip, not a repeat of attempt 1's conflict form.
        assert (set(_ids(first)) & {"S1A", "S1B", "S1C"}
                != set(ids) & {"S1A", "S1B", "S1C"}), (seed, _ids(first), ids)


def test_an_arm_nobody_defined_is_refused(runs_mod):
    """Rather than quietly widening the pool, which would hand the participant
    the wrong arm and record it as though it were the right one."""
    with pytest.raises(ValueError):
        runs_mod.create("P_BAD", arm="one-to-one-ish")


def test_a_restriction_that_leaves_nothing_is_refused(runs_mod):
    with pytest.raises(ValueError):
        runs_mod.create("P_EMPTY", arm="one_to_one", constructs=["teamwork"])
    with pytest.raises(ValueError):
        runs_mod.create("P_UNKNOWN", constructs=["telepathy"])


@pytest.mark.usefixtures("per_slot_draw")
def test_the_cross_construct_exclusion_still_holds_on_a_full_run(runs_mod):
    """S1 A must not share a run with Teamwork. Unchanged by the rewrite that
    made the exclusion pass work per encounter rather than per construct."""
    for seed in range(60):
        ids = _ids(runs_mod.create(f"P_X{seed}", seed=seed))
        if any(i.startswith("S4") for i in ids):
            assert "S1A" not in ids, (seed, ids)


@pytest.mark.usefixtures("per_slot_draw")
def test_the_one_to_one_arm_is_the_only_place_s1a_is_served_at_all(runs_mod):
    """The exclusion that keeps S1 A away from Teamwork is a statement about
    runs that contain Teamwork. Every FULL run contains Teamwork, so S1 A is
    served in no full run — measured below, 0 of 200 — and the 1:1 arm, which
    contains no Teamwork, is the only place it reaches a participant at all.

    This test used to assert `{"S1A", "S1B"} <= ids`: with two forms for two
    slots the arm served both to everyone. A third form makes it five forms for
    four slots, so the arm now draws two of three Conflict Management forms and
    S1 A reaches about two participants in three rather than all of them. That
    is the point of writing one — but it also means this arm's Conflict
    Management sample is drawn from a different scenario mix than the full
    study's, and anyone pooling the two still has to know it.
    """
    served = {}
    for seed in range(200):
        ids = set(_ids(runs_mod.create(f"P_S1A{seed}", seed=seed, arm="one_to_one")))
        # Two of the three, never one and never three: the arm has two slots for
        # this construct and draws without replacement inside it.
        conflict = ids & {"S1A", "S1B", "S1C"}
        assert len(conflict) == 2, (seed, ids)
        for sid in conflict:
            served[sid] = served.get(sid, 0) + 1
    assert set(served) == {"S1A", "S1B", "S1C"}, served
    # Not "about a third each" — the draw is skewed by the exclusion pass, which
    # replaces an excluded form with the first permitted one rather than
    # redrawing. What must hold is that no form is rare enough to be a
    # curiosity and none is universal any more.
    for sid, n in served.items():
        assert 100 <= n <= 180, (sid, n, served)

    full_with_s1a = sum(
        1 for seed in range(200)
        if "S1A" in _ids(runs_mod.create(f"P_S1AF{seed}", seed=seed)))
    assert full_with_s1a == 0, full_with_s1a


@pytest.mark.usefixtures("per_slot_draw")
def test_the_unrestricted_arm_now_has_a_conflict_management_contrast(runs_mod):
    """What S1 C was written for, stated as the fact it is.

    FORM_EXCLUSIONS bars S1 A from any run containing Teamwork and every full
    run contains Teamwork, so before S1 C existed the unrestricted arm served
    exactly one Conflict Management form — S1 B, to every participant. A
    construct measured on one form in the arm that carries the whole study has
    no A/B contrast at all: no form effect can be estimated and no participant
    can be given an unseen form on a second attempt.
    """
    seen = {}
    for seed in range(200):
        for sid in set(_ids(runs_mod.create(f"P_CTR{seed}", seed=seed))):
            if sid.startswith("S1"):
                seen[sid] = seen.get(sid, 0) + 1
    assert set(seen) == {"S1B", "S1C"}, seen
    # Both forms reach a usable share. The split is uneven and that is the
    # exclusion pass showing through, not a bug: a draw that lands on S1 A is
    # corrected to the first permitted form, which is S1 B, so S1 B collects
    # both its own draws and the corrected ones.
    assert min(seen.values()) >= 40, seen


# --- 2. three routes, all Qualtrics-shaped -----------------------------------

def test_all_three_participant_links_answer(client):
    # Keys of six characters or more throughout this file, because that is what
    # runs._PARTICIPANT_KEY_RE accepts and the entry gate now asks the same
    # question of the value that the enrolment path does. A shorter key was
    # always going to produce an `unattributed` run; it now also gets the entry
    # check page first, so a test using one would be testing the broken-pipe
    # path while looking like it tested the normal one.
    for path in PARTICIPANT_LINKS:
        r = client.get(path, params={"pid": f"LINKKEY{path.count('/')}{len(path)}"},
                       follow_redirects=False)
        assert r.status_code == 307, (path, r.text)
        assert r.headers["location"].startswith("/v2?run=")


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_each_link_records_its_own_arm(client, runs_mod, path):
    r = client.get(path, params={"pid": "ARMKEY" + str(abs(hash(path)) % 997)},
                   follow_redirects=False)
    run = runs_mod.get(_run_id_from(r))
    assert run["construct_pool"]["arm"] == ARM_LINKS[path]
    assert len(run["scenarios"]) == 4


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
@pytest.mark.parametrize("spelling", ["pid", "participant_id", "PROLIFIC_PID"])
def test_every_link_accepts_every_participant_key_spelling(client, runs_mod,
                                                           path, spelling):
    """The spellings are declared once and depended on by all three routes. If
    one route ever grows its own signature, this is what goes red."""
    key = f"SP{spelling[:3]}{abs(hash(path)) % 97}"
    r = client.get(path, params={spelling: key}, follow_redirects=False)
    run = runs_mod.get(_run_id_from(r))
    assert run["participant_id"] == key
    assert run["cohort"] == "study"
    assert run["participant_key_status"] == "ok"


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
@pytest.mark.parametrize("broken", [
    "${e://Field/ParticipantKey}",
    "",
    "ParticipantKey",
])
def test_a_broken_pipe_is_refused_identically_on_every_link(client, runs_mod,
                                                            path, broken):
    """The failure this prevents is two participants landing in one run.

    An unreplaced ${e://Field/...} is the same string for everybody, so taking
    it at face value put arrival two inside arrival one's half-finished run. Two
    arrivals with the same broken value must produce two runs, both marked
    unattributable, on every link — not on /start alone.

    POSTed rather than GOT: a key this server cannot use now gets the entry
    check page on a GET, because the raw template link carries exactly these
    values and every fetch of it was minting a run. These are the Continue
    presses behind that page. Two arrivals, two presses, two runs — and note
    there is no ?qid= here, so there is nothing that could legitimately join
    them either (see test_entry_idempotency.py for the case where there is).
    """
    first = client.post(path, params={"pid": broken}, follow_redirects=False)
    second = client.post(path, params={"pid": broken}, follow_redirects=False)
    a, b = runs_mod.get(_run_id_from(first)), runs_mod.get(_run_id_from(second))
    assert a["run_id"] != b["run_id"]
    for run in (a, b):
        assert run["cohort"] == "unattributed"
        assert run["participant_key_status"] != "ok"
        assert run["participant_id"].startswith("unattributed_")
        # The raw value survives for a hand-join, and the arm is still recorded.
        assert run["raw_participant_key"] == (broken or None)
        assert run["construct_pool"]["arm"] == ARM_LINKS[path]


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_a_returning_participant_resumes_rather_than_forking(client, runs_mod, path):
    """Participants close tabs and come back. A second run under the same key is
    a second partial record and a participant who starts the study again."""
    key = f"RESUME{abs(hash(path)) % 89}"
    first = client.get(path, params={"pid": key}, follow_redirects=False)
    second = client.get(path, params={"pid": key}, follow_redirects=False)
    assert _run_id_from(first) == _run_id_from(second)


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_every_link_carries_the_qualtrics_response_id_through(client, runs_mod, path):
    key = f"QIDKEY{abs(hash(path)) % 83}"
    r = client.get(path, params={"pid": key, "qid": "R_0123456789abcde"},
                   follow_redirects=False)
    assert runs_mod.get(_run_id_from(r))["qualtrics_id"] == "R_0123456789abcde"


def test_the_other_arm_does_not_quietly_resume_the_first_one(client, runs_mod, capsys):
    """The one place the arms interact.

    The same key arriving at the group link after a 1:1 run is either a
    participant taking both blocks or an operator mistake, and this route cannot
    tell which. Resuming would mean the group link served a 1:1 run and recorded
    it as one, which nothing downstream could ever notice. A second run, and
    both of them saying the other exists, keeps either reading recoverable.
    """
    key = "BOTHARMS1"
    one = runs_mod.get(_run_id_from(
        client.get("/start/one-to-one", params={"pid": key}, follow_redirects=False)))
    grp = runs_mod.get(_run_id_from(
        client.get("/start/group", params={"pid": key}, follow_redirects=False)))
    assert one["run_id"] != grp["run_id"]
    assert one["construct_pool"]["arm"] == "one_to_one"
    assert grp["construct_pool"]["arm"] == "group"
    # Cross-linked in both directions: either run is the one somebody is looking
    # at, and the participant key they share is the field that is null on an
    # unattributed run.
    one = runs_mod.get(one["run_id"])
    assert [l["run_id"] for l in one["other_arm_runs"]] == [grp["run_id"]]
    assert [l["run_id"] for l in grp["other_arm_runs"]] == [one["run_id"]]
    assert "rather than resuming the other arm" in capsys.readouterr().out


def test_the_three_links_share_one_parameter_declaration(client):
    """The anti-drift guard, and the reason this file exists.

    Three hand-written handlers would have been identical on the day they were
    written. The one that gets edited later is the one whose participant-key
    validation goes missing, and the symptom does not appear until analysis.
    """
    import inspect

    endpoints = {}
    for route in appmod.app.routes:
        if getattr(route, "path", None) in PARTICIPANT_LINKS:
            endpoints[route.path] = route.endpoint
    assert set(endpoints) == set(PARTICIPANT_LINKS)
    for path, fn in endpoints.items():
        params = list(inspect.signature(fn).parameters.values())
        assert len(params) == 1, f"{path} grew its own signature: {params}"
        assert params[0].default.dependency is appmod.entry_params, path


def test_an_arm_that_cannot_be_built_says_so_instead_of_500ing(client, monkeypatch):
    """If the scenario specs an arm needs go missing, the operator has to be
    told which arm and why — not handed a traceback from a draw."""
    from server import runs

    monkeypatch.setattr(runs, "_resolve_pool",
                        lambda arm, constructs: (_ for _ in ()).throw(
                            ValueError("no construct to draw from")))
    r = client.get("/start/group", params={"pid": "NOARM1"}, follow_redirects=False)
    assert r.status_code == 503
    assert "group" in r.json()["detail"]


# --- 3. four encounters, then a code -----------------------------------------

@pytest.mark.parametrize("arm", [None, "one_to_one", "group"])
def test_four_encounters_then_a_finished_code(runs_mod, arm):
    """The advance path and the code, on each arm.

    The code is an assertion about a number of completed encounters, and the arm
    changes which constructs fill them — not how many, and not what the code
    means.
    """
    run = runs_mod.create(f"P_CODE_{arm}", seed=2, arm=arm)
    assert runs_mod.completion_code(run).startswith("RF-PARTIAL-")
    for i in range(4):
        view = runs_mod.view(runs_mod.get(run["run_id"]))
        assert view["position"] == i + 1 and view["total"] == 4
        assert view["done"] is False
        assert view["arm"] == (arm or "full")
        run = runs_mod.advance(run["run_id"], session_id=f"s_17724603{i:02d}_aaaaaa")
    view = runs_mod.view(run)
    assert view["done"] is True
    assert len(run["completed"]) == 4
    code = view["completion_code"]
    assert code.startswith("RF-") and "PARTIAL" not in code


@pytest.mark.parametrize("arm", [None, "one_to_one", "group"])
def test_stopping_early_still_earns_a_partial_code_that_is_not_the_finished_one(
        runs_mod, arm):
    """Somebody who stops after two encounters has still given us their time,
    and the partial code is what they take back to the survey to be paid. It
    must not be the finished code with a word in front of it: the digest is over
    the finished state as well as the run id, so deleting "PARTIAL-" from the
    string yields a code that verifies as nothing.
    """
    run = runs_mod.create(f"P_PART_{arm}", seed=6, arm=arm)
    runs_mod.advance(run["run_id"], session_id="s_1772460400_bbbbbb")
    run = runs_mod.advance(run["run_id"], session_id="s_1772460401_bbbbbb")
    partial = runs_mod.completion_code(run)
    assert partial.startswith("RF-PARTIAL-")
    run["index"] = len(run["scenarios"])
    finished = runs_mod.completion_code(run)
    assert finished != "RF-" + partial[len("RF-PARTIAL-"):]


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_the_run_view_the_page_fetches_knows_the_arm(client, path):
    key = f"VIEWKEY{abs(hash(path)) % 79}"
    r = client.get(path, params={"pid": key}, follow_redirects=False)
    run_id = _run_id_from(r)
    view = client.get(f"/api/run/{run_id}").json()
    assert view["total"] == 4
    assert view["arm"] == ARM_LINKS[path]
    assert view["completion_code"].startswith("RF-PARTIAL-")


def test_the_export_carries_the_arm_as_a_column(client, runs_mod):
    """Analysis groups by arm, so the arm is flat in the export next to the
    record that says what the arm cost."""
    client.get("/start/group", params={"pid": "EXPORT1"}, follow_redirects=False)
    rows = client.get("/api/runs").json()
    row = [r for r in rows if r["participant_id"] == "EXPORT1"][0]
    assert row["arm"] == "group"
    # The flag and the detail behind it both travel. The flag alone is one bool
    # over the whole run; forms_in_reserve is the per-construct truth, and with
    # three forms in both of this arm's constructs it now names both of them —
    # which is exactly why the bool is not enough to export on its own.
    assert row["construct_pool"]["parallel_forms_spent"] is False
    assert set(row["construct_pool"]["forms_in_reserve"]) == {
        "inspirational_leadership", "teamwork"}


# --- 4. the rating console link ----------------------------------------------
