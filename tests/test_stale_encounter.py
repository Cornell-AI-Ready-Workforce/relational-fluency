"""#296: a page left open on an old encounter must not replay it.

Ben had one run open in Firefox and in Chrome. Firefox finished conversation 1
(S3A) and the run moved on to S4A; Chrome, loaded while the run was still on
S3A, then pressed Start and the voice socket opened S3A again. The socket took
the scenario from the query and never asked the run, so a whole conversation
was recorded as encounter 2 under the wrong scenario, /advance refused it with
a 409, and the page's only answer to that 409 was an exit with no code.

The socket now refuses any scenario that is not the run's current one (and any
once the run is done) before a session exists, except for internal runs, the
operator key and researcher launches, which choose their scenario on purpose.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import app as appmod

V2 = Path(__file__).resolve().parents[1] / "static" / "v2.html"


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
def client(store, monkeypatch):
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture()
def created(monkeypatch):
    """Record every registry.create, and stop there: reaching it is the pass."""
    calls = []

    def fake_create(scenario, **kw):
        calls.append(scenario)
        raise FileNotFoundError(f"test stop: {scenario}")

    monkeypatch.setattr(appmod.registry, "create", fake_create)
    return calls


def _arrival(store, runs_mod, key="RF_296", cohort="study"):
    run = runs_mod.create(key, qualtrics_id=f"R_{key}", cohort=cohort)
    pid = store.create_participant(code=key)
    run["participant_record_id"] = pid
    runs_mod.save(run)
    return run, pid


def _ids(run):
    return [s["id"] for s in run["scenarios"]]


def _socket(client, scenario, pid, **extra):
    q = f"/ws/participant/voice?scenario={scenario}&participant_id={pid}"
    for k, v in extra.items():
        q += f"&{k}={v}"
    return client.websocket_connect(q)


def _refused(client, scenario, pid, **extra):
    with _socket(client, scenario, pid, **extra) as ws:
        msg = ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4409
    return msg


def test_the_current_scenario_opens(client, store, runs_mod, created):
    run, pid = _arrival(store, runs_mod)
    first = _ids(run)[0]
    with _socket(client, first, pid, run=run["run_id"]) as ws:
        assert ws.receive_json()["type"] == "error"   # the fake create's stop
    assert created == [first]


def test_a_stale_page_cannot_replay_the_previous_encounter(client, store,
                                                          runs_mod, created):
    """Ben's case: the run is on encounter 2, the old page asks for encounter 1."""
    run, pid = _arrival(store, runs_mod)
    ids = _ids(run)
    run["index"] = 1
    runs_mod.save(run)

    # The old page sent no ?run=; the new one does. Both are refused.
    for extra in ({}, {"run": run["run_id"]}):
        msg = _refused(client, ids[0], pid, **extra)
        assert msg["type"] == "error" and msg["code"] == "stale_encounter"
        assert msg["current"] == ids[1] and msg["done"] is False
    assert created == []          # no session, so no recording and no upload

    with _socket(client, ids[1], pid) as ws:   # the right one still opens
        ws.receive_json()
    assert created == [ids[1]]


def test_a_finished_run_opens_nothing(client, store, runs_mod, created):
    run, pid = _arrival(store, runs_mod)
    ids = _ids(run)
    run["index"] = len(ids)
    runs_mod.save(run)
    msg = _refused(client, ids[-1], pid)
    assert msg["code"] == "stale_encounter" and msg["done"] is True
    assert msg["current"] is None
    assert created == []


def test_a_run_hint_for_someone_elses_run_does_not_unlock_it(client, store,
                                                            runs_mod, created):
    """?run= is honoured only for the participant's own run (_resolve_run), so
    naming another run that happens to be on the wanted scenario changes nothing."""
    run, pid = _arrival(store, runs_mod, key="RF_296_A")
    other, _ = _arrival(store, runs_mod, key="RF_296_B")
    ids = _ids(run)
    run["index"] = 1
    runs_mod.save(run)
    other["scenarios"] = [dict(s) for s in run["scenarios"]]
    other["index"] = 0
    runs_mod.save(other)
    msg = _refused(client, ids[0], pid, run=other["run_id"])
    assert msg["current"] == ids[1]
    assert created == []


def test_internal_runs_keep_choosing_their_scenario(client, store, runs_mod,
                                                   created):
    """tools/sim and tools/load enter through /test (cohort=internal) and open
    whichever scenario they are measuring, whatever the run's order says."""
    run, pid = _arrival(store, runs_mod, key="RF_296_INT", cohort="internal")
    last = _ids(run)[-1]
    with _socket(client, last, pid, run=run["run_id"]) as ws:
        ws.receive_json()
    assert created == [last]


def test_the_operator_key_is_exempt(client, store, runs_mod, created,
                                    monkeypatch):
    run, pid = _arrival(store, runs_mod, key="RF_296_OP")
    run["index"] = 1
    runs_mod.save(run)
    monkeypatch.setattr(appmod, "SESSION_KEY", "op-key", raising=False)
    first = _ids(run)[0]
    with _socket(client, first, pid, key="op-key") as ws:
        ws.receive_json()
    assert created == [first]


def test_a_participant_with_no_run_is_not_checked(client, store, created):
    pid = store.create_participant(code="RF_296_DIRECT")
    with _socket(client, "S2A", pid) as ws:
        ws.receive_json()
    assert created == ["S2A"]


def test_the_page_rechecks_the_run_and_answers_the_refusal():
    """The page half, by its source: re-ask the run before Start, send the run
    on the socket, reload on stale_encounter, and give the scenario-mismatch
    409 a way on instead of the no-code exit."""
    src = V2.read_text(encoding="utf-8")
    start = src.index("async function startSession()")
    assert "if (await runMovedOn()) { reloadIntoRun(); return; }" in src[start:start + 400]
    assert "const runParam = runId ? `&run=${encodeURIComponent(runId)}` : '';" in src
    assert "${pidParam}${runParam}" in src
    assert "m.code === 'stale_encounter'" in src
    assert "/does not match current encounter/.test(detail)" in src
