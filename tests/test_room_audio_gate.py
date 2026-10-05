"""One speaker at a time on the page's audio stream (the voice that changed
mid-sentence in S3 and S4).

The page plays every binary frame on one timeline, in arrival order, with no
speaker tag. A room reply that has started keeps playing after the floor
moves (the `announced` leniency in _pump_member), so the next speaker's chunks
could arrive while the previous one was still streaming, and the page spliced
the two: one voice turning into another inside what sounded like one line.

RealtimeVoiceSessionRunner._send_bytes now gives a room's stream one owner and
queues anyone else's audio until the owner's reply is over.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import test_lost_participant_round as harness  # noqa: E402
from test_lost_participant_round import _short_waits  # noqa: E402,F401

from server import realtime_voice_session as rvs  # noqa: E402

A = b"\x01\x00" * 160
A2 = b"\x02\x00" * 160
B = b"\x03\x00" * 160
B2 = b"\x04\x00" * 160


def _room_runner():
    runner, session, ws = harness.make_runner("S3A")
    runner.room = types.SimpleNamespace(speaking=None)
    return runner, session, ws


def _mid_reply(runner, agent_id, on=True):
    """What _pump_member publishes for a member whose reply is being relayed."""
    runner._member_turns[agent_id] = ([], {"announced": on})


async def _settle(runner, timeout=2.0):
    deadline = time.time() + timeout
    while runner._audio_drain_task is not None and time.time() < deadline:
        await asyncio.sleep(0.02)


@harness.in_a_loop
async def test_a_second_speaker_waits_for_the_first_to_finish():
    runner, session, ws = _room_runner()
    _mid_reply(runner, "alex")
    await runner._send_bytes(A, agent_id="alex")
    await runner._send_bytes(B, agent_id="jordan")
    await runner._send_bytes(A2, agent_id="alex")
    await runner._send_bytes(B2, agent_id="jordan")
    # Alex is still talking: nothing of Jordan's has reached the page.
    assert ws.binary == [A, A2]
    _mid_reply(runner, "alex", on=False)
    await _settle(runner)
    assert ws.binary == [A, A2, B, B2]
    store = session.store
    assert [e["agent_id"] for e in store.of("audio_queued_behind_speaker")] == ["jordan"]
    flushed = store.of("audio_queue_flushed")
    assert len(flushed) == 1 and flushed[0]["idle_release"] is False


@harness.in_a_loop
async def test_chunks_sent_while_draining_stay_in_order():
    runner, session, ws = _room_runner()
    _mid_reply(runner, "alex")
    await runner._send_bytes(A, agent_id="alex")
    await runner._send_bytes(B, agent_id="jordan")
    _mid_reply(runner, "alex", on=False)
    # Jordan's next chunk arrives before the drain has run.
    await runner._send_bytes(B2, agent_id="jordan")
    await _settle(runner)
    assert ws.binary == [A, B, B2]


@harness.in_a_loop
async def test_an_idle_owner_does_not_hold_the_next_speaker_silent(monkeypatch):
    monkeypatch.setattr(rvs, "AUDIO_OWNER_IDLE_S", 0.1)
    runner, session, ws = _room_runner()
    _mid_reply(runner, "alex")  # its finalize never lands
    await runner._send_bytes(A, agent_id="alex")
    await runner._send_bytes(B, agent_id="jordan")
    assert ws.binary == [A]
    await _settle(runner)
    assert ws.binary == [A, B]
    assert session.store.of("audio_queue_flushed")[0]["idle_release"] is True


@harness.in_a_loop
async def test_an_interruption_drops_what_was_waiting():
    runner, session, ws = _room_runner()
    _mid_reply(runner, "alex")
    await runner._send_bytes(A, agent_id="alex")
    await runner._send_bytes(B, agent_id="jordan")
    await runner._send({"type": "assistant_interrupted"})
    _mid_reply(runner, "alex", on=False)
    await _settle(runner)
    assert ws.binary == [A]
    dropped = session.store.of("audio_queue_dropped")
    assert dropped and dropped[0]["audio_ms"] == {"jordan": len(B) // 32}


@harness.in_a_loop
async def test_one_to_one_audio_is_not_gated():
    runner, session, ws = harness.make_runner("S2A")
    await runner._send_bytes(A, agent_id="morgan")
    await runner._send_bytes(B)
    assert ws.binary == [A, B]
    assert not session.store.of("audio_source_switch")
