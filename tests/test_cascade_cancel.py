"""Cascade cancellation policy, pipeline 2026-11-12b, stacked on STT 11-12a.

Use the real adapter and bridge. Reply cancellation is separate from the STT
switch because every default-tail cascade cancel inherits the discard policy.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
from contextlib import asynccontextmanager

import pytest

from server import llm
from server.voice import cascade, realtime as R
from test_cascade_transcribe import (  # shared HTTP/TTS fixtures, no network
    MODEL, Hearing, TTS, bridge, completed, config, until,
)
from test_participant_turn_integrity import LOUD, in_a_loop, runner_for, send_ms


@pytest.fixture(autouse=True)
def cascade_policy(monkeypatch):
    monkeypatch.setenv("CASCADE_CANCELLED_OUTPUT_DISCARD", "1")


def test_cascade_discard_policy_has_its_own_version_and_rollback(monkeypatch):
    assert llm.PIPELINE_VERSION == "2026-11-12b"
    assert llm.provenance(MODEL)["cancelled_output"] == "discard"
    assert R.discards_cancelled_output(MODEL)
    assert not R.discards_cancelled_output("nto.gemini-live-2.5-flash-native-audio")
    monkeypatch.setenv("CASCADE_CANCELLED_OUTPUT_DISCARD", "0")
    assert llm.provenance(MODEL)["cancelled_output"] == "relay"
    assert not R.discards_cancelled_output(MODEL)
    assert R.discards_cancelled_output("gpt-realtime-2.1")
    monkeypatch.setenv("CASCADE_CANCELLED_OUTPUT_DISCARD", "1")
    monkeypatch.setenv("CANCELLED_OUTPUT_DISCARD", "0")
    assert R.discards_cancelled_output(MODEL)
    assert not R.discards_cancelled_output("gpt-realtime-2.1")


@pytest.mark.parametrize("policy", ["discard", "hold", "rollback"])
@in_a_loop
async def test_already_queued_frames_follow_the_cancel_policy(monkeypatch, policy):
    if policy == "rollback":
        monkeypatch.setenv("CASCADE_CANCELLED_OUTPUT_DISCARD", "0")
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
    async with bridge(monkeypatch, [hearing]) as (rt, sock, tts, _):
        if opening:
            await runner._await_participant("start")
        else:
            session.append_user("Hello Morgan.")
        original_record = runner._record_user_turn

        async def record_with_audio_queued(*args, **kwargs):
            # The pump is processing the transcript here, so it cannot read
            # the audio until the withdrawal decision returns. Produce it
            # through TTS and the adapter, before the actual cancel.
            tts.audio()
            await until(lambda: any(f["type"] == "response.output_audio.delta"
                                    for f in sock.frames))
            await original_record(*args, **kwargs)
        runner._record_user_turn = record_with_audio_queued
        async with pump(runner, rt):
            await send_ms(rt, LOUD, 800)
            await rt.commit_turn()
            await asyncio.wait_for(hearing.started.wait(), 2)
            await until(lambda: any(f["type"] == "response.output_audio_transcript.delta"
                                    for f in sock.frames))
            await until(lambda: bool(runner._speaking) if not opening else bool(rt.responding))
            hearing.ready.set()
            await until(lambda: session.store.of("user_turn_suppressed"))
            tts.finish()
            await until(lambda: sock._reply.done())
            await until(lambda: session.store.of("cancelled_output_dropped") or played
                        or session.store.of("first_reply_withheld"))
            assert not played and runner._turn_audio_bytes == 0
            assert any(f["type"] == "response.cancel" for f in sock.sent)
            (dropped,) = session.store.of("cancelled_output_dropped")
            assert dropped["audio_ms"] == 200
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


@asynccontextmanager
async def delayed_tts_bridge(monkeypatch, hearings, close_s):
    """Each reply has its own stream; release a slow close during teardown."""
    release_close = asyncio.Event()
    streams = []

    class Stream(TTS):
        def __init__(self, delay):
            super().__init__()
            self.delay = delay
            self.closing = asyncio.Event()
            self.close_cancelled = False

        async def close(self):
            self.closing.set()
            try:
                if self.delay:
                    try:
                        await asyncio.wait_for(release_close.wait(), self.delay)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise

    async with bridge(monkeypatch, hearings) as (rt, sock, _, requests):
        async def opener():
            stream = Stream(close_s if not streams else 0)
            streams.append(stream)
            return stream
        monkeypatch.setattr(cascade, "open_tts", opener)
        try:
            yield rt, sock, streams, requests
        finally:
            release_close.set()


def assert_replies_in_order(sock):
    created = [(n, f["response"]["id"]) for n, f in enumerate(sock.frames)
               if f["type"] == "response.created"]
    assert len(created) == 2 and created[0][1] != created[1][1]
    old_done = next(n for n, f in enumerate(sock.frames)
                    if f["type"] == "response.done" and f["response"]["id"] == created[0][1])
    assert old_done < created[1][0]
    assert not any(f.get("error", {}).get("code") == "conversation_already_has_active_response"
                   for f in sock.frames)


@pytest.mark.parametrize("close_s", [0.0, 0.15, 10.0])
@in_a_loop
async def test_opening_turn_end_cancel_then_commit_plays_promptly(monkeypatch, close_s):
    monkeypatch.setenv("ELEVENLABS_CONCURRENCY", "1")
    monkeypatch.setattr(R, "REQUEST_UNANSWERED_S", 1.5)
    runner, session, page = runner_for("S2A")
    played = asyncio.Event()

    async def play(pcm):
        played.set()
    page.send_bytes = play
    first = Hearing(completed("Hi Morgan,"), blocked=True)
    second = Hearing(completed("thanks for making time."), blocked=True)
    async with delayed_tts_bridge(monkeypatch, [first, second], close_s) as (rt, sock, streams, _):
        await runner._await_participant("start")
        async with pump(runner, rt):
            await send_ms(rt, LOUD, 800)
            await runner._on_turn_ended()
            await until(lambda: any(f["type"] == "response.output_audio_transcript.delta"
                                    for f in sock.frames))
            assert runner._awaiting_participant and not played.is_set()
            await send_ms(rt, LOUD, 1200)
            async with asyncio.timeout(1.0):
                await runner._on_turn_ended()
                assert not any(f.get("error", {}).get("code") == "conversation_already_has_active_response"
                               for f in sock.frames)
                first.ready.set()
                second.ready.set()
                await until(lambda: len(streams) == 2)
                streams[1].audio()
                await played.wait()
            assert_replies_in_order(sock)
            assert not session.store.of("reply_missing")
            assert not session.store.of("reply_retry")
            assert sum(f["type"] == "input_audio_buffer.commit" for f in sock.sent) == 2
            assert sum(f["type"] == "response.create" for f in sock.sent) == 2
            if close_s == 10:
                assert streams[0].close_cancelled


@in_a_loop
async def test_withdrawal_then_next_commit_survives_slow_tts_close(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_CONCURRENCY", "1")
    monkeypatch.setattr(R, "REQUEST_UNANSWERED_S", 1.5)
    runner, session, page = runner_for("S2A")
    session.append_user("Hello Morgan.")
    played = asyncio.Event()

    async def play(pcm):
        played.set()
    page.send_bytes = play
    first = Hearing(completed(None), blocked=True)
    second = Hearing(completed("I need more time to finish."))
    async with delayed_tts_bridge(monkeypatch, [first, second], 10) as (rt, sock, streams, _):
        async with pump(runner, rt):
            await send_ms(rt, LOUD, 800)
            await rt.commit_turn()
            await until(lambda: runner._speaking)
            first.ready.set()
            await until(lambda: session.store.of("suppressed_turn_reply_cancelled"))
            await streams[0].closing.wait()
            assert not sock._reply.done() and not played.is_set()
            await send_ms(rt, LOUD, 1200)
            async with asyncio.timeout(1.0):
                await runner._on_turn_ended()
                assert not any(f.get("error", {}).get("code") == "conversation_already_has_active_response"
                               for f in sock.frames)
                await until(lambda: len(streams) == 2)
                streams[1].audio()
                await played.wait()
            assert_replies_in_order(sock)
            assert streams[0].close_cancelled
            assert not session.store.of("reply_missing") and not session.store.of("reply_retry")


@in_a_loop
async def test_a_create_while_active_and_not_cancelling_is_still_refused(monkeypatch):
    async with bridge(monkeypatch, []) as (rt, sock, _, _):
        await rt.request_response()
        await until(lambda: any(f["type"] == "response.output_audio_transcript.delta"
                                for f in sock.frames))
        await sock.send('{"type":"response.create"}')
        assert any(f.get("error", {}).get("code") == "conversation_already_has_active_response"
                   for f in sock.frames)
        assert sum(f["type"] == "response.created" for f in sock.frames) == 1


@in_a_loop
async def test_a_cancel_before_audio_removes_unheard_sentences_from_context(monkeypatch):
    async with bridge(monkeypatch, []) as (rt, sock, _, requests):
        await rt.request_response()
        await until(lambda: any(f["type"] == "response.output_audio_transcript.delta"
                                for f in sock.frames))
        assert any(i["role"] == "assistant" for i in sock.items)
        assert not any(f["type"] == "response.output_audio.delta" for f in sock.frames)
        await rt.cancel_response()
        await until(lambda: sock._reply.done())
        assert not any(i["role"] == "assistant" for i in sock.items)
        await rt.request_response()
        await until(lambda: len(requests) == 2)
        assert not any(m["role"] == "assistant" for m in requests[1]["messages"])


@in_a_loop
async def test_cancel_before_the_reply_task_starts_keeps_frame_order(monkeypatch):
    async with bridge(monkeypatch, []) as (_, sock, _, _):
        await sock.send('{"type":"response.create"}')
        await sock.send('{"type":"response.cancel"}')
        await sock.send('{"type":"response.create"}')
        await until(lambda: sum(f["type"] == "response.created" for f in sock.frames) == 2)
        assert_replies_in_order(sock)


@in_a_loop
async def test_cancel_join_timeout_still_releases_the_slot_and_orders_frames(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_CONCURRENCY", "1")
    monkeypatch.setattr(cascade, "TTS_CLOSE_TIMEOUT_S", 10.0)
    monkeypatch.setattr(cascade, "REPLY_CANCEL_TIMEOUT_S", 0.05)
    async with delayed_tts_bridge(monkeypatch, [], 10) as (rt, sock, streams, _):
        await rt.request_response()
        await until(lambda: any(f["type"] == "response.output_audio_transcript.delta"
                                for f in sock.frames))
        await rt.cancel_response()
        await streams[0].closing.wait()
        async with asyncio.timeout(0.5):
            await rt.request_response()
            await until(lambda: len(streams) == 2)
        assert_replies_in_order(sock)
        assert streams[0].close_cancelled
        cancelled = next(f for f in sock.frames if f["type"] == "response.done")
        assert cancelled["response"]["status"] == "cancelled"


@in_a_loop
async def test_runner_withdrawal_during_create_join_cancels_the_new_reply(monkeypatch):
    """A is withdrawn, B is also no speech while B's create joins A, then C
    is real speech. B must be cancelled so C can receive a prompt reply."""
    monkeypatch.setenv("ELEVENLABS_CONCURRENCY", "1")
    monkeypatch.setattr(R, "REQUEST_UNANSWERED_S", 1.5)
    runner, session, page = runner_for("S2A")
    session.append_user("Hello Morgan.")
    played = asyncio.Event()

    async def play(pcm):
        played.set()
    page.send_bytes = play
    first = Hearing(completed(None), blocked=True)
    second = Hearing(completed(None), blocked=True)
    third = Hearing(completed("I need more time to finish."))
    async with delayed_tts_bridge(monkeypatch, [first, second, third], 0.45) as (rt, sock, streams, _):
        async with pump(runner, rt):
            await send_ms(rt, LOUD, 800)
            await rt.commit_turn()
            await until(lambda: runner._speaking)
            first.ready.set()
            await until(lambda: session.store.of("suppressed_turn_reply_cancelled"))
            await streams[0].closing.wait()

            await send_ms(rt, LOUD, 1200)
            turn_b = asyncio.create_task(runner._on_turn_ended())
            try:
                await asyncio.wait_for(second.started.wait(), 1)
                assert not turn_b.done() and sock._create_lock.locked()
                second.ready.set()
                await until(lambda: len(session.store.of("user_turn_suppressed")) == 2)
                await asyncio.wait_for(turn_b, 1)
                await until(lambda: len(session.store.of("suppressed_turn_reply_cancelled")) == 2)
                reply_b = sock._reply
                assert reply_b.cancelling() or reply_b.done(), "B's withdrawal cancel was lost"
                await until(reply_b.done)
                await until(lambda: len(session.store.of("cancelled_output_dropped")) == 2)

                stream_count = len(streams)
                await send_ms(rt, LOUD, 1200)
                async with asyncio.timeout(1.0):
                    await runner._on_turn_ended()
                    await until(lambda: len(streams) > stream_count)
                    streams[-1].audio()
                    await played.wait()
                created = [(n, f["response"]["id"]) for n, f in enumerate(sock.frames)
                           if f["type"] == "response.created"]
                assert len(created) == 3 and len({rid for _, rid in created}) == 3
                for (_, rid), (next_at, _) in zip(created, created[1:]):
                    done_at = next(n for n, f in enumerate(sock.frames)
                                   if f["type"] == "response.done" and f["response"]["id"] == rid)
                    assert done_at < next_at
                assert not any(f.get("error", {}).get("code") == "conversation_already_has_active_response"
                               for f in sock.frames)
                assert not session.store.of("reply_missing") and not session.store.of("reply_retry")
            finally:
                if not turn_b.done():
                    turn_b.cancel()
                await asyncio.gather(turn_b, return_exceptions=True)


@asynccontextmanager
async def stalled_websocket(close_timeout):
    """A real websocket over AF_UNIX socketpair. The peer upgrades, reads
    frames, and never answers a close. No listener, DNS, or TCP connection."""
    client_socket, peer_socket = socket.socketpair()
    client_socket.setblocking(False)
    peer_socket.setblocking(False)
    loop = asyncio.get_running_loop()
    peer_closed = asyncio.Event()

    async def peer():
        try:
            header = b""
            while b"\r\n\r\n" not in header:
                chunk = await loop.sock_recv(peer_socket, 4096)
                if not chunk:
                    return
                header += chunk
            key = next(line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")
                       if line.lower().startswith(b"sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1(
                key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            await loop.sock_sendall(peer_socket, b"HTTP/1.1 101 Switching Protocols\r\n"
                                   b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                                   b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            while await loop.sock_recv(peer_socket, 4096):
                pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            peer_closed.set()

    peer_task = asyncio.create_task(peer())
    ws = None
    try:
        ws = await cascade.websockets.connect("ws://localhost/", sock=client_socket,
                                             proxy=None, ping_interval=None,
                                             close_timeout=close_timeout)
        yield ws, peer_closed
    finally:
        if ws is not None:
            ws.transport.abort()
            await asyncio.wait_for(ws.wait_closed(), 1)
        peer_task.cancel()
        await asyncio.gather(peer_task, return_exceptions=True)
        client_socket.close()
        peer_socket.close()


@pytest.mark.parametrize("cancel_cleanup", [False, True])
@in_a_loop
async def test_stalled_tts_close_aborts_connection_when_slot_is_released(monkeypatch, cancel_cleanup):
    close_bound = 10 if cancel_cleanup else 0.05
    monkeypatch.setattr(cascade, "TTS_CLOSE_TIMEOUT_S", close_bound)
    async with stalled_websocket(close_bound) as (ws, peer_closed):
        slot = asyncio.Semaphore(1)
        await slot.acquire()
        reader = asyncio.create_task(asyncio.sleep(60))

        async def opened():
            return ws, reader, slot
        tts = asyncio.create_task(opened())
        await tts
        cleanup = asyncio.create_task(cascade.CascadeSocket._close_reply_tts(tts))
        try:
            if cancel_cleanup:
                await until(lambda: ws.protocol.close_sent is not None)
                cleanup.cancel()  # The outer reply-join bound can do this.
                with pytest.raises(asyncio.CancelledError):
                    await cleanup
            else:
                await asyncio.wait_for(cleanup, 0.5)
            await asyncio.wait_for(slot.acquire(), 0.1)
            slot.release()
            assert ws.transport.is_closing(), "the released slot still has an open connection"
            await asyncio.wait_for(peer_closed.wait(), 0.1)
        finally:
            cleanup.cancel()
            reader.cancel()
            await asyncio.gather(cleanup, reader, return_exceptions=True)
