"""What decides a run's shape, and who is allowed to decide it.

Three of the four things that make a run either usable evidence or silent junk
are set at the door, and each of them was reachable by whoever held the link:

  * `?variant=` pins the form of every construct. Round two gated `?cohort=`
    behind the operator key in the same handler and left this one open, so a
    stray letter in a Qualtrics redirect served the forbidden S1 A + Teamwork
    pairing to 200 runs in 200 — more than the 94 in 200 the round-two fix was
    built to eliminate — and wrote "form was pinned by the caller; exclusion not
    applied" onto every one of them. On the two arm links it did something
    simpler and worse: it served the participant the SAME ENCOUNTER TWICE.

  * `cohort` decides whether a run is study data. It was resolved at encounter
    time by scanning for the newest run pointing at the participant record, so
    an encounter's cohort was not a property of the encounter at all and could
    be changed by anything that happened afterwards — including one
    unauthenticated POST. And the VALUE was never normalised, so 'Internal',
    ' internal ' and 'banana' all landed on runs and every consumer's `==`
    dropped them out of both `?cohort=study` and `?cohort=internal`.

  * a required environment variable decides whether any consent can be recorded
    at all. `storage.missing_required_env()` existed to say so and was wired to
    nothing: no startup hook called it, /health did not publish it, and the
    first evidence of the misconfiguration was an empty dataset.

These tests are about the decisions, not the routes: who may make them, what
values they may take, and whether they can be re-made later.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server import storage


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


REPO_ROOT = Path(__file__).resolve().parent.parent
OPERATOR_KEY = "test-session-key"

#: The pairing FORM_EXCLUSIONS exists to prevent: S1 A and Teamwork in one run.
FORBIDDEN_FORM = "S1A"
FORBIDDEN_WITH = "teamwork"


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def store(tmp_path, monkeypatch, runs_mod):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    storage.init_storage()
    return storage


@pytest.fixture()
def guarded(store, monkeypatch):
    """A deployment with the researcher key configured, which is production."""
    monkeypatch.setattr(appmod, "SESSION_KEY", OPERATOR_KEY, raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


@pytest.fixture()
def open_client(store, monkeypatch):
    """No SESSION_KEY: a local checkout, where the whole app is open by design."""
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


def _run_from(response, runs_mod):
    assert response.status_code == 307, response.text
    run_id = response.headers["location"].split("run=")[1].split("&")[0]
    return runs_mod.get(run_id)


def _ids(run):
    return [s["id"] for s in run["scenarios"]]


def _bad_pairing(run) -> bool:
    ids = {s["id"] for s in run["scenarios"]}
    constructs = {s["construct"] for s in run["scenarios"]}
    return FORBIDDEN_FORM in ids and FORBIDDEN_WITH in constructs


# =============================================================================
# ?variant= is a study parameter, not a participant one
# =============================================================================

@pytest.mark.usefixtures("per_slot_draw")
def test_a_participant_supplied_variant_is_ignored(guarded, runs_mod):
    """Measured the way the round-two fix was measured, and it is the same
    measurement that showed this one was still open: `?variant=A` with no
    researcher key produced the forbidden pairing on every run it touched, and
    stamped the run with an explanation naming a caller who was a stray query
    parameter."""
    run = _run_from(
        guarded.get("/start", params={"pid": "RF_VAR_1", "variant": "A",
                                      "qid": "R_1"}, follow_redirects=False),
        runs_mod)
    assert not _bad_pairing(run), (
        f"a keyless ?variant=A served {_ids(run)}, which is the S1 A + Teamwork "
        f"pairing the instrument forbids")
    assert not [e for e in run["form_exclusions"]
                if e.get("reason", "").startswith("form was pinned")], (
        "the run records a pin nobody with the researcher key asked for")


def test_the_operator_may_still_pin_a_variant(guarded, runs_mod):
    """Positive control. Piloting one form is a real thing the lab does, and it
    is the reason the parameter exists; what changed is who may ask."""
    run = _run_from(
        guarded.get("/start", params={"pid": "RF_VAR_2", "variant": "A",
                                      "qid": "R_2", "key": OPERATOR_KEY},
                    follow_redirects=False),
        runs_mod)
    assert all(s["variant"].upper() == "A" for s in run["scenarios"]), _ids(run)


def test_an_operator_pinned_run_is_not_study_data_unless_asked_for(guarded,
                                                                   runs_mod):
    """The containment /test had and /start did not.

    Pinning every construct to one letter is what makes _apply_form_exclusions
    stand down — the slot is marked `pinned` and the pass leaves a pinned slot
    alone by design — so `?variant=A` on a full run serves the S1 A + Teamwork
    pairing the instrument forbids: 300 of 300 seeds, each stamped "form was
    pinned by the caller; exclusion not applied". That is a legitimate operator
    action and the pin is still honoured (the test above is the positive
    control). What it must not do is default into the STUDY cohort, which is how
    a deliberately non-valid pairing reaches the analysis set with an
    explanation attached. /test forces cohort="internal" for exactly this
    reason; this is the same containment on the other door.
    """
    run = _run_from(
        guarded.get("/start", params={"pid": "RF_VAR_2B", "variant": "A",
                                      "qid": "R_2B", "key": OPERATOR_KEY},
                    follow_redirects=False),
        runs_mod)
    assert all(s["variant"].upper() == "A" for s in run["scenarios"]), _ids(run)
    assert run["cohort"] == "internal", (
        f"a pinned-form run landed in cohort {run['cohort']!r}; the assignment "
        f"{_ids(run)} would be in the study dataset")
    # And an operator who really means it can still say so.
    said = _run_from(
        guarded.get("/start", params={"pid": "RF_VAR_2C", "variant": "A",
                                      "qid": "R_2C", "cohort": "study",
                                      "key": OPERATOR_KEY},
                    follow_redirects=False),
        runs_mod)
    assert said["cohort"] == "study"


def test_a_local_checkout_still_honours_the_variant(open_client, runs_mod):
    """With no SESSION_KEY configured the whole app is open, and this parameter
    behaves like every other operator affordance rather than inventing its own
    rule (see app._operator_key)."""
    run = _run_from(
        open_client.get("/start", params={"pid": "RF_VAR_3", "variant": "A"},
                        follow_redirects=False),
        runs_mod)
    assert all(s["variant"].upper() == "A" for s in run["scenarios"]), _ids(run)


def test_an_unusable_variant_letter_is_still_refused_for_the_operator(guarded):
    """The round-one refusal stays: a letter no form carries is an operator
    mistake in a link, and it must fail where the link is handed out."""
    r = guarded.get("/start", params={"pid": "RF_VAR_4", "variant": "Z",
                                      "key": OPERATOR_KEY},
                    follow_redirects=False)
    assert r.status_code == 400, r.text


@pytest.mark.usefixtures("per_slot_draw")
def test_an_unusable_variant_letter_never_turns_a_participant_away(guarded,
                                                                   runs_mod):
    """And a participant carrying the same broken link is not refused at the
    door mid-study: the parameter is not theirs to set, so it is ignored, the
    way a stray ?cohort= already is. A 400 here costs the encounter outright."""
    r = guarded.get("/start", params={"pid": "RF_VAR_5", "variant": "Z",
                                      "qid": "R_5"}, follow_redirects=False)
    assert r.status_code == 307, r.text
    assert not _bad_pairing(_run_from(r, runs_mod))


def test_pinning_a_variant_on_the_full_run_still_pins_it(runs_mod):
    """Positive control for the duplicate fix: where a construct has one slot,
    a pin is still a pin and still honoured exactly."""
    for seed in range(50):
        run = runs_mod.create(f"RF_PIN_{seed}", variant="B", seed=seed)
        assert all(s["variant"].upper() == "B" for s in run["scenarios"]), \
            _ids(run)


# =============================================================================
# cohort is decided once, at the start, and recorded
# =============================================================================

def test_an_unauthenticated_caller_cannot_mint_a_study_run(guarded, runs_mod):
    """Step 2 of the reproduction. POST /api/run is the second door to run
    creation, it has no client in this repository, and it minted cohort="study"
    for anybody who could reach the port."""
    r = guarded.post("/api/run", json={"participant_id": "CR9ZZ99Z"})
    assert r.status_code == 401, r.text
    assert list(runs_mod.RUNS_DIR.glob("*.json")) == [], (
        "an unauthenticated POST /api/run minted a run anyway")

    r = guarded.post("/api/run", params={"key": OPERATOR_KEY},
                     json={"participant_id": "CR9ZZ99Z"})
    assert r.status_code == 200, r.text
    assert r.json()["cohort"] == "study"


def test_a_study_encounter_still_resolves_its_own_run(open_client, runs_mod,
                                                      store):
    """Positive control: the ordinary /start arrival still joins its encounter
    to its run, its cohort and its participant key."""
    r = open_client.get("/start", params={"pid": "RF_CTX_1", "qid": "R_ctx"},
                        follow_redirects=False)
    loc = r.headers["location"]
    pid = loc.split("participant_id=")[1].split("&")[0]
    run_id = loc.split("run=")[1].split("&")[0]
    ctx = appmod._run_context(pid)
    assert ctx == {"run_id": run_id, "cohort": "study",
                   "participant_key": "RF_CTX_1", "encounter_index": 1}, ctx


# --- the cohort value --------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("internal", "internal"),
    ("Internal", "internal"),
    ("INTERNAL", "internal"),
    ("  internal  ", "internal"),
    ("internal\n", "internal"),
    ("Study", "study"),
    ("UNATTRIBUTED", "unattributed"),
])
def test_a_cohort_value_is_normalised_before_it_reaches_a_run(runs_mod, raw,
                                                              want):
    """Every consumer compares with `==`, so a spelling that is not the
    canonical one drops out of BOTH `?cohort=study` and `?cohort=internal` — the
    run is neither excluded nor included, it is invisible. Round two gated the
    caller and not the string."""
    run = runs_mod.create(f"RF_COH_{want}_{abs(hash(raw)) % 10 ** 6}",
                          cohort=raw)
    assert run["cohort"] == want


def test_a_cohort_nobody_defined_is_refused_rather_than_stored(runs_mod):
    """'banana' is not a cohort, and storing it produces a run that no filter
    can name and no analyst will ever see. Refused where it is handed in, like
    an unknown arm and an unknown variant letter."""
    with pytest.raises(ValueError):
        runs_mod.create("RF_COH_BAD", cohort="banana")
    assert runs_mod.known_cohorts() == ("study", "internal", "unattributed")


def test_an_operator_mistyping_a_cohort_is_told_rather_than_ignored(guarded):
    """The operator holds the key, so this is their link and their typo, and it
    silently voids every run the link creates. They get a 400 that names it."""
    r = guarded.get("/start", params={"pid": "RF_COH_1", "cohort": "banana",
                                      "key": OPERATOR_KEY},
                    follow_redirects=False)
    assert r.status_code == 400, r.text
    assert "banana" in r.text


def test_a_stray_cohort_on_a_participants_link_is_still_only_ignored(guarded,
                                                                     runs_mod):
    """Positive control, and the line the 400 above must not cross: a recruited
    person carrying a link with junk in it is mid-study, and refusing them at
    the door costs the encounter outright."""
    r = guarded.get("/start", params={"pid": "RF_COH_2", "cohort": "banana",
                                      "qid": "R_c2"}, follow_redirects=False)
    assert r.status_code == 307, r.text
    assert _run_from(r, runs_mod)["cohort"] == "study"


def test_the_internal_door_still_tags_its_runs_internal(open_client, runs_mod):
    """Positive control for the normalisation: /test's own tag is unchanged."""
    r = open_client.get("/test", params={"name": "demo-lab"},
                        follow_redirects=False)
    assert _run_from(r, runs_mod)["cohort"] == "internal"


# =============================================================================
# a required variable has to be visible before the wave, not after it
# =============================================================================

def test_the_preflight_is_silent_when_the_deployment_is_configured(monkeypatch,
                                                                   capsys):
    """Positive control: a configured deployment does not get a warning it has
    to learn to ignore."""
    for name in storage.REQUIRED_ENV:
        monkeypatch.setenv(name, "cornell-irb-2026-09-v3")
    appmod.run_preflights()
    assert "required environment" not in capsys.readouterr().out


def test_the_general_rule_is_general(monkeypatch):
    """The docstring promises "the next variable somebody makes mandatory is
    caught the day it is made mandatory". It was one module's dict read by one
    function in that module; any module under server/ may declare REQUIRED_ENV
    and the operator-facing test already reads all of them."""
    module = type(storage)("server._required_env_probe")
    module.REQUIRED_ENV = {"RF_PROBE_REQUIRED_ENV": "a probe, not a real one"}
    import sys

    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.delenv("RF_PROBE_REQUIRED_ENV", raising=False)
    assert "RF_PROBE_REQUIRED_ENV" in storage.missing_required_env()
    monkeypatch.setenv("RF_PROBE_REQUIRED_ENV", "answered")
    assert "RF_PROBE_REQUIRED_ENV" not in storage.missing_required_env()


# --- what counts as an answer ------------------------------------------------
