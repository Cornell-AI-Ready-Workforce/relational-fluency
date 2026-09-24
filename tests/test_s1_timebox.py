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

    # The end of some other reply does not: the note is still pending, so
    # the closing line has not been spoken (P6 review).
    await runner._maybe_advance()
    assert not advanced
    # The turn that speaks the closing line takes the note at its finalize,
    # and moves the encounter on.
    assert runner._take_direction()["source"] == "handoff"
    await runner._maybe_advance()
    assert advanced == [0]
    tb = _events(runner, "interaction_timeboxed")[-1]
    assert tb["interaction"] == "i1" and tb["unreached_triggers"] == ["t1_retaliation_fork"]


def test_the_gate_paced_forms_are_untouched(on_model):
    on_model(GPT)
    runner, _ = make_runner("S2A")
    assert runner._timebox_seconds() is None
    assert runner._next_beat_hint() is not None


# --------------------------------------------------------------------------
# The proactive hand-off (pipeline 2026-09-24a). The P5 S1A sim: past the
# two minutes, a participant who said nothing from 95 s to 145 s was not
# handed on, because the closing line was briefed only at the end of their
# next turn (155.7 s) and probed 12 s after that (172.6 s).
# --------------------------------------------------------------------------

def _timeboxed(monkeypatch, *, rt=None):
    runner, _ = make_runner("S1A")
    runner.rt = rt or FakeRT()
    sent = []

    async def _send(m):
        sent.append(m)
    runner._send = _send
    advanced = []

    async def _advance():
        advanced.append(runner.segment)
        return True
    runner._advance_segment = _advance

    async def _deliver(rt, instructions):
        rt.updates.append(instructions) if hasattr(rt, "updates") else None
        return True
    runner._deliver_brief = _deliver

    async def _no():
        return False
    runner._at_ceiling = _no
    runner._trigger_idx = len(runner._triggers())
    now = time.time()
    runner._interaction_started_at = now - 121
    runner._last_activity = now - 4          # 4 s since the last reply played
    runner._play_cursor = now - 4
    return runner, advanced


def test_a_silent_participant_is_handed_off_once_the_timebox_is_up(on_model, monkeypatch):
    on_model(GPT)

    async def go():
        runner, advanced = _timeboxed(monkeypatch)
        assert await runner._proactive_handoff() is True
        assert runner._handoff_briefed and runner._handoff_probed
        assert runner.rt.committed == 1, "the character is made to say it"
        (hb,) = _events(runner, "handoff_briefed")
        assert hb["next"] == "Sam" and hb["seconds"] >= 120
        (hp,) = _events(runner, "handoff_probed")
        assert hp["interaction"] == "i1"
        assert _events(runner, "stage_direction")[-1]["source"] == "handoff"
        # Asked once: the next tick does nothing more.
        assert await runner._proactive_handoff() is False
        assert runner.rt.committed == 1
        # The turn that speaks the closing line moves the encounter on, as
        # before: its finalize takes the note, then advances.
        runner._take_direction()
        await runner._maybe_advance()
        assert advanced == [0]
        assert _events(runner, "interaction_timeboxed")
    asyncio.run(go())


def test_the_hand_off_waits_for_quiet_after_playback(on_model, monkeypatch):
    on_model(GPT)

    async def go():
        runner, _ = _timeboxed(monkeypatch)
        runner._last_activity = time.time() - 1          # they just spoke
        assert await runner._proactive_handoff() is True  # other probes wait
        assert not runner._handoff_briefed and runner.rt.committed == 0
        runner._last_activity = time.time() - 10
        runner._play_cursor = time.time() + 5             # a reply still playing
        assert await runner._proactive_handoff() is True
        assert not runner._handoff_briefed
        runner._play_cursor = time.time() - 3.5
        assert await runner._proactive_handoff() is True
        assert runner._handoff_briefed and runner.rt.committed == 1
    asyncio.run(go())


def test_the_hand_off_is_not_asked_for_over_a_reply_in_flight(on_model, monkeypatch):
    on_model(GPT)

    async def go():
        runner, _ = _timeboxed(monkeypatch)
        runner.rt.responding = True
        assert await runner._proactive_handoff() is True
        assert not runner._handoff_briefed and runner.rt.committed == 0
        runner.rt.responding = False
        await runner._proactive_handoff()
        assert runner._handoff_probed
    asyncio.run(go())


def test_before_the_timebox_nothing_changes(on_model, monkeypatch):
    on_model(GPT)

    async def go():
        runner, _ = _timeboxed(monkeypatch)
        runner._interaction_started_at = time.time() - 60
        assert await runner._proactive_handoff() is False
        assert not runner._handoff_briefed and runner.rt.committed == 0
    asyncio.run(go())


def test_a_refused_probe_is_tried_again(on_model, monkeypatch):
    on_model(GPT)

    class Busy(FakeRT):
        async def commit_turn(self):
            self.committed += 1
            return False if self.committed == 1 else True

    async def go():
        runner, _ = _timeboxed(monkeypatch, rt=Busy())
        await runner._proactive_handoff()
        assert runner._handoff_briefed and not runner._handoff_probed
        await runner._proactive_handoff()
        assert runner._handoff_probed and runner.rt.committed == 2
        assert len(_events(runner, "handoff_briefed")) == 1
    asyncio.run(go())


def test_the_watchdog_hands_off_on_its_own_and_the_pad_is_a_probe(on_model, monkeypatch):
    """Through the real silence watchdog and the real gpt bridge: the pad is
    committed on a cleared buffer and tagged as a probe, so its transcript is
    suppressed as probe_pad (tests/test_participant_turn_integrity.py)."""
    on_model(GPT)
    monkeypatch.setenv("PROBE_TICK_SECONDS", "0.05")
    monkeypatch.setenv("HANDOFF_IDLE_S", "0.2")
    from test_participant_turn_integrity import bridge
    rt = bridge()

    async def go():
        runner, _ = _timeboxed(monkeypatch, rt=rt)
        runner._last_activity = runner._play_cursor = time.time()
        task = asyncio.ensure_future(runner._silence_watchdog())
        started = time.time()
        while not runner._handoff_probed and time.time() - started < 3:
            await asyncio.sleep(0.05)
        runner._closed = True
        await asyncio.wait_for(task, 2)
        assert runner._handoff_probed
        assert 0.2 <= time.time() - started < 2.5
        assert rt._commit_tags[-1]["probe"] is True
        assert "input_audio_buffer.clear" in rt.ws.types()
    asyncio.run(go())


def test_the_hand_off_clock_is_a_knob(monkeypatch):
    from server import llm
    from server.voice import realtime as R
    assert R.handoff_idle_s() == 3.0
    monkeypatch.setenv("HANDOFF_IDLE_S", "5")
    assert llm.provenance(GPT)["pacing"]["handoff_idle_s"] == 5.0


# --------------------------------------------------------------------------
# P6 review. F1: the proactive hand-off raced a finalize still in _steer, and
# that finalize then advanced over the closing line. F2: a refused _enter
# rolled the hand-off flags back to "not briefed", and the watchdog asked for
# a second closing line.
# --------------------------------------------------------------------------

def test_a_finalize_still_steering_is_not_overtaken_by_the_hand_off(on_model, monkeypatch):
    """S1, timebox 120 s: reply R1 ends past the mark and its finalize
    spends a long time in _steer. The watchdog must not brief and probe the
    closing line meanwhile; R1's own _maybe_advance briefs it, the watchdog
    then probes, and only the closing line's finalize advances."""
    on_model(GPT)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.05")

    async def go():
        runner, advanced = _timeboxed(monkeypatch)
        steering = asyncio.Event()

        async def slow_steer():
            steering.set()
            await asyncio.sleep(0.4)       # the LLM review ("up to eleven seconds")
        runner._steer = slow_steer
        settled = asyncio.Event()
        settled.set()
        runner._spawn_finalize(runner._finalize_turn(
            runner.agent_id, runner.agent, runner.rt, ["Fair enough."], None,
            settled=settled))
        await asyncio.wait_for(steering.wait(), 2)
        # R1 has played and the participant has been quiet: every other guard
        # of the hand-off is clear.
        runner._last_activity = runner._play_cursor = time.time() - 10
        assert await runner._proactive_handoff() is True
        assert not runner._handoff_briefed and runner.rt.committed == 0
        await asyncio.gather(*runner._finalize_tasks)
        # R1's finalize briefed the closing line and did not advance.
        assert runner._handoff_briefed and not advanced
        assert len(_events(runner, "handoff_briefed")) == 1
        runner._last_activity = runner._play_cursor = time.time() - 10
        assert await runner._proactive_handoff() is True
        assert runner._handoff_probed and runner.rt.committed == 1
        runner._take_direction()            # the closing line's finalize
        await runner._maybe_advance()
        assert advanced == [0]
    asyncio.run(go())


def test_an_earlier_finalize_cannot_advance_over_an_unspoken_closing_line(on_model, monkeypatch):
    on_model(GPT)

    async def go():
        runner, advanced = _timeboxed(monkeypatch)
        assert await runner._proactive_handoff() is True
        assert runner._handoff_probed          # the closing line is being asked for
        # A finalize that was already under way reaches _maybe_advance.
        await runner._maybe_advance()
        assert not advanced
        assert not _events(runner, "interaction_timeboxed")
    asyncio.run(go())


def test_a_refused_advance_keeps_the_hand_off_spent(on_model, monkeypatch):
    on_model(GPT)

    async def go():
        runner, _ = make_runner("S1A")
        runner.rt = FakeRT()

        async def _send(m):
            pass
        runner._send = _send

        async def _deliver(rt, instructions):
            return True
        runner._deliver_brief = _deliver

        async def _no():
            return False
        runner._at_ceiling = _no
        entered = []

        async def _enter(agent, new_interaction):
            entered.append(agent.id)
            return len(entered) > 1            # the gateway refuses once
        runner._enter = _enter
        runner._trigger_idx = len(runner._triggers())
        now = time.time()
        runner._interaction_started_at = now - 125
        runner._last_activity = runner._play_cursor = now - 10
        assert await runner._proactive_handoff() is True
        runner._take_direction()               # the closing line is spoken
        await runner._maybe_advance()          # ... and _enter is refused
        assert entered and runner.segment == 0
        assert runner._handoff_briefed and runner._handoff_probed
        # No second closing line: the watchdog has nothing to ask for.
        assert await runner._proactive_handoff() is False
        assert runner.rt.committed == 1
        assert len(_events(runner, "handoff_briefed")) == 1
        assert len(_events(runner, "handoff_probed")) == 1
        # The next turn's end retries the advance, and it goes through.
        await runner._maybe_advance()
        assert len(entered) == 2
        assert runner.segment == 1 or runner._series_idx == 1
        assert not runner._handoff_briefed and not runner._handoff_probed
    asyncio.run(go())
