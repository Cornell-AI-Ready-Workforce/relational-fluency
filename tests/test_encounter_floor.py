"""The encounter clock: a seven-minute floor, a twelve-minute wrap, a
thirteen-minute stop (docs/study1-plan.md, E4).

The floor is enforced in two places and this file holds both to the same
numbers: the runner (auto-advance, the actor's end tool, the participant's
move-on) and POST /api/run/{id}/advance, the backstop for the page's End button.
Withdrawal is never gated; internal runs are exempt from the floor.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import app as appmod  # noqa: E402
from server import storage  # noqa: E402
import test_lost_participant_round as harness  # noqa: E402  (FakeSession / make_runner)


# ---------------------------------------------------------------------------
# the numbers
# ---------------------------------------------------------------------------

def test_the_defaults_are_the_studys_numbers(monkeypatch):
    for k in ("ENCOUNTER_MIN_SECONDS", "ENCOUNTER_WRAP_SECONDS", "ENCOUNTER_MAX_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    assert storage.encounter_timing() == {"min_seconds": 420.0, "wrap_seconds": 720.0, "max_seconds": 780.0}


def test_the_environment_overrides_them(monkeypatch):
    monkeypatch.setenv("ENCOUNTER_MIN_SECONDS", "300")
    monkeypatch.setenv("ENCOUNTER_WRAP_SECONDS", "600")
    monkeypatch.setenv("ENCOUNTER_MAX_SECONDS", "660")
    assert storage.encounter_timing() == {"min_seconds": 300.0, "wrap_seconds": 600.0, "max_seconds": 660.0}


def test_an_unusable_value_falls_back_rather_than_zeroing_the_floor(monkeypatch, capsys):
    monkeypatch.setenv("ENCOUNTER_MIN_SECONDS", "seven minutes")
    assert storage.encounter_timing()["min_seconds"] == 420.0
    assert "ENCOUNTER_MIN_SECONDS" in capsys.readouterr().out


def test_the_wrap_and_stop_cannot_sit_below_the_floor(monkeypatch):
    monkeypatch.setenv("ENCOUNTER_MIN_SECONDS", "500")
    monkeypatch.setenv("ENCOUNTER_WRAP_SECONDS", "100")
    monkeypatch.setenv("ENCOUNTER_MAX_SECONDS", "200")
    t = storage.encounter_timing()
    assert t["max_seconds"] >= t["min_seconds"] and t["min_seconds"] <= t["wrap_seconds"] <= t["max_seconds"]


def test_the_run_view_carries_the_clock(tmp_path, monkeypatch):
    from server import runs
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("ENCOUNTER_MIN_SECONDS", "300")
    run = runs.create("P_TIMING", qualtrics_id="R_T", cohort="internal")
    assert runs.view(run)["timing"]["min_seconds"] == 300.0


# ---------------------------------------------------------------------------
# the advance route
# ---------------------------------------------------------------------------

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
def sessions_root(tmp_path, monkeypatch, store):
    from server import video
    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(appmod, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    return root


@pytest.fixture()
def client(store, sessions_root, monkeypatch):
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS", list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


def _enrol(store, runs_mod, key, cohort="study"):
    run = runs_mod.create(key, qualtrics_id=f"R_{key}", cohort=cohort, variant="A")
    pid = store.create_participant(code=key)
    run["participant_record_id"] = pid
    runs_mod.save(run)
    return run, pid


def _encounter(sessions_root, run, pid, sid, *, started_ago=None, duration_s=None):
    sdir = sessions_root / sid
    sdir.mkdir(parents=True, exist_ok=True)
    manifest = {"session_id": sid, "participant_id": pid,
                "scenario": run["scenarios"][0]["id"], "status": "closed", "n_turns": 6}
    if started_ago is not None:
        manifest["started_at"] = time.time() - started_ago
    if duration_s is not None:
        manifest["duration_s"] = duration_s
    (sdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (sdir / "events.jsonl").write_text(
        json.dumps({"type": "user_transcript", "final": True, "text": "hello"}) + "\n", encoding="utf-8")
    return sdir


def test_a_study_encounter_under_the_floor_is_refused_with_a_reason(client, store, runs_mod, sessions_root):
    run, pid = _enrol(store, runs_mod, "PKEY_FLOOR")
    _encounter(sessions_root, run, pid, "s_1772460300_f10001", started_ago=100)
    r = client.post(f"/api/run/{run['run_id']}/advance", params={"session_id": "s_1772460300_f10001"})
    assert r.status_code == 409, r.text
    assert "asks for" in r.json()["detail"] and "Stop and leave" in r.json()["detail"]
    assert runs_mod.get(run["run_id"])["index"] == 0, "the run advanced anyway"


def test_an_encounter_past_the_floor_advances(client, store, runs_mod, sessions_root):
    run, pid = _enrol(store, runs_mod, "PKEY_OK")
    _encounter(sessions_root, run, pid, "s_1772460300_f10002", duration_s=431.2)
    r = client.post(f"/api/run/{run['run_id']}/advance", params={"session_id": "s_1772460300_f10002"})
    assert r.status_code == 200, r.text
    assert r.json()["position"] == 2


def test_the_closed_manifests_duration_is_what_counts_not_the_clock_now(client, store, runs_mod, sessions_root):
    """A participant who finished at 7:10 and pressed End an hour later is not
    refused, and one who finished at 3:00 is not admitted by waiting."""
    run, pid = _enrol(store, runs_mod, "PKEY_DUR")
    _encounter(sessions_root, run, pid, "s_1772460300_f10003", started_ago=3600, duration_s=180.0)
    r = client.post(f"/api/run/{run['run_id']}/advance", params={"session_id": "s_1772460300_f10003"})
    assert r.status_code == 409, r.text


def test_internal_runs_are_exempt(client, store, runs_mod, sessions_root):
    run, pid = _enrol(store, runs_mod, "PKEY_INT", cohort="internal")
    _encounter(sessions_root, run, pid, "s_1772460300_f10004", started_ago=30)
    r = client.post(f"/api/run/{run['run_id']}/advance", params={"session_id": "s_1772460300_f10004"})
    assert r.status_code == 200, r.text


def test_a_lowered_floor_is_honoured(client, store, runs_mod, sessions_root, monkeypatch):
    monkeypatch.setenv("ENCOUNTER_MIN_SECONDS", "60")
    run, pid = _enrol(store, runs_mod, "PKEY_LOW")
    _encounter(sessions_root, run, pid, "s_1772460300_f10005", duration_s=90)
    r = client.post(f"/api/run/{run['run_id']}/advance", params={"session_id": "s_1772460300_f10005"})
    assert r.status_code == 200, r.text


def test_withdrawal_is_never_gated(client, store, runs_mod, sessions_root):
    run, pid = _enrol(store, runs_mod, "PKEY_WD")
    _encounter(sessions_root, run, pid, "s_1772460300_f10006", started_ago=20)
    r = client.post(f"/api/run/{run['run_id']}/withdraw",
                    json={"participant_id": pid, "session_id": "s_1772460300_f10006"})
    assert r.status_code == 200, r.text
    assert r.json()["withdrawn"]


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _study_numbers(monkeypatch):
    for k in ("ENCOUNTER_MIN_SECONDS", "ENCOUNTER_WRAP_SECONDS", "ENCOUNTER_MAX_SECONDS"):
        monkeypatch.delenv(k, raising=False)


def _last_segment(runner):
    runner.segment = len(runner.interactions) - 1
    runner._series_idx = 0


def test_only_the_final_interaction_is_the_last_segment():
    runner, _, _ = harness.make_runner("S1A")      # two 1:1 interactions
    assert runner._is_last_segment() is False
    _last_segment(runner)
    assert runner._is_last_segment() is True


def test_moving_on_between_interactions_is_never_held():
    runner, session, ws = harness.make_runner("S1A")
    assert _run(runner._hold_at_floor("move_on")) is False
    assert not session.store.of("floor_held") and not ws.frames("floor_held")


def test_the_last_interaction_is_held_until_the_floor_and_says_so_once():
    runner, session, ws = harness.make_runner("S1A")
    _last_segment(runner)
    assert _run(runner._hold_at_floor("move_on")) is True
    assert _run(runner._hold_at_floor("end_conversation")) is True
    held = session.store.of("floor_held")
    assert len(held) == 1 and held[0]["reason"] == "move_on" and held[0]["seconds_left"] > 400
    assert len(ws.frames("floor_held")) == 2, "the page is told each time; the record once"


def test_past_the_floor_nothing_is_held():
    runner, session, ws = harness.make_runner("S1A")
    _last_segment(runner)
    runner._encounter_started_at = time.time() - 421
    assert _run(runner._hold_at_floor("move_on")) is False
    assert not session.store.of("floor_held")


def test_the_participants_move_on_does_not_complete_an_early_encounter(monkeypatch):
    runner, session, ws = harness.make_runner("S1A")
    _last_segment(runner)
    calls = []

    async def advance():
        calls.append(1)
        return False
    monkeypatch.setattr(runner, "_advance_segment", advance)
    _run(runner._handle_client_command(json.dumps({"type": "advance_interaction"})))
    assert calls == [] and not ws.frames("encounter_complete")
    assert ws.frames("floor_held")
    runner._encounter_started_at = time.time() - 500
    _run(runner._handle_client_command(json.dumps({"type": "advance_interaction"})))
    assert calls == [1] and ws.frames("encounter_complete")


def test_the_actors_end_tool_is_held_the_same_way(monkeypatch):
    runner, session, ws = harness.make_runner("S1A")
    _last_segment(runner)
    calls = []

    async def advance():
        calls.append(1)
        return False
    monkeypatch.setattr(runner, "_advance_segment", advance)
    _run(runner._advance_from_tool())
    assert calls == [] and session.store.of("floor_held")[0]["reason"] == "end_conversation"


def test_the_wrap_is_called_once_and_the_stop_completes_the_encounter():
    runner, session, ws = harness.make_runner("S1A")
    runner._encounter_started_at = time.time() - 730       # past 12:00, before 13:00
    assert _run(runner._at_ceiling()) is False
    assert _run(runner._at_ceiling()) is False
    assert len(session.store.of("ceiling_wrap")) == 1 and len(ws.frames("wrap_up")) == 1
    runner._encounter_started_at = time.time() - 790       # past 13:00
    assert _run(runner._at_ceiling()) is True
    done = ws.frames("encounter_complete")
    assert done and done[-1]["reason"] == "ceiling"
    assert session.store.of("ceiling_reached")


def test_the_stop_outranks_planted_beats_still_waiting(monkeypatch):
    runner, session, ws = harness.make_runner("S1A")
    runner._encounter_started_at = time.time() - 800
    monkeypatch.setattr(runner, "_next_trigger", lambda: {"id": "t_still_waiting"})
    _run(runner._maybe_advance())
    assert ws.frames("encounter_complete") and session.store.of("ceiling_reached")


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

def test_the_page_takes_its_clock_from_the_run_and_holds_end_until_the_floor():
    src = (ROOT / "static" / "v2.html").read_text(encoding="utf-8")
    assert "applyTiming(run.timing)" in src
    early = src[src.index("$('stopBtn').addEventListener('click'"):]
    early = early[:early.index("if (!confirm('Finish this conversation and move on?'))")]
    assert "return;" in early and "endSession()" not in early, \
        "End before the floor must not end the session; it says why and stays"
    assert "Stop and leave the study" in early, "the note names the control that is never held"
    assert "s >= MAX_S && started && !ceilingFired" in src, "the page has no hard stop of its own"
    for frame in ("'floor_held'", "'wrap_up'"):
        assert f"m.type === {frame}" in src, f"the page ignores {frame}"
    assert "/asks for/.test(detail)" in src, "a 409 from the floor is rendered as a dead end"
