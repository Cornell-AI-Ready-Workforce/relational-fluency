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
