"""Cascade cancellation policy, pipeline 2026-11-12b, stacked on STT 11-12a.

Use the real adapter and bridge. Reply cancellation is separate from the STT
switch because every default-tail cascade cancel inherits the discard policy.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager

import pytest

from server import llm
from server.voice import realtime as R
from test_cascade_transcribe import (  # shared HTTP/TTS fixtures, no network
    MODEL, Hearing, bridge, completed, config, until,
)
from test_participant_turn_integrity import LOUD, in_a_loop, runner_for, send_ms


def test_cascade_discard_policy_has_its_own_version_and_rollback(monkeypatch):
    assert llm.PIPELINE_VERSION == "2026-11-12b"
    assert llm.provenance(MODEL)["cancelled_output"] == "discard"
    assert R.discards_cancelled_output(MODEL)
    assert not R.discards_cancelled_output("nto.gemini-live-2.5-flash-native-audio")
    monkeypatch.setenv("CANCELLED_OUTPUT_DISCARD", "0")
    assert llm.provenance(MODEL)["cancelled_output"] == "relay"
    assert not R.discards_cancelled_output(MODEL)


@pytest.mark.parametrize("policy", ["discard", "hold", "rollback"])
@in_a_loop
async def test_already_queued_frames_follow_the_cancel_policy(monkeypatch, policy):
    if policy == "rollback":
        monkeypatch.setenv("CANCELLED_OUTPUT_DISCARD", "0")
    async with bridge(monkeypatch, []) as (rt, sock, _, _):
        await rt.request_response()
        stream = rt.events()
        try:
            while True:
                event = await asyncio.wait_for(anext(stream), 2)
                if event["type"] == "agent_transcript_delta":
                    break
            rid = rt._response_created_id
            assert rid
            # The frame is already in the adapter's receive queue when the
            # cancel is sent. It has not reached the runner or page yet.
            sock._emit({"type": "response.output_audio.delta", "response_id": rid,
                        "item_id": sock.items[-1]["id"],
                        "delta": base64.b64encode(b"\x01\x00" * 4800).decode("ascii")})
            await rt.cancel_response(discard_tail=policy != "hold")
            events = []
            while True:
                event = await asyncio.wait_for(anext(stream), 2)
                events.append(event)
                if event["type"] == "response_done":
                    break
            if policy == "discard":
                assert not any(e["type"] == "agent_audio" for e in events)
                (dropped,) = [e for e in events if e["type"] == "cancelled_output"]
                assert dropped["audio_ms"] == 200
                assert dropped["response_id"] == rid
            else:
                assert any(e["type"] == "agent_audio" for e in events)
                assert not any(e["type"] == "cancelled_output" for e in events)
            await until(lambda: sock._reply.done())
        finally:
            await stream.aclose()


@asynccontextmanager
async def pump(runner, rt):
    runner.rt = rt
    task = asyncio.create_task(runner._pump(rt))
    await asyncio.sleep(0)
    try:
        yield
    finally:
        runner._closed = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for pending in list(runner._finalize_tasks):
            pending.cancel()
        await asyncio.gather(*runner._finalize_tasks, return_exceptions=True)


@pytest.mark.parametrize("opening", [True, False])
@in_a_loop
async def test_silence_withdraws_an_inaudible_reply_through_the_pump(monkeypatch, opening):
    runner, session, page = runner_for("S2A")
    played = []

    async def play(pcm):
        played.append(pcm)
    page.send_bytes = play
    hearing = Hearing(completed(None), blocked=True)
    async with bridge(monkeypatch, [hearing]) as (rt, sock, _, _):
        if opening:
            await runner._await_participant("start")
        else:
            session.append_user("Hello Morgan.")
        async with pump(runner, rt):
            await send_ms(rt, LOUD, 800)
            await rt.commit_turn()
            await asyncio.wait_for(hearing.started.wait(), 2)
            await until(lambda: any(f["type"] == "response.output_audio_transcript.delta"
                                    for f in sock.frames))
            await until(lambda: bool(runner._speaking) if not opening else bool(rt.responding))
            hearing.ready.set()
            await until(lambda: session.store.of("user_turn_suppressed"))
            await until(lambda: any(f["type"] == "response.cancel" for f in sock.sent))
            await until(lambda: sock._reply.done())
            assert not played and runner._turn_audio_bytes == 0
            assert not session.store.of("user_turn")
            if opening:
                assert runner._awaiting_participant
            else:
                assert session.store.of("suppressed_turn_reply_cancelled")


@pytest.mark.parametrize("kept", ["audible", "later_commit"])
@in_a_loop
async def test_silence_keeps_an_audible_or_later_reply(monkeypatch, kept):
    runner, session, _ = runner_for("S2A")
    session.append_user("Hello Morgan.")
    hearing = Hearing(completed(None), blocked=True)
    later = Hearing(completed("I still need your answer."), blocked=True)
    async with bridge(monkeypatch, [hearing, later]) as (rt, sock, tts, _):
        async with pump(runner, rt):
            await send_ms(rt, LOUD, 800)
            await rt.commit_turn()
            await until(lambda: runner._speaking)
            if kept == "audible":
                tts.audio()
                await until(lambda: runner._turn_audio_bytes > 0)
            else:
                # Finish the old reply, then start the actual later commit's
                # reply while the first commit still owes its transcript.
                tts.audio()
                tts.finish()
                await until(lambda: sock._reply.done() and not runner._speaking)
                await send_ms(rt, LOUD, 800)
                await rt.commit_turn()
                await until(lambda: runner._agent_turn_began_at >= rt.last_commit_at)
            cancels = sum(f["type"] == "response.cancel" for f in sock.sent)
            hearing.ready.set()
            await until(lambda: session.store.of("reply_to_suppressed_turn"))
            (event,) = session.store.of("reply_to_suppressed_turn")
            if kept == "later_commit":
                assert event["kept"] == "later_commit"
            else:
                assert event["audio_ms"] > 0 and event["playing"]
            assert sum(f["type"] == "response.cancel" for f in sock.sent) == cancels
            assert not sock._reply.done()
