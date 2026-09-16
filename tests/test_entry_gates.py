"""What the entry path lets through, and what it must not.

Six defects sit behind the three participant links and the internal test
entrance, and they share a shape: a parameter or a state that the entry code
reads but does not believe, so the run document ends up asserting something
that is not true of the person who arrived.

  * a withdrawal recorded against one run document rather than against the
    person, so a second tab (or the other arm's link) carried on recording
    somebody who had pressed stop;
  * a same-arm return mistaken for a cross-arm arrival, which forked the
    participant's finished run and stranded their completion code;
  * a ?variant= letter no form carries, taken as a deliberate pin and used to
    switch off the discriminant-validity exclusion for a whole wave;
  * a ?cohort= supplied by whoever holds the link, which decides whether the
    run is study data at all;
  * an internal test entrance that reaches live audio and webcam capture in
    three unauthenticated requests.

These tests are about the entrances, in the manner of tests/test_links.py: what
arrives, what is written, and what is refused.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    """server.runs writing into a temp directory (see tests/test_links.py)."""
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def store(tmp_path, monkeypatch, runs_mod):
    """server.storage writing into the same temp directory."""
    from server import storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    storage.init_storage()
    return storage


@pytest.fixture()
def client(store, monkeypatch):
    """A TestClient whose storage and runs are entirely inside tmp_path."""
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


def _run_id_from(response) -> str:
    # 307 for a GET arrival, 303 for the entry check page's Continue POST — the
    # POST must NOT be preserved, because a preserved POST lands on /v2, which
    # serves GET only. Both are arrivals; which one a test gets depends only on
    # whether the link carried a key this server could use.
    assert response.status_code in (307, 303), response.text
    loc = response.headers["location"]
    assert loc.startswith("/v2?run="), loc
    return loc.split("run=")[1].split("&")[0]


def _enter(client, path, pid, **params) -> str:
    """Arrive on one of the participant links and return the run id."""
    q = {"pid": pid}
    q.update(params)
    return _run_id_from(client.get(path, params=q, follow_redirects=False))


def _ids(run: dict) -> list:
    return [s["id"] for s in run["scenarios"]]


def _all_runs(runs_mod) -> list:
    if not runs_mod.RUNS_DIR.exists():
        return []
    return [json.loads(f.read_text(encoding="utf-8"))
            for f in runs_mod.RUNS_DIR.glob("*.json")]


def _consented_arrival(store, runs_mod, key="RF_GATE_1", arm=None):
    """One participant as the study leaves them: a run, and a consented record.

    Built through runs.create and storage.create_participant because the join
    the capture gate turns on (run.participant_record_id) is the one /start
    writes, and a hand-made pair could quietly stop matching it.
    """
    run = runs_mod.create(key, qualtrics_id=f"R_{key}", cohort="study", arm=arm)
    pid = store.create_participant(code=key, consent_given=True,
                                   consent_version="v1")
    run["participant_record_id"] = pid
    runs_mod.save(run)
    return run, pid


# --- B2: a withdrawal has to reach the capture socket ------------------------

def test_a_withdrawn_participant_is_refused_by_the_capture_gate(store, runs_mod):
    """The promise in the consent text is that they may stop at any time.

    Before this, the gate asked the participant RECORD alone. Withdrawing set
    `withdrawn` on the run and nothing on the record, so the record still read
    consented, the gate still passed, and the socket that opens the microphone
    and the webcam still opened — for someone who had pressed stop. advance()
    refused them a completion code afterwards, but the recording had already
    happened, which is the part no later refusal undoes.
    """
    run, pid = _consented_arrival(store, runs_mod)
    assert appmod._consented_participant(pid) is not None, "setup"

    runs_mod.withdraw(run["run_id"])

    assert appmod._consented_participant(pid) is None


def test_the_voice_socket_closes_on_a_withdrawn_participant(client, store,
                                                            runs_mod, monkeypatch):
    """The same thing end to end, on the socket that actually captures."""
    run, pid = _consented_arrival(store, runs_mod, key="RF_GATE_WS")

    def no_such_scenario(*a, **k):
        raise FileNotFoundError("unknown scenario: conflict")

    monkeypatch.setattr(appmod.registry, "create", no_such_scenario)
    # Positive control: before the withdrawal this socket opens, so a 4403
    # below is the withdrawal being honoured and not a broken fixture.
    with client.websocket_connect(
            f"/ws/participant/voice?scenario=conflict&participant_id={pid}") as ws:
        assert ws.receive_json()["type"] == "error"

    runs_mod.withdraw(run["run_id"])

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
                f"/ws/participant/voice?scenario=conflict&participant_id={pid}"):
            pass
    assert caught.value.code == 4403


def test_the_text_socket_refuses_a_withdrawn_participant_too(client, store,
                                                             runs_mod, monkeypatch):
    """A transcript opened under a withdrawn record is still study data: it
    resolves the run, carries cohort "study" and is queued to a human rater."""
    run, pid = _consented_arrival(store, runs_mod, key="RF_GATE_TXT")

    def no_such_scenario(*a, **k):
        raise FileNotFoundError("unknown scenario: conflict")

    monkeypatch.setattr(appmod.registry, "create", no_such_scenario)
    runs_mod.withdraw(run["run_id"])

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
                f"/ws/participant?scenario=conflict&participant_id={pid}"):
            pass
    assert caught.value.code == 4403


def test_the_anonymous_text_entrance_still_opens(client, monkeypatch):
    """The withdrawal check must not widen the gate it sits behind: the
    documented single-agent text entrance opens with no participant_id."""
    def no_such_scenario(*a, **k):
        raise FileNotFoundError("unknown scenario: conflict")

    monkeypatch.setattr(appmod.registry, "create", no_such_scenario)
    with client.websocket_connect("/ws/participant?scenario=conflict") as ws:
        assert ws.receive_json()["type"] == "error"


# --- B7: a withdrawal is about the person, not one run document --------------

def test_a_withdrawal_reaches_every_run_the_person_has(runs_mod):
    """Two arms, one person, one decision to stop."""
    one = runs_mod.create("RF_W_BOTH", arm="one_to_one")
    group = runs_mod.create("RF_W_BOTH", arm="group")

    runs_mod.withdraw(one["run_id"])

    assert runs_mod.get(one["run_id"])["withdrawn"]
    assert runs_mod.get(group["run_id"])["withdrawn"], \
        "the other arm's run still reads as live for someone who withdrew"


def test_the_other_arm_link_does_not_re_enrol_someone_who_withdrew(client, runs_mod):
    """The reproduction, exactly: withdraw on one arm, touch the other arm's
    link, reopen the first. That used to mint a FRESH run with withdrawn null
    and four encounters queued, so /api/runs showed the same person withdrawn
    and live at once and a withdrawal report read "withdrew and then carried
    on"."""
    first = _enter(client, "/start/one-to-one", "RF_W_ARMS")
    # The record id /start minted, which is what the page sends with the stop.
    assert client.post(
        f"/api/run/{first}/withdraw",
        json={"participant_id": runs_mod.get(first)["participant_record_id"]},
    ).status_code == 200

    second = _enter(client, "/start/group", "RF_W_ARMS")
    third = _enter(client, "/start/one-to-one", "RF_W_ARMS")

    assert third == first, "the first arm forked a fresh run after a withdrawal"
    ids = {r["run_id"] for r in _all_runs(runs_mod)}
    assert ids == {first}, f"a withdrawn participant was given new runs: {ids}"
    for run_id in (first, second, third):
        assert runs_mod.get(run_id)["withdrawn"], run_id


def test_no_run_of_a_withdrawn_participant_reads_as_live(client, runs_mod):
    """What an analyst sees. Every run under the key says withdrawn, or the
    withdrawal report contradicts itself."""
    _enter(client, "/start/one-to-one", "RF_W_LIVE")
    first = _enter(client, "/start/one-to-one", "RF_W_LIVE")
    client.post(f"/api/run/{first}/withdraw",
                json={"participant_id": runs_mod.get(first)["participant_record_id"]})
    _enter(client, "/start/group", "RF_W_LIVE")
    _enter(client, "/start", "RF_W_LIVE")

    mine = [r for r in _all_runs(runs_mod) if r["participant_id"] == "RF_W_LIVE"]
    assert mine
    assert all(r.get("withdrawn") for r in mine), \
        [r["run_id"] for r in mine if not r.get("withdrawn")]


# --- B6: a same-arm return is not a cross-arm arrival ------------------------

def test_a_same_arm_return_resumes_the_run_it_belongs_to(client, runs_mod):
    """The reproduction: a finished 1:1 run, then the group link, then the 1:1
    link again. find_for_participant answered with the participant's NEWEST run
    whatever arm it belonged to, so the group run failed the arm comparison and
    the 1:1 link built a THIRD run — leaving the participant's finished code
    unreachable from the only URL they were given."""
    one = _enter(client, "/start/one-to-one", "RF_ARM_FORK")
    group = _enter(client, "/start/group", "RF_ARM_FORK")
    again = _enter(client, "/start/one-to-one", "RF_ARM_FORK")

    assert again == one, "the 1:1 link minted a second 1:1 run"
    assert group != one
    ids = {r["run_id"] for r in _all_runs(runs_mod)}
    assert ids == {one, group}, f"one human minted {len(ids)} runs: {ids}"


def test_a_finished_run_is_still_the_one_that_link_hands_back(client, runs_mod):
    """The consequence that costs the participant money: their completion code.

    A forked run is done:false with a PARTIAL code, and the finished code they
    were shown is no longer reachable from the link they hold.
    """
    one = _enter(client, "/start/one-to-one", "RF_ARM_CODE")
    run = runs_mod.get(one)
    run["index"] = len(run["scenarios"])
    runs_mod.save(run)
    finished_code = runs_mod.completion_code(run)

    _enter(client, "/start/group", "RF_ARM_CODE")
    again = _enter(client, "/start/one-to-one", "RF_ARM_CODE")

    view = client.get(f"/api/run/{again}").json()
    assert view["done"] is True
    assert view["completion_code"] == finished_code
    assert "PARTIAL" not in view["completion_code"]


def test_a_genuine_cross_arm_arrival_is_still_cross_linked(client, runs_mod):
    """The behaviour that must survive the fix: someone who really does arrive
    on the other arm gets their own run, and both runs say so."""
    one = _enter(client, "/start/one-to-one", "RF_ARM_LINK")
    group = _enter(client, "/start/group", "RF_ARM_LINK")

    assert group != one
    a, b = runs_mod.get(one), runs_mod.get(group)
    assert [l["run_id"] for l in a.get("other_arm_runs", [])] == [group]
    assert [l["run_id"] for l in b.get("other_arm_runs", [])] == [one]
    assert a["construct_pool"]["arm"] == "one_to_one"
    assert b["construct_pool"]["arm"] == "group"


def test_a_return_to_the_same_link_still_resumes_with_no_other_arm_in_play(client):
    """The dropped-connection case the lookup exists for, unchanged."""
    first = _enter(client, "/start", "RF_ARM_PLAIN")
    assert _enter(client, "/start", "RF_ARM_PLAIN") == first


# --- B4: an unrecognised ?variant= --------------------------------------------

def _bad_pairing(runs_mod, run: dict) -> bool:
    """Does this run serve S1 A alongside Teamwork?

    The pairing FORM_EXCLUSIONS exists to prevent: S1 A and both Teamwork forms
    turn on the same situation, so serving them together compromises the
    discriminant validity of the pair.
    """
    constructs = {s["construct"] for s in run["scenarios"]}
    if "teamwork" not in constructs:
        return False
    return any(s["construct"] == "conflict_management" and s["variant"] == "A"
               for s in run["scenarios"])


def test_a_variant_letter_no_form_carries_is_refused(runs_mod):
    """An unknown arm is already refused at create(); an unknown variant is the
    same mistake with a worse consequence — it does not fail, it silently pins
    every construct and switches the exclusion table off.

    A LETTER, which is what the harm needs. The empty value is not in this list:
    it pins nothing, so it cannot switch anything off, and refusing it turned a
    paid participant away at the door instead. See
    test_an_empty_variant_means_no_pin_and_not_a_refusal below.
    """
    for bad in ("Z", "1", "AB"):
        with pytest.raises(ValueError):
            runs_mod.create(f"RF_VAR_{bad!r}", seed=1, variant=bad)


@pytest.mark.parametrize("bad", ["Z", "1"])
def test_an_unknown_variant_never_disables_the_exclusion(runs_mod, bad):
    """Counted, not argued: over seeds 0-199 this was 94 bad pairings before
    the fix and 0 after, each of the 94 stamped "form was pinned by the caller;
    exclusion not applied" — the run document explaining away the very pairing
    the table exists to prevent.

    That 94 was counted when Conflict Management had two forms, and it was the
    same number as the share of draws landing on S1 A, which was the point: an
    unknown letter did not steer the draw, it only switched off the correction.
    With three forms per construct the share is a third rather than a half —
    re-measured 68 of 200 seeds, 3357 of 10000 (0.336) — so the two numbers
    would now both be ~68. The identity still holds and the assertion below is
    unchanged; only the figure has moved, and it is written down here so the
    94 is not read as a current measurement."""
    bad_pairings = 0
    for seed in range(200):
        try:
            run = runs_mod.create(f"RF_VARB_{bad}_{seed}", seed=seed, variant=bad)
        except ValueError:
            continue
        if _bad_pairing(runs_mod, run):
            bad_pairings += 1
    assert bad_pairings == 0


@pytest.mark.usefixtures("per_slot_draw")
def test_no_variant_at_all_is_still_the_clean_baseline(runs_mod):
    """0/200, unchanged: the exclusion pass corrects every draw that lands on
    S1 A."""
    bad_pairings = sum(
        _bad_pairing(runs_mod, runs_mod.create(f"RF_VARN_{seed}", seed=seed))
        for seed in range(200))
    assert bad_pairings == 0


def test_the_legitimate_variant_pin_still_works(runs_mod):
    """variant=A and variant=B are a real operator choice — piloting one form
    rather than a random mix — and they must keep working, including the A pin
    whose conflict with the exclusion is honoured and RECORDED."""
    a = runs_mod.create("RF_VAR_A", seed=3, variant="A")
    assert all(s["variant"] == "A" for s in a["scenarios"]), _ids(a)
    conflict = [e for e in a["form_exclusions"]
                if e["construct"] == "conflict_management"]
    assert conflict and conflict[0]["resolved"] is False
    assert "pinned by the caller" in conflict[0]["reason"]

    b = runs_mod.create("RF_VAR_B", seed=3, variant="B")
    assert all(s["variant"] == "B" for s in b["scenarios"]), _ids(b)
    assert runs_mod.create("RF_VAR_a", seed=3, variant="a")["scenarios"][0]["variant"] == "A"


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_an_entry_link_refuses_an_unknown_variant_rather_than_recording_it(
        client, runs_mod, path):
    """The wave-sized version: one bad letter in the Qualtrics redirect used to
    invalidate every run it built, silently."""
    r = client.get(path, params={"pid": "RF_VAR_LINK", "variant": "Z"},
                   follow_redirects=False)
    assert r.status_code == 400, r.text
    assert not _all_runs(runs_mod), "a run was built on an unusable variant"


def test_an_entry_link_still_honours_variant_a(client, runs_mod):
    """The unrestricted link: four constructs, four slots, every one of them A.

    Parametrised over all three links once, which asserted something two of them
    cannot do. See the arm-link test below for what they do instead and why.
    """
    r = client.get("/start", params={"pid": "RF_VAR_OK", "variant": "A"},
                   follow_redirects=False)
    run = runs_mod.get(_run_id_from(r))
    assert all(s["variant"] == "A" for s in run["scenarios"]), _ids(run)
    pool = run["construct_pool"]
    assert pool["variant_pin"] == "A"
    assert pool["variant_pin_unfilled"] == [], "nothing was left unfilled here"


@pytest.mark.parametrize("path", ["/start/one-to-one", "/start/group"])
def test_an_arm_link_fills_what_the_variant_pin_can_and_says_what_it_could_not(
        client, runs_mod, path):
    """An arm link and a variant pin ask for two things that cannot both happen.

    An arm restricts the run to two constructs and a run is always four
    encounters, so each construct fills two slots; a construct has exactly one
    form carrying a given letter. `?variant=A` on an arm link can therefore
    cover half the run and no more, and the question is only what the other half
    is. It used to be the same two encounters over again — S1A, S2A, S1A, S2A,
    in 200 runs out of 200 on both arms and for both letters — which a
    participant spots on sight and which is worth nothing as data to whoever
    asked for the pin.

    So the pin is honoured while forms carrying the letter last, the remaining
    slots get the construct's UNSEEN form, no encounter repeats, and the run
    document carries the shortfall as a field. The alternative to a recorded
    shortfall is not a satisfied pin — it is the same conversation twice with
    nothing on the run saying so.
    """
    r = client.get(path, params={"pid": "RF_VAR_OK", "variant": "A"},
                   follow_redirects=False)
    run = runs_mod.get(_run_id_from(r))
    ids = [s["id"] for s in run["scenarios"]]

    assert len(set(ids)) == len(ids), f"the same encounter twice: {ids}"
    # Half the run is the pinned letter: one A form per construct in the arm.
    assert sum(1 for s in run["scenarios"] if s["variant"] == "A") == 2, ids
    # And every construct's first encounter is the letter that was asked for.
    seen = set()
    for s in run["scenarios"]:
        if s["construct"] not in seen:
            seen.add(s["construct"])
            assert s["variant"] == "A", ids

    pool = run["construct_pool"]
    assert pool["variant_pin"] == "A"
    unfilled = pool["variant_pin_unfilled"]
    assert len(unfilled) == 2, unfilled
    assert {u["requested_variant"] for u in unfilled} == {"A"}
    assert all(u["served"] in ids for u in unfilled)
    assert sorted(u["construct"] for u in unfilled) == \
        sorted(pool["constructs"])


@pytest.mark.usefixtures("per_slot_draw")
@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_an_empty_variant_means_no_pin_and_not_a_refusal(
        client, runs_mod, path, blank):
    """`&variant=` with nothing after it is how a half-filled link renders, and
    it has to build the ordinary counterbalanced run.

    The refusal above is for a LETTER no form carries, because such a letter was
    read as a deliberate pin and switched the exclusion table off for a whole
    wave. An empty value pins nothing — it never reached that branch and never
    could — so refusing it buys no correctness and costs a paid participant
    their session at the door, mid-study, with a 400 and nothing to do about it.
    That is the one thing this entry surface is built never to do: an unusable
    participant KEY is recorded as `unattributed` and waved through for exactly
    this reason, and an unusable form letter matters less than the key does.
    """
    run = runs_mod.get(_enter(client, path, "RF_VAR_BLANK", variant=blank))
    assert len(run["scenarios"]) == 4, _ids(run)
    # Not "the letters came out mixed" — an honest draw lands all-B about one
    # time in eight, and that run is correct. What must not be there is the
    # claim that a human chose the form, which is the only thing
    # _apply_form_exclusions treats as a reason to leave a bad pairing standing.
    pinned = [e for e in run["form_exclusions"]
              if "pinned by the caller" in (e.get("reason") or "")]
    assert not pinned, f"an empty variant was recorded as a caller's pin: {pinned}"


@pytest.mark.usefixtures("per_slot_draw")
@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_variant_builds_the_run_no_variant_at_all_would(runs_mod, blank):
    """Same key, same seed, same four encounters: empty and absent are one case.

    The exact statement the refusal broke — a link carrying `&variant=` produced
    a 400 where the same link without it produced this run.
    """
    absent = runs_mod.create("RF_VAR_SAME", seed=11)
    empty = runs_mod.create("RF_VAR_SAME2", seed=11, variant=blank)
    assert _ids(empty) == _ids(absent)


@pytest.mark.usefixtures("per_slot_draw")
def test_an_empty_variant_leaves_the_exclusion_table_switched_on(runs_mod):
    """The measurement that made B4 a blocker, run against the empty value.

    0/200 bad pairings for no variant at all; this asserts the same of
    `variant=""`, which is what shows the empty case was never part of B4's harm
    and can be waved through rather than refused.
    """
    bad_pairings = sum(
        _bad_pairing(runs_mod, runs_mod.create(f"RF_VARE_{seed}", seed=seed,
                                               variant=""))
        for seed in range(200))
    assert bad_pairings == 0


# --- B3: ?cohort= is the operator's, not the participant's -------------------

@pytest.fixture()
def fielded(client, monkeypatch):
    """A deployment that could actually be fielding participants.

    SESSION_KEY set, which is not decoration: _refuse_unprotected_public_start
    will not let this process serve on a public bind without one, so "no
    SESSION_KEY" means loopback, which means no recruited participants. The
    operator/participant distinction these tests turn on only exists where a
    credential exists, and this is the shape a real wave runs in.
    """
    monkeypatch.setattr(appmod, "SESSION_KEY", "s3cret", raising=False)
    return client


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_a_participant_supplied_cohort_does_not_decide_the_dataset(
        fielded, runs_mod, path):
    """cohort=internal is not a label, it is an exit from the study.

    storage._consent_provenance short-circuits on it: no Qualtrics response id
    required, no UPSTREAM_CONSENT_VERSION required, consent_upstream_verified
    false. So a participant whose link picked up that parameter was recorded
    AND silently dropped from ?cohort=study.
    """
    run = runs_mod.get(_enter(fielded, path, "RF_COH_1", cohort="internal"))
    assert run["cohort"] == "study"


@pytest.mark.parametrize("bogus", ["internal", "unattributed", "gold", "STUDY"])
def test_no_cohort_a_participant_can_type_is_taken_at_face_value(
        fielded, runs_mod, bogus):
    run = runs_mod.get(_enter(fielded, "/start", f"RF_COH_{bogus}", cohort=bogus))
    assert run["cohort"] == "study"


def test_an_unattributable_arrival_still_lands_in_its_own_cohort(fielded, runs_mod):
    """Dropping the parameter must not drop the distinction the code already
    makes: a key that did not pipe is still not study data.

    POSTed rather than GOT because an unusable key now gets the entry check page
    on the GET — the raw template link carries exactly this value, and the
    fetches of it were minting runs. This is the Continue press behind that
    page, which is where the arrival itself now happens; what the cohort rule
    has to hold for is unchanged.
    """
    run = runs_mod.get(_run_id_from(
        fielded.post("/start", params={"pid": "${e://Field/ParticipantKey}",
                                       "cohort": "study"},
                     follow_redirects=False)))
    assert run["cohort"] == "unattributed"


def test_an_operator_holding_the_session_key_may_still_choose_the_cohort(
        fielded, runs_mod):
    """A deliberate operator choice is legitimate and stays available — it is
    how a member of the lab walks the study links without contaminating the
    dataset."""
    run = runs_mod.get(_enter(fielded, "/start", "RF_COH_OP",
                              cohort="internal", key="s3cret"))
    assert run["cohort"] == "internal"


def test_the_wrong_key_does_not_buy_the_cohort(fielded, runs_mod):
    run = runs_mod.get(_enter(fielded, "/start", "RF_COH_BAD",
                              cohort="internal", key="not-it"))
    assert run["cohort"] == "study"


def test_a_keyless_deployment_is_all_operator_and_that_is_the_rule_everywhere(
        client, runs_mod):
    """Stated rather than left to be discovered.

    With no SESSION_KEY there is no way to tell an operator from anyone else,
    and the app's answer to that everywhere — check_key, _require_owner_or_key,
    /test — is the same: open. It is not a hole here either, because
    _refuse_unprotected_public_start stops this process serving on a public
    bind without a key, so a keyless deployment is one nobody is recruited into.
    """
    assert appmod._operator_key(None) is True
    run = runs_mod.get(_enter(client, "/start", "RF_COH_DEV", cohort="internal"))
    assert run["cohort"] == "internal"


# --- B5: the internal test entrance ------------------------------------------

def test_the_test_entrance_needs_the_key_when_one_is_configured(
        client, runs_mod, monkeypatch):
    """GET /test -> POST /api/consent -> ws voice reached live audio and webcam
    capture in three requests, from anywhere on the internet, on the study's
    gateway budget. Containment held (cohort=internal), so this is a spend and
    recording surface rather than a data-integrity one — but it is a door."""
    monkeypatch.setattr(appmod, "SESSION_KEY", "s3cret", raising=False)

    r = client.get("/test", params={"name": "stranger"}, follow_redirects=False)
    assert r.status_code == 401, r.text
    assert not _all_runs(runs_mod), "an unauthenticated request minted a run"


def test_the_test_entrance_still_works_for_someone_holding_the_key(
        client, runs_mod, monkeypatch):
    """It is NOT deleted: the researcher demos the platform to their lab with
    it, and the demo door in the next phase is built on top of it."""
    monkeypatch.setattr(appmod, "SESSION_KEY", "s3cret", raising=False)

    r = client.get("/test", params={"name": "jennie", "variant": "A",
                                    "key": "s3cret"}, follow_redirects=False)
    run = runs_mod.get(_run_id_from(r))
    assert run["cohort"] == "internal"
    assert run["participant_record_id"]
    assert all(s["variant"] == "A" for s in run["scenarios"])


def test_the_test_entrance_stays_open_in_local_dev(client, runs_mod):
    """Gated the way the rest of the app gates things: open when no SESSION_KEY
    is configured, so a local checkout still runs without ceremony."""
    r = client.get("/test", params={"name": "dev"}, follow_redirects=False)
    assert runs_mod.get(_run_id_from(r))["cohort"] == "internal"


def test_the_test_entrance_refuses_an_unknown_variant_too(client, runs_mod):
    r = client.get("/test", params={"name": "dev", "variant": "Z"},
                   follow_redirects=False)
    assert r.status_code == 400, r.text
    assert not _all_runs(runs_mod)
