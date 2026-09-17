"""A withdrawal is a statement about the PERSON, so it lives on their record.

Round two taught `runs.withdraw` to stamp every run under a participant key and
taught the capture gate to go looking for those stamps. That closed the two
routes anybody thought to check and left the shape of the defect untouched:
nothing wrote a withdrawal onto the participant RECORD, so every reader that
asks the record — which is every reader that is not the capture gate — still
opened. Eight reproduced consequences came out of that one omission, and they
are not eight bugs. They are one bug with eight exits:

  * PUT /api/sessions/{id}/video accepted a withdrawn participant's webcam
    bytes and wrote them to disk;
  * GET /api/sessions/{id}/video-upload-url signed a PUT into the study bucket
    for them;
  * POST /api/sessions/{id}/video-uploaded wrote into their trail, touching S3
    not at all, so it succeeded on a host with no credentials;
  * POST /api/consent minted a SECOND record carrying no withdrawal, and the
    voice socket opened on it;
  * an unreadable run file turned a withdrawal back into consent, because the
    scan skipped what it could not parse;
  * the gate was at socket OPEN only, so a session already live kept recording;
  * /start re-enrolled them whenever the arm lookup could not find the run.

So these tests are not "does route X check". They are: does the withdrawal
reach the record, does every route that takes participant-owned data or spends
money ask the same question through one helper, and — the half that keeps a gate
honest — is a LIVE participant still completely unaffected, and can a withdrawn
one still READ their own closing page. A gate that refuses everybody is not a
fix, it is the same wave lost a different way.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import app as appmod

SCENARIO = "S1B"

#: Two encounters on disk, in the shape storage.SessionStore mints ids in.
WITHDRAWN_SID = "s_1772460300_44c9a2"
LIVE_SID = "s_1772460300_44c9b3"
SPARE_SID = "s_1772460300_44c9c4"
SPARE_LIVE_SID = "s_1772460300_44c9d5"


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
    """No SESSION_KEY: the open-collection deployment, which is where a
    participant-facing gate has to hold on its own."""
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


class FakeS3:
    """Enough of the boto3 client to prove whether a PUT was signed."""

    def __init__(self):
        self.signed = []

    def head_object(self, **kw):
        from botocore.exceptions import ClientError

        raise ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject")

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        self.signed.append((op, Params["Key"]))
        return f"https://s3.invalid/{Params['Key']}?sig=x"


@pytest.fixture()
def s3(monkeypatch):
    from server import video

    stub = FakeS3()
    monkeypatch.setattr(video, "_s3", stub)
    return stub


def _session(sessions_root, sid, owner, scenario=SCENARIO):
    sdir = sessions_root / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "manifest.json").write_text(json.dumps(
        {"session_id": sid, "participant_id": owner, "scenario": scenario,
         "status": "closed", "n_turns": 3}), encoding="utf-8")
    (sdir / "events.jsonl").write_text("", encoding="utf-8")
    return sdir


def _arrival(store, runs_mod, key, *, arm=None):
    """One participant as /start plus POST /api/consent leaves them."""
    run = runs_mod.create(key, qualtrics_id=f"R_{key}", cohort="study", arm=arm)
    pid = store.create_participant(code=key, consent_given=True,
                                   consent_version="v1")
    run["participant_record_id"] = pid
    runs_mod.save(run)
    return run, pid


def _events(sdir):
    return [json.loads(l) for l in
            (sdir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]


@pytest.fixture()
def pair(client, store, runs_mod, sessions_root):
    """A withdrawn participant and a live one, each with an encounter on disk.

    Both exist in every test below on purpose. Every refusal here is asserted
    against the live participant as well, because the cheapest way to "fix" a
    withdrawal gate is to refuse everyone, and that loses the wave rather than
    honouring one person's stop.
    """
    gone_run, gone_pid = _arrival(store, runs_mod, "PKEY002")
    live_run, live_pid = _arrival(store, runs_mod, "PKEYLIVE")
    gone_dir = _session(sessions_root, WITHDRAWN_SID, gone_pid)
    live_dir = _session(sessions_root, LIVE_SID, live_pid)
    r = client.post(f"/api/run/{gone_run['run_id']}/withdraw",
                    json={"participant_id": gone_pid})
    assert r.status_code == 200, r.text
    assert r.json().get("withdrawn"), r.text
    return {
        "gone_run": gone_run, "gone_pid": gone_pid, "gone_dir": gone_dir,
        "live_run": live_run, "live_pid": live_pid, "live_dir": live_dir,
    }


# --- the root: the withdrawal has to reach the record ------------------------

def test_withdrawing_writes_the_stop_onto_the_participant_record(pair, store):
    """The one fact every other test here rests on.

    Before this the withdrawal existed only on run documents, so any reader that
    asked the record — and the record is what a participant id resolves to —
    read a consented participant who had pressed stop. Putting it on the record
    is what makes the answer the same at every one of the eight exits, instead
    of eight places each remembering to run a directory scan.
    """
    rec = store.get_participant(pair["gone_pid"])
    assert rec["withdrawn"], (
        "the participant record carries no withdrawal, so every reader that "
        "asks the record rather than scanning the runs still sees consent")
    assert rec["withdrawn"].get("at")
    assert rec["withdrawn"].get("reason")

    live = store.get_participant(pair["live_pid"])
    assert not live.get("withdrawn"), "a live participant was stamped withdrawn"


def test_the_run_level_stamps_survive(pair, runs_mod):
    """Kept deliberately: an analyst needs to know which run they stopped in,
    and at which encounter. The record answers "did this person stop"; the run
    answers "where". Neither replaces the other."""
    run = runs_mod.get(pair["gone_run"]["run_id"])
    assert run["withdrawn"], "the run-level stamp was dropped"
    assert run["withdrawn"].get("index") is not None


# --- the webcam chain --------------------------------------------------------

def test_the_local_webcam_put_is_refused_after_a_withdrawal(pair, client,
                                                            sessions_root, s3):
    """The loudest of the eight. This route lands webcam bytes on the task's own
    filesystem, and it had no withdrawal check at all: check_participant,
    _session_dir and _require_session_owner, none of which ask whether the
    person behind the record stopped."""
    from server import video

    r = client.put(f"/api/sessions/{WITHDRAWN_SID}/video",
                   params={"participant_id": pair["gone_pid"]},
                   content=b"\0" * 4096)
    assert r.status_code == 403, r.text
    assert not video.local_path(WITHDRAWN_SID).exists(), (
        "a withdrawn participant's webcam recording was written to disk")

    # Positive control: the gate is not over-broad.
    r = client.put(f"/api/sessions/{LIVE_SID}/video",
                   params={"participant_id": pair["live_pid"]},
                   content=b"\0" * 4096)
    assert r.status_code == 200, r.text
    assert video.local_path(LIVE_SID).exists()


def test_the_presigned_webcam_url_is_refused_after_a_withdrawal(
        pair, client, sessions_root, s3):
    """The other half of the same chain. A signed PUT is a write into the IRB
    bucket that this process cannot take back once it is handed out, so the
    refusal has to happen before anything is signed, not after."""
    _session(sessions_root, SPARE_SID, pair["gone_pid"])
    r = client.get(f"/api/sessions/{SPARE_SID}/video-upload-url",
                   params={"participant_id": pair["gone_pid"]})
    assert r.status_code == 403, r.text
    assert s3.signed == [], (
        "a PUT into the study bucket was signed for a withdrawn participant")

    _session(sessions_root, SPARE_LIVE_SID, pair["live_pid"])
    r = client.get(f"/api/sessions/{SPARE_LIVE_SID}/video-upload-url",
                   params={"participant_id": pair["live_pid"]})
    assert r.status_code == 200, r.text
    assert s3.signed, "a live participant could no longer be handed an upload URL"


def test_the_camera_absence_report_is_refused_after_a_withdrawal(pair, client):
    """It touches S3 not at all, so it succeeded on any host, credentials or
    none, and it wrote an event into a withdrawn person's trail."""
    r = client.post(f"/api/sessions/{WITHDRAWN_SID}/video-uploaded",
                    params={"participant_id": pair["gone_pid"],
                            "no_camera": "NotAllowedError"})
    assert r.status_code == 403, r.text
    assert _events(pair["gone_dir"]) == [], (
        "an event was appended to a withdrawn participant's encounter trail")

    r = client.post(f"/api/sessions/{LIVE_SID}/video-uploaded",
                    params={"participant_id": pair["live_pid"],
                            "no_camera": "NotAllowedError"})
    assert r.status_code == 200, r.text
    assert [e["type"] for e in _events(pair["live_dir"])] == ["video_absent"]


# --- re-consent --------------------------------------------------------------

@pytest.fixture()
def no_gateway(monkeypatch):
    """Let the socket gate be the only thing under test.

    A socket that gets PAST the gate goes on to build a Session, which would
    reach the model gateway — the suite's network guard forbids that, and it is
    not what these two tests are about. registry.create raising FileNotFoundError
    is the shape tests/test_entry_gates.py already uses: the socket accepts and
    answers with an error frame, so "opened" and "refused" stay distinguishable
    without anything leaving the machine.
    """
    def no_such_scenario(*a, **k):
        raise FileNotFoundError(f"unknown scenario: {SCENARIO}")

    monkeypatch.setattr(appmod.registry, "create", no_such_scenario)


def test_re_consenting_cannot_mint_a_record_without_the_withdrawal(
        pair, client, store, runs_mod, no_gateway):
    """The way back in that needed no session id and no run id at all.

    POST /api/consent with a bare code takes the minting branch, and a brand new
    record carried no withdrawal: withdrawal_for_record found no run pointing at
    it, and participant_withdrawal was being handed a record id where it wanted
    a participant key. The voice socket accepted. A record minted for somebody
    who stopped has to be born carrying that fact."""
    r = client.post("/api/consent",
                    json={"code": "PKEY002", "consent_given": True})
    assert r.status_code == 200, r.text
    fresh = r.json()["participant_id"]
    assert fresh != pair["gone_pid"]

    rec = store.get_participant(fresh)
    assert rec.get("withdrawn"), (
        "a second record was minted for a withdrawn person with no withdrawal "
        "on it, and the capture socket opens on exactly this record")
    assert appmod._consented_participant(fresh) is None

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
                f"/ws/participant/voice?scenario={SCENARIO}"
                f"&participant_id={fresh}"):
            pass
    assert caught.value.code == 4403


def test_a_participant_who_never_withdrew_can_still_consent_and_connect(
        pair, client, store, no_gateway):
    """Positive control for the branch above: minting must still work, and the
    socket must still open, for someone who never pressed stop."""
    r = client.post("/api/consent",
                    json={"code": "PKEYFRESH", "consent_given": True})
    assert r.status_code == 200, r.text
    fresh = r.json()["participant_id"]
    assert not store.get_participant(fresh).get("withdrawn")
    assert appmod._consented_participant(fresh) is not None

    with client.websocket_connect(
            f"/ws/participant/voice?scenario={SCENARIO}"
            f"&participant_id={fresh}") as ws:
        assert ws.receive_json()["type"] == "error"


# --- failing closed ----------------------------------------------------------

def test_an_unreadable_run_file_cannot_turn_a_withdrawal_back_into_consent(
        pair, runs_mod, store):
    """The scan skipped any file it could not parse, so corrupting (or losing a
    write to) the owner run answered "nobody withdrew" and the gate opened.

    Two defences, and the test asserts the outcome rather than which one fired:
    the withdrawal is on the record, so the run file is no longer the only copy,
    and the scan that backs it up refuses to report a clean negative when it
    could not read everything it was asked to read."""
    for f in sorted(runs_mod.RUNS_DIR.glob("*.json")):
        run = json.loads(f.read_text(encoding="utf-8"))
        if run.get("participant_record_id") == pair["gone_pid"]:
            f.write_text("{ this is not json", encoding="utf-8")

    assert appmod._withdrawn(pair["gone_pid"]) is True
    assert appmod._consented_participant(pair["gone_pid"]) is None


def test_an_unreadable_run_file_does_not_block_a_live_participant(
        pair, runs_mod, store):
    """The fail-closed rule above is bounded by the record.

    A live participant's own record says nothing about a withdrawal and is
    readable, so it answers on its own — an unrelated corrupt run file in the
    directory must not cost them their encounter."""
    (runs_mod.RUNS_DIR / "aaaaaaaaaaaa.json").write_text(
        "{ not json either", encoding="utf-8")
    assert appmod._withdrawn(pair["live_pid"]) is False
    assert appmod._consented_participant(pair["live_pid"]) is not None


# --- the live session --------------------------------------------------------

def test_withdrawing_stops_a_session_that_is_already_recording(
        pair, client, runs_mod, monkeypatch):
    """The gate was at socket OPEN only. Someone who pressed stop mid-encounter
    kept being recorded until they closed the tab, because /api/run/{id}/withdraw
    wrote two files and never looked at the registry of live sessions."""
    stopped = []

    class FakeStore:
        def __init__(self, pid):
            self.participant_id = pid

    class FakeSession:
        def __init__(self, sid, pid, run_id):
            self.id = sid
            self.store = FakeStore(pid)
            self.run_id = run_id
            self.participant_ws = None

    sess = FakeSession("s_1772460300_44c9e6", pair["gone_pid"],
                       pair["gone_run"]["run_id"])
    other = FakeSession("s_1772460300_44c9f7", pair["live_pid"],
                        pair["live_run"]["run_id"])
    registry = appmod.registry
    monkeypatch.setitem(registry._sessions, sess.id, sess)
    monkeypatch.setitem(registry._sessions, other.id, other)
    monkeypatch.setattr(registry, "drop", lambda sid: stopped.append(sid))

    r = client.post(f"/api/run/{pair['gone_run']['run_id']}/withdraw",
                    json={"participant_id": pair["gone_pid"]})
    assert r.status_code == 200, r.text
    assert sess.id in stopped, (
        "the encounter that was live when they pressed stop kept recording")
    assert other.id not in stopped, (
        "withdrawing one participant tore down another participant's encounter")


# --- /start ------------------------------------------------------------------

def test_start_does_not_re_enrol_a_withdrawn_participant_by_their_record(
        pair, client, runs_mod, store):
    """The /start withdrawal branch was gated on `key_status == "ok"`, so the
    lookup was skipped for exactly the arrivals this platform is designed
    around: the ones whose Qualtrics key did not pipe. Their record id is in the
    URL the platform itself handed them, and nothing asked it."""
    before = len(list(runs_mod.RUNS_DIR.glob("*.json")))
    r = client.get("/start",
                   params={"participant_id": pair["gone_pid"], "qid": "R_x"},
                   follow_redirects=False)
    assert r.status_code == 307, r.text
    after = len(list(runs_mod.RUNS_DIR.glob("*.json")))
    assert after == before, (
        "a new run was minted for a participant who had already withdrawn")

    run_id = r.headers["location"].split("run=")[1].split("&")[0]
    assert runs_mod.get(run_id)["withdrawn"], (
        "/start handed a withdrawn participant a run with no stop on it")


def test_start_still_enrols_a_participant_who_never_withdrew(pair, client,
                                                             runs_mod):
    """Positive control: the ordinary arrival is untouched."""
    before = len(list(runs_mod.RUNS_DIR.glob("*.json")))
    r = client.get("/start", params={"pid": "PKEYNEW1", "qid": "R_y"},
                   follow_redirects=False)
    assert r.status_code == 307, r.text
    assert len(list(runs_mod.RUNS_DIR.glob("*.json"))) == before + 1
    run_id = r.headers["location"].split("run=")[1].split("&")[0]
    assert not runs_mod.get(run_id).get("withdrawn")


# --- what a withdrawn participant may still do -------------------------------

def test_a_withdrawn_participant_can_still_read_their_closing_page(pair, client):
    """The half that keeps this from being a wave lost a different way.

    Someone who stops has still given their time and is still owed the partial
    completion code they take back to the survey to be paid. Reading is not
    recording, so the gate is on the routes that take their data or spend money,
    not on the ones that tell them where to go next."""
    run_id = pair["gone_run"]["run_id"]
    r = client.get(f"/api/run/{run_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["completion_code"], "no completion code on the closing page"
    assert body["withdrawn"], "the closing page could not tell they had stopped"

    assert client.get("/api/run/config").status_code == 200
    assert client.get("/v2").status_code == 200


def test_withdrawing_twice_is_still_idempotent(pair, client, store, runs_mod):
    """A second stop keeps the first moment, on the record as well as the run:
    that is when they actually stopped, and a report that moves the timestamp
    every time the button is pressed is not a report."""
    first = store.get_participant(pair["gone_pid"])["withdrawn"]["at"]
    r = client.post(f"/api/run/{pair['gone_run']['run_id']}/withdraw",
                    json={"participant_id": pair["gone_pid"],
                          "reason": "second_press"})
    assert r.status_code == 200, r.text
    assert store.get_participant(pair["gone_pid"])["withdrawn"]["at"] == first


# --- what the gate must NOT do -----------------------------------------------
#
# Everything above this line asks whether a stop is honoured. These ask the
# other question, and it is the one round three got wrong: what does the gate do
# to people who did not stop. A withdrawal is permanent by design — withdraw()
# is idempotent-forward and advance() refuses a withdrawn run — so every way of
# arriving at one wrongly ends a real participant's study with no way back, and
# writes "withdrew" in the one field an IRB reads.

def test_a_store_read_error_refuses_but_never_writes_a_withdrawal(
        pair, client, store, runs_mod, monkeypatch):
    """One transient read error must not end a live participant's study.

    The gate answers "I could not read the store" with a stamp, because a gate
    that cannot tell has to refuse. /start then fed that stamp straight into
    runs.withdraw, so an EFS blip — the failure this code base retries for
    everywhere else — wrote a permanent stop onto a live consenting
    participant's run, onto every other run under their key, and onto their
    participant record, reason "withdrawal_status_unknown". The refusal is right
    and lasts as long as the blip; the writing is not, and lasts forever.
    """
    live_pid, live_run = pair["live_pid"], pair["live_run"]
    real = appmod.participant_withdrawal
    calls = {"n": 0}

    def flaky(pid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "Input/output error")
        return real(pid)

    monkeypatch.setattr(appmod, "participant_withdrawal", flaky)
    r = client.get("/start", params={"participant_id": live_pid, "qid": "R_b"},
                   follow_redirects=False)
    assert r.status_code == 307, r.text
    handed = runs_mod.get(r.headers["location"].split("run=")[1].split("&")[0])

    assert not handed.get("withdrawn"), (
        "a transient read error was written down as a withdrawal")
    assert not runs_mod.get(live_run["run_id"]).get("withdrawn"), (
        "it reached the participant's original run too")
    assert not store.get_participant(live_pid).get("withdrawn"), (
        "and their participant record, which is what every gate now reads")

    # And it did not outlive the blip: the next, healthy arrival under their own
    # key is an ordinary one.
    monkeypatch.setattr(appmod, "participant_withdrawal", real)
    r2 = client.get("/start", params={"pid": "PKEYLIVE"}, follow_redirects=False)
    later = runs_mod.get(r2.headers["location"].split("run=")[1].split("&")[0])
    assert not later.get("withdrawn"), (
        "the blip's stamp was inherited by the next arrival under this key")


def test_a_store_read_error_still_refuses_capture(pair, client, sessions_root,
                                                  s3, monkeypatch):
    """Positive control for the test above, and the half that keeps it honest.

    Not writing the stamp must not mean not refusing. While the store cannot be
    read the gate still says no to everything that records, stores or spends,
    because the file it cannot read may be the one carrying the stop.
    """
    def broken(pid):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(appmod, "participant_withdrawal", broken)
    r = client.put(f"/api/sessions/{LIVE_SID}/video",
                   params={"participant_id": pair["live_pid"]},
                   content=b"\0" * 4096)
    assert r.status_code == 403, r.text


def test_one_unreadable_file_does_not_refuse_a_record_no_run_names(
        client, store, runs_mod, sessions_root, s3):
    """The fail-closed rule was bounded by "which run names this record", and a
    great many records are named by no run at all.

    /start mints the record and then writes the pointer onto the run, and that
    write-back can fail on its own — the handler retries it once and logs. For
    every participant left in that state the scan could not name a run, so ONE
    stray half-written file anywhere in the runs directory refused them:
    capture, uploads, their encounter. The record still knows the participant
    key it was minted under, and a run under that key is the same person.
    """
    runs_mod.create("PKEYORPH", qualtrics_id="R_o", cohort="study")
    pid = store.create_participant(code="PKEYORPH", consent_given=True,
                                   consent_version="v1")
    # participant_record_id deliberately never written back onto the run.
    (runs_mod.RUNS_DIR / "bbbbbbbbbbbb.json").write_text("{ nope",
                                                         encoding="utf-8")

    assert appmod._withdrawn(pid) is False
    assert appmod._consented_participant(pid) is not None
    sdir = _session(sessions_root, SPARE_LIVE_SID, pid)
    r = client.put(f"/api/sessions/{SPARE_LIVE_SID}/video",
                   params={"participant_id": pid}, content=b"\0" * 4096)
    assert r.status_code == 200, r.text
    assert (sdir / "webcam.webm").exists()


def test_one_unreadable_file_still_cannot_un_withdraw_a_participant(
        client, store, runs_mod):
    """Positive control for the test above: the narrowing is about identifying
    the person, not about believing a directory it could not read.

    Here the record's own run IS the unreadable file and no other run of theirs
    exists, so nothing identifies them and the gate still refuses.
    """
    run = runs_mod.create("PKEYORPH2", qualtrics_id="R_o2", cohort="study")
    pid = store.create_participant(code="PKEYORPH2", consent_given=True,
                                   consent_version="v1")
    (runs_mod.RUNS_DIR / f"{run['run_id']}.json").write_text(
        "{ half written", encoding="utf-8")
    assert appmod._withdrawn(pid) is True
    assert appmod._consented_participant(pid) is None


def test_a_participant_presenting_their_own_record_is_still_stopped(
        pair, client, runs_mod):
    """Positive control: the ownership check must not cost a withdrawn person
    their own stop. Their key and their own record id together — which is what
    this platform's own redirect carries — still returns the stopped run and
    enrols nobody."""
    before = len(list(runs_mod.RUNS_DIR.glob("*.json")))
    r = client.get("/start", params={"pid": "PKEY002",
                                     "participant_id": pair["gone_pid"]},
                   follow_redirects=False)
    assert r.status_code == 307, r.text
    assert len(list(runs_mod.RUNS_DIR.glob("*.json"))) == before, (
        "a withdrawn participant was enrolled again")
    handed = runs_mod.get(r.headers["location"].split("run=")[1].split("&")[0])
    assert handed["withdrawn"], "their stop was dropped by the ownership check"
