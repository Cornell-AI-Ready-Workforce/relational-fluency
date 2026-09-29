"""What the scenario routes tell a participant, and what they tell a researcher.

Issue #38, seen on production on 2026-09-25 walking the intro of
/v2?scenario=S4A: the page header and the situation card both read "Planning an
internal rollout (teamwork, var. A)". Every participant was told the skill being
measured and which parallel form they had drawn, on a study that keeps its
raters blind to both, and the scenario request in the network tab carried the
characters' persona dials — the manipulation itself — beside it.

The fix has two halves and both are pinned here:

*   The title is the spec's own title. It is the string the header, the
    situation card and the voice socket's `session` frame show, so it is the
    participant's string.
*   The skill, the form and the persona dials are served to the researcher's
    pages as fields of their own (the key they already send), and not to a
    participant. Nothing a researcher page reads may lose information: the
    launch card still gets the personas, the pickers still get skill and
    variant, the session listing gains them because it used to read them out
    of the title.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server.scenarios_v3 import available, compile_scenario, load_spec

KEY = "test-session-key"


@pytest.fixture()
def keyed(monkeypatch):
    """A deployment with a researcher key, which is what production is."""
    monkeypatch.setattr(appmod, "SESSION_KEY", KEY)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


def _names_a_construct(title: str, spec: dict) -> bool:
    construct = spec["construct"].replace("_", " ")
    return construct in title.lower() or "var." in title or f"({spec['variant']})" in title


# --- the title ---------------------------------------------------------------

@pytest.mark.parametrize("sid", available())
def test_the_title_is_the_specs_own_and_names_no_skill_or_form(sid):
    spec = load_spec(sid)
    title = compile_scenario(sid).title
    assert title == spec["title"]
    assert not _names_a_construct(title, spec), (
        f"{sid}: the participant's header reads {title!r}, which names the "
        f"skill ({spec['construct']}) or the form ({spec['variant']})")


def test_the_voice_socket_session_frame_sends_the_same_title():
    """The `session` frame repaints the header from scenario.title; the page
    has no other title to show once the conversation starts."""
    import inspect

    src = inspect.getsource(appmod)
    frame = src[src.index('"type": "session"'):][:400]
    assert '"title": session.scenario.title' in frame


# --- what a participant is served --------------------------------------------

def test_a_participant_gets_no_skill_form_or_persona_dials(keyed):
    r = keyed.get("/api/scenarios/S4A")
    assert r.status_code == 200
    d = r.json()
    assert d["title"] == "Planning an internal rollout"
    for field in ("skill", "variant", "personas", "parallel_form"):
        assert field not in d, f"a participant was served {field!r}: {d.get(field)!r}"
    # Everything their own page reads is still there.
    for field in ("id", "title", "intro", "briefing", "mode", "cast", "intro_image"):
        assert field in d, f"the participant page lost {field!r}"
    assert "var. " not in r.text and '"teamwork"' not in r.text


def test_a_participant_listing_carries_no_skill_or_form(keyed):
    rows = keyed.get("/api/scenarios").json()
    assert rows, "the listing is empty"
    for row in rows:
        assert "skill" not in row and "variant" not in row and "parallel_form" not in row, row
        assert "(" not in row["title"], row


# --- the run a participant is on (issue #38, second half) ---------------------
#
# The scenario routes were half of it. GET /api/run/{id} is what the page itself
# requests on every /v2?run= link (and /advance and /withdraw answer with the
# same view), and it sent each encounter as the run built it: the construct,
# the form, its sibling form, and the title and construct of the NEXT
# encounter, which the page takes care never to preview. The page reads
# run.current.id and nothing else of either.

@pytest.fixture()
def a_run(keyed, tmp_path, monkeypatch):
    from server import runs, storage

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    storage.init_storage()
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    run = runs.create("RF_PROBE_1", seed=7)
    run["participant_record_id"] = "p_1790365076_2bb22c"
    runs.save(run)
    return run


def _leaks(view: dict, run: dict) -> list:
    text = __import__("json").dumps(view)
    found = [f for f in ("construct", "variant", "parallel_form") if f'"{f}"' in text]
    for entry in run["scenarios"]:
        if entry["construct"] in text:
            found.append(entry["construct"])
    nxt = run["scenarios"][run["index"] + 1]
    if nxt["title"] in text:
        found.append(f"the next encounter's title {nxt['title']!r}")
    return found


def test_a_participant_is_not_told_what_their_run_measures(keyed, a_run):
    r = keyed.get(f"/api/run/{a_run['run_id']}")
    assert r.status_code == 200, r.text
    view = r.json()
    assert not _leaks(view, a_run), _leaks(view, a_run)
    first = a_run["scenarios"][0]
    # What the page reads is still there.
    assert view["current"] == {"id": first["id"], "title": first["title"]}
    assert "next" not in view, "a participant is not previewed the next encounter"
    for field in ("run_id", "position", "total", "done", "completed", "withdrawn",
                  "timing", "completion_code", "cohort"):
        assert field in view, f"the participant page lost {field!r}"


def test_nor_by_the_other_routes_that_answer_with_the_run(keyed, a_run):
    rid = a_run["run_id"]
    r = keyed.post(f"/api/run/{rid}/withdraw",
                   json={"participant_id": a_run["participant_record_id"]})
    assert r.status_code == 200, r.text
    assert not _leaks(r.json(), a_run), _leaks(r.json(), a_run)


def test_the_researcher_still_gets_the_run_as_it_was_built(keyed, a_run):
    view = keyed.get(f"/api/run/{a_run['run_id']}", params={"key": KEY}).json()
    assert view["current"] == a_run["scenarios"][0]
    assert view["next"] == a_run["scenarios"][1]


def test_every_route_that_returns_a_run_goes_through_the_participant_view():
    import inspect

    src = inspect.getsource(appmod)
    assert "return runs.view(" not in src, (
        "a route returns the raw run view; use _run_view(run, key)")


# --- what a researcher is served ---------------------------------------------

def test_the_researcher_still_gets_skill_form_and_persona_dials(keyed):
    d = keyed.get("/api/scenarios/S4A", params={"key": KEY}).json()
    assert d["skill"] == "teamwork"
    assert d["variant"] == "A"
    assert d["personas"], "the launch card's default gears are gone"
    rows = {r["id"]: r for r in keyed.get("/api/scenarios", params={"key": KEY}).json()}
    assert rows["S1B"]["skill"] == "conflict_management"
    assert rows["S1B"]["variant"] == "B"


def test_a_keyless_development_box_is_the_researcher(monkeypatch):
    """No SESSION_KEY is a laptop, where check_key waves everybody through the
    whole dataset: hiding three fields there protects nothing and would strip
    the launch card of its defaults."""
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    d = TestClient(appmod.app).get("/api/scenarios/S1A").json()
    assert d["skill"] == "conflict_management" and d["personas"]


def test_the_session_listing_names_skill_and_form_as_fields(keyed, tmp_path, monkeypatch):
    """The researcher console's session picker read the skill and the form out
    of the title. The title no longer has them, so the rows carry them."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, scenario TEXT, model TEXT,"
                 " started_at TEXT, n_turns INT, status TEXT, duration_s REAL,"
                 " run_id TEXT, cohort TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s_1','S2A','m','2026-09-25',8,"
                 "'closed',420.0,'r1','study')")
    conn.commit()
    conn.close()
    from server import storage
    monkeypatch.setattr(storage, "DB_PATH", db)
    rows = keyed.get("/api/sessions", params={"key": KEY}).json()
    row = next(r for r in rows if r["id"] == "s_1")
    assert row["title"] == "Promised raise and a competing offer"
    assert row["skill"] == "influence" and row["variant"] == "A"


def test_the_researcher_console_prints_skill_and_form_from_their_own_fields():
    """Read, because the console is a page: every picker that printed the
    title prints the tag built from `skill` and `variant` beside it."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "static" / "researcher.html").read_text(
        encoding="utf-8")
    assert "function researchTag(s)" in src
    assert src.count("researchTag(s)") >= 4, "a picker still prints the bare title"


# --- issue #46: which model the participant talks to -------------------------
#
# The scenario data said `"model": "nto.gemini-3.1-flash-lite"`. That is the
# text model (director, steering, and what a launch's ?model= overrides); the
# characters the participant speaks with are played by the realtime model. The
# two are named separately now, and `model` keeps the value it always had so
# the launch card that preselects from it is not broken.

def test_the_scenario_data_names_the_voice_model_and_the_text_model(keyed, monkeypatch):
    from server.voice import realtime as bridge

    monkeypatch.setattr(bridge, "MODEL", "gpt-realtime-2.1")
    for params in ({}, {"key": KEY}):          # a participant, and the researcher
        d = keyed.get("/api/scenarios/S2A", params=params).json()
        assert d["realtime_model"] == "gpt-realtime-2.1", d
        assert d["text_model"] == appmod.DEFAULT_MODEL, d
        assert d["text_model"] != d["realtime_model"]
        # The old field, for its old readers: the same value it always had.
        assert d["model"] == d["text_model"]


def test_the_voice_model_is_the_one_a_session_would_record(keyed):
    """The same source the manifest's realtime_model is read from, so the
    scenario data and the record cannot disagree about one deployment."""
    from server.session import _realtime_model_name

    d = keyed.get("/api/scenarios/S1A").json()
    assert d["realtime_model"] == (_realtime_model_name() or None)


def test_the_launch_card_preselects_the_text_model_by_its_own_name():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "static" / "researcher.html").read_text(
        encoding="utf-8")
    assert "launchDetail.text_model || launchDetail.model" in src
