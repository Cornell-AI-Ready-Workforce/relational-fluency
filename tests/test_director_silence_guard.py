"""A routed character told to stay silent is dropped, and the drop is recorded.

nto.gemini-3.5-flash-lite (the director since 2026-09-23) wrote "Stay silent
and let them talk" for Jordan on 3 of 77 replayed S3A calls. The hard rules
already forbid naming someone in order to silence them, because a person told
to be silent is still handed the floor and still fills it. These tests hold the
code-side guard that backs the rule up. No gateway: the client is a stub that
returns a canned set_speakers call.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import director as D  # noqa: E402


class _Agent:
    def __init__(self, aid, name):
        self.id, self.name, self.system_prompt = aid, name, f"# You are {name}"


class _Scenario:
    cast = [_Agent("alex", "Alex"), _Agent("jordan", "Jordan"), _Agent("casey", "Casey")]
    scene = "A team meeting after two resignations."
    director_prompt = "Alex challenges; Jordan sits it out; Casey is anxious."
    opener: list = []


def _director(speakers):
    block = SimpleNamespace(type="tool_use", name="set_speakers",
                            input={"speakers": speakers, "rationale": "r"})

    class _Messages:
        async def create(self, **_kw):
            return SimpleNamespace(content=[block], stop_reason="tool_use")

    class _Client:
        messages = _Messages()

        def with_options(self, **_kw):
            return self

    events = []
    d = D.Director(_Scenario(), client=_Client(),
                   on_event=lambda kind, **kw: events.append({"type": kind, **kw}))
    return d, events


def _route(d):
    return asyncio.run(d.route([{"speaker": "user", "text": "Where are we?"}], "Where are we?"))


def test_the_default_is_the_2026_09_23_choice():
    src = (ROOT / "server" / "director.py").read_text(encoding="utf-8")
    assert 'setting("DIRECTOR_MODEL", "nto.gemini-3.5-flash-lite")' in src


@pytest.mark.parametrize("intent", [
    "Stay silent and let them talk; you have not opened your mouth all meeting",
    "Stay quiet.",
    "Just stay quiet for now",
    "Keep quiet and let Alex carry it",
    "Remain silent",
    "Say nothing this turn",
    "Don't speak yet",
    "Do not respond",
    "Hold back and listen",
])
def test_a_silence_direction_is_dropped_and_recorded(intent):
    d, events = _director([
        {"agent_id": "alex", "intent": "Press them on the date."},
        {"agent_id": "jordan", "intent": intent},
    ])
    out = _route(d)
    assert [s["agent_id"] for s in out] == ["alex"]
    dropped = [e for e in events if e["type"] == "director_silence_intent_dropped"]
    assert dropped and dropped[0]["agent_id"] == "jordan" and dropped[0]["intent"] == intent
    decision = [e for e in events if e["type"] == "director_decision"][-1]
    assert decision["speakers"] == ["alex"]


@pytest.mark.parametrize("intent", [
    "Say that you stopped doing the second pass three weeks ago.",
    "You have been quiet since Priya left; answer shortly and hand it back.",
    "Keep it short: one sentence on the review queue.",
    "Listen to what they just offered, then say whether it is real.",
    "Be blunt about the two who left.",
])
def test_a_speaking_direction_that_mentions_quiet_is_kept(intent):
    d, events = _director([{"agent_id": "jordan", "intent": intent}])
    assert [s["agent_id"] for s in _route(d)] == ["jordan"]
    assert not [e for e in events if e["type"] == "director_silence_intent_dropped"]


def test_dropping_a_silenced_speaker_does_not_make_the_next_one_a_repeat():
    """alex, jordan(silenced), alex: the second alex is a consecutive repeat of
    the first only because jordan was removed, and the room should hear alex
    once, not twice in a row."""
    d, _ = _director([
        {"agent_id": "alex", "intent": "Challenge the plan."},
        {"agent_id": "jordan", "intent": "Stay quiet."},
        {"agent_id": "alex", "intent": "Challenge it again."},
    ])
    assert [s["agent_id"] for s in _route(d)] == ["alex"]
