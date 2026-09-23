"""The S1 timebox: two minutes with the instigating colleague, a briefed
closing line, then the hand-off to the counterpart. Drives the runner's
_maybe_advance with a fake gateway; no network."""
import asyncio, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import pytest
from test_final_runner_voice import make_runner, on_model, GPT  # noqa: F401  (fixtures/helpers)


class FakeRT:
    def __init__(self):
        self.updates = []
        self.responding = False
        self.voice = "coral"
        self.model = "gpt-realtime-2.1"
        self.committed = 0
    async def update_instructions(self, text, **kw):
        self.updates.append(text); return True
    async def send_audio(self, b): pass
    async def commit_turn(self): self.committed += 1


def _events(runner, kind):
    return [e for e in runner.session.store.events if e.get("type") == kind]


def test_s1_hands_off_at_the_timebox(on_model, monkeypatch):
    on_model(GPT)
    asyncio.run(_s1(monkeypatch))


async def _s1(monkeypatch):
    runner, _ = make_runner("S1A")
    runner.rt = FakeRT()
    sent = []
    async def _send(m): sent.append(m)
    runner._send = _send
    advanced = []
    async def _advance():
        advanced.append(runner.segment); return True
    runner._advance_segment = _advance
    async def _deliver(rt, instructions):
        rt.updates.append(instructions); return True
    runner._deliver_brief = _deliver
    async def _no(): return False
    runner._at_ceiling = _no
    runner._trigger_idx = len(runner._triggers())   # t1 already fired
    runner._turns_this_interaction = 3

    # The move-on control is not offered for a timeboxed interaction.
    assert runner._timebox_seconds() == 120
    assert runner._next_beat_hint() is None

    # Under two minutes: nothing happens (the 180 s / 8-turn gate would also hold).
    runner._interaction_started_at = time.time() - 60
    await runner._maybe_advance()
    assert not advanced and not runner._handoff_briefed

    # Past two minutes: the closing line is briefed, recorded, and not yet advanced.
    runner._interaction_started_at = time.time() - 121
    await runner._maybe_advance()
    assert runner._handoff_briefed and not advanced
    note = runner.rt.updates[-1]
    assert "DIRECTOR NOTE" in note and "last turn" in note and "Sam is out by the elevators" in note
    sd = _events(runner, "stage_direction")[-1]
    assert sd["source"] == "handoff" and sd["trigger_id"] is None
    assert _events(runner, "handoff_briefed")[-1]["next"] == "Sam"

    # A planted beat cannot be briefed over the closing line.
    runner._trigger_idx = 0
    await runner._brief_next_beat(probing=False)
    assert not _events(runner, "trigger_fired")

    # The turn that speaks the closing line moves the encounter on.
    await runner._maybe_advance()
    assert advanced == [0]
    tb = _events(runner, "interaction_timeboxed")[-1]
    assert tb["interaction"] == "i1" and tb["unreached_triggers"] == ["t1_retaliation_fork"]


def test_the_gate_paced_forms_are_untouched(on_model):
    on_model(GPT)
    runner, _ = make_runner("S2A")
    assert runner._timebox_seconds() is None
    assert runner._next_beat_hint() is not None
