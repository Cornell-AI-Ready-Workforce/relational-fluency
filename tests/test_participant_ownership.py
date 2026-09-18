"""Whose record is it, and whose run is it — asked before anything is ended.

Every gate in this file closes a hole that needed no key and no unusual
precondition. Two of them let a stranger permanently end another participant's
study with a single POST:

  * POST /api/consent/decline withdrew whatever run_id it was handed. It did
    compute `recorded`, but against the POSTED participant record, which nothing
    checked belonged to that run: decline your own throwaway record, name
    somebody else's run, and their study is over. runs.withdraw has no clearing
    path and runs.advance refuses a withdrawn run, so "over" means over.
  * POST /api/consent accepted any existing participant_id with no ownership
    check at all, so a person who had stopped could consent a stranger's live
    record and be recorded under it.

And three that make a withdrawal mean what config/consent.yaml says it means.
A withdrawal stopped the RECORDING and not the CAPTURE: registry.drop closed the
store, and the participant's microphone socket stayed open, still read, still
forwarded to the model provider and still billed, until they closed the tab. The
teardown was wired to one of the three places a withdrawal is recorded, so
declining in a second tab or being found withdrawn on arrival stopped nothing.
And POST /api/run/{id}/advance still answered 200 on a withdrawn run.

THE OTHER HALF, AND THE REASON IT COMES FIRST IN THIS FILE. A gate that catches
the attack and the participant is a worse defect than the one it closes. Every
refusal below is paired with the legitimate user who must still get through, and
those four live at the top: a consenting participant finishes an encounter,
uploads their webcam bytes, advances and is given a code; a withdrawn one still
reaches their own closing page, their own run view and their partial code; a
researcher holding SESSION_KEY is locked out of nothing; and an unreadable file
in the runs or participants directory refuses nobody.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from server import app as appmod

SCENARIO = "S1B"


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def store(tmp_path, monkeypatch, runs_mod):
    from server import storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    # The deployment has said which approved text the survey is showing.
    # Without it storage refuses to record ANY study consent, and every
    # positive control below would pass for the wrong reason — a 404 that says
    # nothing about whose record it is.
    storage.init_storage()
    return storage


@pytest.fixture()
def sessions_root(tmp_path, monkeypatch, store):
    """The session directory every participant-facing route resolves against."""
    from server import video

    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(appmod, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    return root


@pytest.fixture()
def client(store, sessions_root, monkeypatch):
    """No SESSION_KEY: the open-collection deployment, which is the one these
    holes were reproduced on and the one a participant-facing gate has to hold
    on by itself."""
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


class FakeS3:
    """Enough of the boto3 client to keep the suite's network guard happy.

    The webcam PUT asks video.exists() before it stores anything, and that is a
    HeadObject. Answering 404 here is the deployment with a bucket and no object
    yet, which is what a live participant's first upload actually meets."""

    def head_object(self, **kw):
        from botocore.exceptions import ClientError

        raise ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject")


@pytest.fixture()
def s3(monkeypatch):
    from server import video

    stub = FakeS3()
    monkeypatch.setattr(video, "_s3", stub)
    return stub


def _arrival(store, runs_mod, key, *, consented=True):
    """One participant as /start leaves them: a run, and one record bound to it
    in both directions. `consented=False` is the record /start actually mints —
    pending, waiting for POST /api/consent to flip it."""
    run = runs_mod.create(key, qualtrics_id=f"R_{key}", cohort="study")
    pid = store.create_participant(code=key,
                                   run_id=run["run_id"], cohort="study")
    run["participant_record_id"] = pid
    runs_mod.save(run)
    return run, pid


def _session(sessions_root, sid, owner, scenario=SCENARIO, n_turns=3):
    sdir = sessions_root / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "manifest.json").write_text(json.dumps(
        {"session_id": sid, "participant_id": owner, "scenario": scenario,
         "status": "closed", "n_turns": n_turns}), encoding="utf-8")
    (sdir / "events.jsonl").write_text("", encoding="utf-8")
    return sdir


def _current_scenario(run):
    return run["scenarios"][run.get("index", 0)]["id"]


class FakeWS:
    """Just enough WebSocket to say whether the capture socket was shut."""

    def __init__(self):
        self.closed_with = None

    async def close(self, code=1000):
        self.closed_with = code


class FakeStore:
    def __init__(self, pid):
        self.participant_id = pid


class FakeSession:
    """A registry entry shaped like the live encounter the teardown has to find.

    Carries a real capture socket, because the defect this file reproduces is
    precisely that registry.drop closed the STORE and left the socket open."""

    def __init__(self, sid, pid, run_id):
        self.id = sid
        self.store = FakeStore(pid)
        self.run_id = run_id
        self.participant_ws = FakeWS()

    async def close_participant_socket(self, code=1000):
        ws = self.participant_ws
        if ws is None:
            return False
        self.participant_ws = None
        await ws.close(code=code)
        return True


@pytest.fixture()
def live_encounter(monkeypatch):
    """Put a session in the registry and keep registry.drop observable.

    drop() is replaced rather than run, for the same reason the existing
    withdrawal suite replaces it: a FakeSession has no SessionStore to close.
    What the tests then assert is the pair — dropped AND the socket shut —
    because dropping alone is the defect."""
    dropped = []
    monkeypatch.setattr(appmod.registry, "drop", lambda sid: dropped.append(sid))

    def put(sid, pid, run_id):
        sess = FakeSession(sid, pid, run_id)
        monkeypatch.setitem(appmod.registry._sessions, sid, sess)
        return sess

    return dropped, put


# --- the positive controls, written first ------------------------------------

def test_a_withdrawn_participant_still_reaches_their_own_closing_page(
        client, store, runs_mod):
    """They gave us their time and are owed the partial code they take back to
    the survey to be paid. READING is never what a withdrawal blocks."""
    run, pid = _arrival(store, runs_mod, "PKEYGONE")
    run_id = run["run_id"]
    assert client.post(f"/api/run/{run_id}/withdraw",
                       json={"participant_id": pid}).status_code == 200

    r = client.get(f"/api/run/{run_id}")
    assert r.status_code == 200, r.text
    assert r.json()["completion_code"], "a stopped participant lost their code"
    assert r.json()["withdrawn"], "the run view no longer says they stopped"

    assert client.get("/api/run/config").status_code == 200
    assert client.get("/v2").status_code == 200


# --- ITEM 7: a decline may only stop the run it belongs to -------------------

# --- ITEM 8: POST /api/consent may only act on the caller's own record -------

    # Deliberately NOT refused, and worth stating so the gate is not widened by
    # a later reader: a caller holding the victim's run id and record id holds
    # their study link, and this platform has no way to be a different person
    # from the one the link names. _record_is_this_arrival makes the same
    # judgement at /start. What is refused is the id ALONE, and an asserted key
    # that the record does not carry.


# --- ITEM 4: the withdrawal has to close the socket, not just the store ------

def test_closing_the_capture_socket_ends_the_readers_loop():
    """And the close has to END THE READER, or it is decoration.

    RealtimeVoiceSessionRunner._client_to_model is `while not closed: msg =
    await self.ws.receive()`, and it forwards every frame it gets to the model
    provider. The whole of item 4 rests on one question about a real Starlette
    socket: does a close from ANOTHER task turn that pending receive() into a
    websocket.disconnect? A fake that records the call cannot answer it.

    So this is the runner's loop in miniature, against a real socket, with no
    scenario and no gateway: count the frames, take the close, and show that the
    next receive is a disconnect — which is the return path the runner already
    has, and which lets its FIRST_COMPLETED wait cancel the rest and close the
    gateway session in its finally.

    FastAPI and WebSocket are imported at module scope on purpose: this file
    carries `from __future__ import annotations`, so a locally-imported
    annotation is a string FastAPI resolves against module globals and would
    otherwise read as a missing query parameter."""
    from server.session import Session

    probe = FastAPI()
    seen = {}

    @probe.websocket("/capture")
    async def capture(ws: WebSocket):
        await ws.accept()
        forwarded = 0
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                seen["ended"] = "disconnect"
                seen["forwarded"] = forwarded
                return
            if msg.get("text") == "they withdrew":
                class Holder:
                    participant_ws = ws

                await Session.close_participant_socket(Holder())
                continue
            forwarded += 1

    with TestClient(probe) as probe_client:
        with probe_client.websocket_connect("/capture") as ws:
            ws.send_bytes(b"\0" * 320)        # one frame of microphone audio
            ws.send_text("they withdrew")
            closed = ws.receive()
            assert closed["type"] == "websocket.close", (
                "the participant's browser was never told the socket was "
                f"closed: {closed}")

    assert seen.get("ended") == "disconnect", (
        "the reader was still waiting on the socket after the withdrawal, so "
        "audio would go on being forwarded to the model provider")
    assert seen.get("forwarded") == 1, seen


def test_withdrawing_closes_the_microphone_and_not_only_the_recorder(
        client, store, runs_mod, live_encounter):
    """The route-level half of the same fact."""
    dropped, put = live_encounter
    run, pid = _arrival(store, runs_mod, "PKEYMIC")
    sess = put("s_1772460300_22bb01", pid, run["run_id"])
    ws = sess.participant_ws

    r = client.post(f"/api/run/{run['run_id']}/withdraw",
                    json={"participant_id": pid})
    assert r.status_code == 200, r.text
    assert sess.id in dropped, "the live encounter was not torn down at all"
    assert ws.closed_with is not None, (
        "the recorder was closed and the microphone socket was left open: audio "
        "kept being read and forwarded to the model provider, and billed")


def test_a_withdrawal_does_not_close_another_participants_microphone(
        client, store, runs_mod, live_encounter):
    """Positive control. The cheapest way to pass the test above is to close
    every socket in the registry, which would end the encounters of everybody
    else in the wave."""
    dropped, put = live_encounter
    gone, gone_pid = _arrival(store, runs_mod, "PKEYMIC2")
    live, live_pid = _arrival(store, runs_mod, "PKEYMICLIVE")
    mine = put("s_1772460300_22bb02", gone_pid, gone["run_id"])
    theirs = put("s_1772460300_22bb03", live_pid, live["run_id"])

    assert client.post(f"/api/run/{gone['run_id']}/withdraw",
                       json={"participant_id": gone_pid}).status_code == 200
    assert mine.id in dropped
    assert theirs.id not in dropped
    assert theirs.participant_ws.closed_with is None, (
        "withdrawing one participant cut off another participant's microphone")


# --- ITEM 6: wired to the fact, not to the one route -------------------------

def test_arriving_on_a_link_after_withdrawing_tears_down_what_is_still_live(
        client, store, runs_mod, live_encounter):
    """The third place a withdrawal is recorded. /start stamps the stop onto a
    run that did not carry it yet — a person who stopped on one arm touching the
    other arm's link — and stopped nothing that was still recording."""
    dropped, put = live_encounter
    run, pid = _arrival(store, runs_mod, "PKEYARR")
    # Stopped on the record, not yet on this run: the shape runs.withdraw
    # repairs at the door.
    store.record_withdrawal(pid, {"at": 1.0, "reason": "participant_withdrew"})
    sess = put("s_1772460300_33cc02", pid, run["run_id"])

    r = client.get("/start", params={"pid": "PKEYARR", "participant_id": pid,
                                     "qid": "R_arr"}, follow_redirects=False)
    assert r.status_code == 307, r.text
    assert runs_mod.get(run["run_id"])["withdrawn"], (
        "/start handed a withdrawn participant a run with no stop on it")
    assert sess.id in dropped, (
        "the arrival recorded the withdrawal and left the encounter recording")
    assert sess.participant_ws is None


def test_an_ordinary_arrival_tears_down_nothing(client, store, runs_mod,
                                                live_encounter):
    """Positive control for the wiring above: /start is walked by every
    participant in the wave, most of them mid-run and none of them stopped."""
    dropped, put = live_encounter
    run, pid = _arrival(store, runs_mod, "PKEYFINE")
    sess = put("s_1772460300_33cc03", pid, run["run_id"])

    r = client.get("/start", params={"pid": "PKEYFINE", "qid": "R_fine"},
                   follow_redirects=False)
    assert r.status_code == 307, r.text
    assert dropped == [], "an ordinary arrival tore down a live encounter"
    assert sess.participant_ws.closed_with is None


# --- ITEM 9: advance on a withdrawn run --------------------------------------

def test_advance_refuses_a_withdrawn_run(client, store, runs_mod, sessions_root):
    """It answered 200. runs.advance no-ops on a withdrawn run, so the encounter
    was never recorded against it — but the caller was told it had been, which
    is the one thing a completion endpoint may not get wrong."""
    run, pid = _arrival(store, runs_mod, "PKEYADV")
    sid = "s_1772460300_44dd01"
    _session(sessions_root, sid, pid, scenario=_current_scenario(run))
    assert client.post(f"/api/run/{run['run_id']}/withdraw",
                       json={"participant_id": pid}).status_code == 200

    r = client.post(f"/api/run/{run['run_id']}/advance", params={"session_id": sid})
    assert r.status_code == 403, r.text
    assert runs_mod.get(run["run_id"])["index"] == 0


def test_advance_is_still_idempotent_for_an_encounter_already_recorded(
        client, store, runs_mod, sessions_root):
    """Positive control, and the reason the refusal sits BELOW the idempotency
    check. A participant who finishes an encounter and then presses stop may
    have a retried advance already in flight; answering it 403 would strand the
    finished encounter it was confirming."""
    run, pid = _arrival(store, runs_mod, "PKEYADV2")
    sid = "s_1772460300_44dd02"
    _session(sessions_root, sid, pid, scenario=_current_scenario(run))

    assert client.post(f"/api/run/{run['run_id']}/advance",
                       params={"session_id": sid}).status_code == 200
    assert client.post(f"/api/run/{run['run_id']}/withdraw",
                       json={"participant_id": pid}).status_code == 200

    r = client.post(f"/api/run/{run['run_id']}/advance", params={"session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["completion_code"], (
        "a retried advance for an already-recorded encounter lost the code")


# --- ITEM 10: a second record of the same person ------------------------------

# --- ITEM 11: the stop control ends the caller's study, not a stranger's ------
#
# The positive controls come first, and they are the point. POST
# /api/run/{id}/withdraw is the only stop a consented participant has, and a
# gate here that refuses the wrong person does not cost them a request — it
# costs them the thing config/consent.yaml promises. So: the ordinary stop with
# a live encounter, the stop pressed on a run whose record was never minted, and
# the researcher key (above, test_the_researcher_key_is_locked_out_of_nothing).
# Only then the stranger.

def test_a_participant_stopping_their_own_study_still_stops_it(
        client, store, runs_mod, live_encounter):
    """POSITIVE CONTROL. The whole point of the route, with the microphone open.

    This is what static/v2.html's leaveBtn sends: the run in the path, the
    participant record id the page was given at /start, and the session id of
    the encounter they are in. It must stop the run, stamp the record, tear the
    encounter out of the registry and close the socket — before and after any
    gate is added to this route."""
    dropped, put = live_encounter
    run, pid = _arrival(store, runs_mod, "PKEYOWNSTOP")
    sess = put("s_1772460300_66ff01", pid, run["run_id"])

    r = client.post(f"/api/run/{run['run_id']}/withdraw", json={
        "participant_id": pid, "session_id": sess.id,
        "reason": "participant_withdrew"})
    assert r.status_code == 200, r.text
    assert r.json()["withdrawn"], "their own stop did not stop their run"
    assert r.json()["completion_code"], (
        "a partial stop lost the code they take back to the survey to be paid")
    assert runs_mod.get(run["run_id"])["withdrawn"]
    assert store.get_participant(pid)["withdrawn"]
    assert sess.id in dropped, "their live encounter kept recording"
    assert sess.participant_ws is None, "their microphone was left open"


def test_the_stop_control_still_works_when_the_run_never_minted_a_record(
        client, runs_mod):
    """POSITIVE CONTROL. /start's mint can fail, and it hands the page no
    participant_id when it does — so the commonest legitimate stop on that run
    carries nothing but the run id in the path.

    There is no participant record for this run, under its key or anywhere else,
    so there is no enrolment for a stranger to end and nobody for a gate to
    protect. Refusing here would refuse the one person it could be."""
    run = runs_mod.create("PKEYNOMINT", qualtrics_id="R_nomint", cohort="study")
    assert not run.get("participant_record_id")

    r = client.post(f"/api/run/{run['run_id']}/withdraw",
                    json={"reason": "participant_withdrew"})
    assert r.status_code == 200, r.text
    assert runs_mod.get(run["run_id"])["withdrawn"], (
        "the only stop control a mint-failed arrival has was refused")


def test_a_stranger_cannot_stop_a_run_that_is_not_theirs(
        client, store, runs_mod, live_encounter, capsys):
    """The keyless, precondition-free way to end somebody else's study.

    The route asked nothing at all: an empty POST to a run id ended it. Run ids
    are not secrets by this code base's own standard — one travels in the
    participant's address bar as /v2?run=..., and where SESSION_KEY is unset GET
    /api/runs hands out the whole roster keylessly — and there is no clearing
    path, so "ended" is permanent: advance 403s them, the capture socket is
    refused, and their record reads withdrawn to an IRB.

    What the gate asks for is the record id /start put in the participant's own
    URL, which no keyless route echoes back."""
    dropped, put = live_encounter
    victim, victim_pid = _arrival(store, runs_mod, "PKEYVICTIM")
    sess = put("s_1772460300_66ff02", victim_pid, victim["run_id"])

    r = client.post(f"/api/run/{victim['run_id']}/withdraw",
                    json={"reason": "not_me"})
    assert r.status_code == 403, r.text
    assert not runs_mod.get(victim["run_id"]).get("withdrawn"), (
        "a stranger's POST ended this participant's study")
    assert not store.get_participant(victim_pid).get("withdrawn"), (
        "the stop was carried onto the victim's participant record")
    assert dropped == [], "a stranger's POST tore down a live encounter"
    assert sess.participant_ws.closed_with is None, (
        "a stranger's POST cut off a participant's microphone")
    assert "does not belong" in capsys.readouterr().out

    # And the victim is still in the study: the refusal cost them nothing.
    r = client.post(f"/api/run/{victim['run_id']}/withdraw",
                    json={"participant_id": victim_pid})
    assert r.status_code == 200, r.text


def test_a_stranger_holding_their_own_record_cannot_stop_a_foreign_run(
        client, store, runs_mod):
    """A record the caller really does hold is evidence about their own run and
    about nothing else. Same shape as the decline hole above: arrive normally,
    get a real record, then name somebody else's run."""
    victim, victim_pid = _arrival(store, runs_mod, "PKEYVICTIM2")
    _stranger, stranger_pid = _arrival(store, runs_mod, "PKEYSTRANGER")

    r = client.post(f"/api/run/{victim['run_id']}/withdraw",
                    json={"participant_id": stranger_pid, "reason": "not_me"})
    assert r.status_code == 403, r.text
    assert not runs_mod.get(victim["run_id"]).get("withdrawn")
    assert not store.get_participant(victim_pid).get("withdrawn")
