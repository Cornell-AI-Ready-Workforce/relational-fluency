"""The line under each character's role on the room tiles (`display_note`).

A tile says who someone is (`display_role`) and, under it, one thing the
participant already knows about them. The note is participant-facing text in
a blind study, so it may only restate the participant's own situation card:
never the character's research role (`role`, e.g. "excluded; holds the key
insight"), the brief they are given, or what they have been told to do.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server.scenarios_v3 import available, compile_scenario, load_spec

KEY = "test-session-key"


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setattr(appmod, "SESSION_KEY", KEY)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


@pytest.mark.parametrize("sid", available())
def test_every_character_has_a_short_note(sid):
    for aid, a in load_spec(sid)["agents"].items():
        note = a.get("display_note", "")
        assert note, f"{sid}/{aid} has no display_note"
        assert len(note) <= 60, f"{sid}/{aid} note is too long for a tile: {note!r}"


@pytest.mark.parametrize("sid", available())
def test_a_note_never_repeats_the_research_role(sid):
    for aid, a in load_spec(sid)["agents"].items():
        note = a.get("display_note", "").lower()
        research = (a.get("role") or "").lower()
        for word in ("excluded", "insight", "cynic", "disengaged", "anxious",
                     "dominates", "relabels", "neutral", "solo", "urging"):
            assert word not in note, f"{sid}/{aid} note {note!r} names {word!r}"
        assert research not in note, f"{sid}/{aid} note restates its research role"


@pytest.mark.parametrize("sid", available())
def test_the_compiled_cast_carries_the_note(sid):
    spec = load_spec(sid)
    for agent in compile_scenario(sid).cast:
        assert agent.note == spec["agents"][agent.id]["display_note"]


def test_a_participant_is_served_the_note_and_not_the_research_role(keyed):
    d = keyed.get("/api/scenarios/S4A").json()
    by_name = {c["name"]: c for c in d["cast"]}
    assert by_name["Dan"]["note"] == "His draft: every team live on Monday the 9th"
    assert by_name["Priya"]["note"] == "Here to settle the rollout plan"
    assert "insight" not in str(d["cast"]) and "excluded" not in str(d["cast"])
