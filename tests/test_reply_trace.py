"""Reply trace uses the real 1:1 runner, bridge and adapter; no network."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from server.reply_trace import ReplyTrace, bind, observe
from server.voice import cascade, realtime as R
from test_cascade_cancel import pump
from test_cascade_transcribe import MODEL, Hearing, TTS, Wire, completed, config, until
from test_participant_turn_integrity import LOUD, in_a_loop, runner_for, send_ms


class Chunks(httpx.AsyncByteStream):
    def __init__(self, pieces):
        self.pieces = pieces

    async def __aiter__(self):
        for piece in self.pieces:
            if isinstance(piece, asyncio.Event):
                await piece.wait()
            else:
                yield ('data: ' + json.dumps({"choices": [{"delta": {"content": piece}}]}) + '\n\n').encode()
        yield b'data: [DONE]\n\n'


class Speech(TTS):
    def __init__(self, finish=True):
        super().__init__()
        self.sent = []
        self.auto_finish = finish

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("inputs"):
            self.audio()
        if msg.get("close_socket") and self.auto_finish:
            self.finish()


@asynccontextmanager
async def pipeline(monkeypatch, pieces=("Okay, go on. ",), hearings=(), connect=None,
                   finish=True, traced=True):
    runner, session, page = runner_for("S2A")
    requests, streams = [], []
    pending = list(hearings)

    async def handle(req):
        body = json.loads(req.content)
        requests.append(body)
        if body.get("stream"):
            return httpx.Response(200, stream=Chunks(pieces))
        hearing = pending.pop(0)
        hearing.started.set()
        await hearing.ready.wait()
        return httpx.Response(hearing.status, json=hearing.body)

    async def open_tts():
        if connect is not None:
            await connect.wait()
        speech = Speech(finish)
        streams.append(speech)
        return speech

    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")
    monkeypatch.setattr(cascade, "open_tts", open_tts)
    if not traced:
        monkeypatch.setattr("server.realtime_voice_session.bind_reply_trace", lambda *a: None, raising=False)
    audio = []

    async def send_bytes(data):
        audio.append(data)

    page.send_bytes = send_bytes
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sock = Wire(api_key="k", http=client)
        rt = R.RealtimeVoiceSession("You are Morgan.", model=MODEL, api_key="k")
        rt.ws = sock
        async with pump(runner, rt):
            try:
                yield runner, rt, sock, session.store, page, streams, requests, audio
            finally:
                tasks = list(sock._tasks)
                await rt.close()
                await asyncio.gather(*tasks, return_exceptions=True)
                observe(runner, "flush")


def record(runner):
    return next(iter(runner._reply_trace.replies.values()))


async def ask(rt):
    await send_ms(rt, LOUD, 800)
    await rt.commit_turn()


def edges(rec):
    s = rec["stages"]
    for a, b in (("create_entered", "create_released"),
                 ("create_released", "request_started"),
                 ("request_started", "first_content"),
                 ("first_content", "first_sentence_ready"),
                 ("tts_slot_wait_start", "tts_slot_acquired"),
                 ("tts_slot_acquired", "tts_connected"),
                 ("first_sentence_ready", "first_text_sent"),
                 ("tts_connected", "first_text_sent"),
                 ("first_text_sent", "tts_first_audio"),
                 ("tts_first_audio", "first_output_emitted")):
        assert s[a] <= s[b], (a, b, s)


@pytest.mark.parametrize("slow", ["model", "tts"])
@pytest.mark.parametrize("pieces,trigger", [
    (("Okay", ", go", " on", ".", " "), "punctuation"),
    (("A long reply without punctuation",), "final_flush"),
    (("Complete sentence.",), "final_flush"),
])
@in_a_loop
async def test_causal_edges_with_both_overlap_orders(monkeypatch, slow, pieces, trigger):
    gate = asyncio.Event()
    contents = (gate, *pieces) if slow == "model" else pieces
    async with pipeline(monkeypatch, contents, [Hearing(completed("Hello there."))],
                        connect=gate if slow == "tts" else None) as data:
        runner, rt, sock, store, page, _, _, audio = data
        await ask(rt)
        if slow == "model":
            await until(lambda: "tts_connected" in record(runner)["stages"])
            assert "first_content" not in record(runner)["stages"]
        else:
            await until(lambda: "first_sentence_ready" in record(runner)["stages"])
            assert "tts_connected" not in record(runner)["stages"]
        gate.set()
        await until(lambda: sock._reply.done() and audio)
        rec = record(runner)
        edges(rec)
        assert rec["sentence_trigger"] == trigger
        assert rec["page_turn"] == page.frames("assistant_started")[0]["turn"]
        commit = runner._reply_trace.commits[rec["commit_item_ids"][0]]
        assert commit["decision"] == {"outcome": "accepted", "reason": None}
        assert commit["stages"]["stt_started"] <= commit["stages"]["stt_done"]
        assert not store.of("reply_trace")  # no store write on any boundary
    assert len(store.of("reply_trace")) == len(store.of("commit_trace")) == 1
    assert store.of("reply_trace")[0]["clock"] == "encounter_s"


@in_a_loop
async def test_hold_release_and_play_end_do_not_own_trace_lifetime(monkeypatch):
    hearing = Hearing(completed("Hello there."), blocked=True)
    async with pipeline(monkeypatch, hearings=[hearing], finish=False) as data:
        runner, rt, sock, store, page, streams, _, audio = data
        runner._awaiting_participant = True
        await ask(rt)
        await until(lambda: "first_output_hold_started" in record(runner)["stages"])
        assert not audio
        hearing.ready.set()
        await until(lambda: bool(audio))
        rec = record(runner)
        assert rec["stages"]["first_output_hold_started"] <= rec["stages"]["first_output_hold_released"]
        commit = runner._reply_trace.commits[rec["release_commit_item_id"]]
        assert commit["stages"]["transcript_decided"] <= rec["stages"]["first_output_hold_released"]
        # The page writes/removes its legacy row before cascade teardown.
        runner._timing.ack({"phase": "end", "turn": rec["page_turn"], "interrupted": True,
                            "receipt_perf_ms": 12.0, "scheduled_audio_s": 2.0})
        assert len(store.of("turn_timing")) == 1
        assert "reply_done" not in rec["stages"]
        streams[0].finish()
        await until(lambda: sock._reply.done())
        assert rec["status"] == "done"
    assert len(store.of("turn_timing")) == len(store.of("reply_trace")) == 1
    assert "reply_done" in store.of("reply_trace")[0]["stages"]


@pytest.mark.parametrize("failed", [False, True])
@in_a_loop
async def test_reversed_and_failed_stt_keep_their_commit_identity(monkeypatch, failed):
    first = Hearing({"choices": []} if failed else completed("First spoken line."), blocked=True)
    second = Hearing(completed("Second spoken line."), blocked=True)
    async with pipeline(monkeypatch, hearings=[first, second]) as data:
        runner, rt, _, store, _, _, _, _ = data
        await send_ms(rt, LOUD, 800)
        await rt.commit_input()
        await first.started.wait()
        a = runner._reply_trace.latest_commit
        await send_ms(rt, LOUD, 800)
        await rt.commit_input()
        await second.started.wait()
        b = runner._reply_trace.latest_commit
        second.ready.set()
        await until(lambda: runner._reply_trace.commits[b]["decision"] is not None)
        assert runner._reply_trace.commits[a]["decision"] is None
        first.ready.set()
        await until(lambda: runner._reply_trace.commits[a]["decision"] is not None)
        ca, cb = (runner._reply_trace.commits[i] for i in (a, b))
        assert ca["stages"]["stt_done"] > cb["stages"]["stt_done"]
        assert ca["decision"]["outcome"] == ("failed" if failed else "accepted")
        assert cb["decision"]["outcome"] == "accepted"
    assert {e["commit_item_id"] for e in store.of("commit_trace")} == {a, b}


@pytest.mark.parametrize("wait", ["slot", "connect"])
@in_a_loop
async def test_cancelled_opening_retains_partial_stamps(monkeypatch, wait):
    gate = asyncio.Event()
    slot = asyncio.Semaphore(0 if wait == "slot" else 1)
    monkeypatch.setattr(cascade, "_tts_slot", lambda: slot)
    async with pipeline(monkeypatch, connect=gate) as data:
        runner, rt, sock, store, _, _, _, _ = data
        await rt.request_response()
        await until(lambda: "tts_slot_wait_start" in record(runner)["stages"])
        if wait == "connect":
            await until(lambda: "tts_slot_acquired" in record(runner)["stages"])
        await rt.cancel_response()
        await until(lambda: sock._reply.done())
        rec = record(runner)
        assert rec["status"] == "cancelled" and rec["page_turn"] is None
        assert "tts_connected" not in rec["stages"]
        assert ("tts_slot_acquired" in rec["stages"]) == (wait == "connect")
        assert "first_audio_ms" not in sock.timings
        assert not any(it.get("role") == "assistant" for it in sock.items)
    assert len(store.of("reply_trace")) == 1


@in_a_loop
async def test_create_waiting_in_d4_join_keeps_both_reply_identities(monkeypatch):
    close_started, close_release = asyncio.Event(), asyncio.Event()
    async with pipeline(monkeypatch, finish=False) as data:
        runner, rt, sock, store, _, streams, _, _ = data
        await rt.request_response()
        await until(lambda: bool(streams and streams[0].sent))
        old = sock._reply_id

        async def slow_close():
            close_started.set()
            await close_release.wait()

        streams[0].close = slow_close
        await rt.cancel_response()
        await close_started.wait()
        create = asyncio.create_task(rt.request_response())
        await until(lambda: len(runner._reply_trace.replies) == 2)
        new = next(rid for rid in runner._reply_trace.replies if rid != old)
        assert "create_released" not in runner._reply_trace.replies[new]["stages"]
        close_release.set()
        await create
        await until(lambda: len(streams) == 2)
        streams[1].finish()
        await until(lambda: sock._reply.done())
        a, b = (runner._reply_trace.replies[r] for r in (old, new))
        assert b["stages"]["create_entered"] <= a["stages"]["reply_done"] <= b["stages"]["create_released"]
    assert len(store.of("reply_trace")) == 2


def normalized(value):
    identities = {}

    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and v.startswith(("item_", "resp_", "call_")):
            return identities.setdefault(v, f"identity_{len(identities)}")
        return v
    return walk(value)


@pytest.mark.parametrize("broken", [False, True])
@in_a_loop
async def test_frames_requests_and_behavior_timings_unchanged(monkeypatch, broken):
    snapshots = []
    for traced in (True, False):
        with monkeypatch.context() as patch:
            # Freeze the existing wall clock so legacy durations and page
            # relative dates are comparable; only random IDs are normalized.
            patch.setattr(time, "time", lambda: 1800000000.0)
            if broken and traced:
                def fail(*a, **kw):
                    raise RuntimeError("trace boundary failed")
                for method in ("stamp", "create", "commit", "decision", "stt", "page", "done", "socket_closed", "flush"):
                    patch.setattr(ReplyTrace, method, fail)
            async with pipeline(patch, hearings=[Hearing(completed("Hello there."))], traced=traced) as data:
                runner, rt, sock, _, page, streams, requests, audio = data
                await ask(rt)
                await until(lambda: sock._reply.done() and bool(audio))
                await until(lambda: bool(page.frames("assistant_done")))
                # Legacy values retain their names, units and first-audio gate.
                assert set(sock.timings) == {"stt_ms", "llm_first_token_ms", "llm_total_ms",
                                              "tts_slot_wait_ms", "tts_connect_ms", "first_audio_ms"}
                snapshots.append((normalized([sock.frames, requests, streams[0].sent, page.json]), audio))
    def compare(a, b, path="snapshot"):
        assert type(a) is type(b), path
        if isinstance(a, dict):
            assert a.keys() == b.keys(), path
            for key in a:
                compare(a[key], b[key], path + "." + key)
        elif isinstance(a, (list, tuple)):
            assert len(a) == len(b), path
            for i, (x, y) in enumerate(zip(a, b)):
                compare(x, y, path + f"[{i}]")
        else:
            assert a == b, (path, a, b)
    compare(*snapshots)


def test_vad_sample_estimate_and_monotonic_clock(monkeypatch):
    runner, session, _ = runner_for("S2A")
    trace = ReplyTrace(session.store)
    vad = R.SilenceDetector(noise_margin=0)
    for _ in range(40):
        vad.feed(LOUD)
    for _ in range(45):
        mark = vad.feed(b'\0\0' * 320)
    assert mark == "turn_ended"
    trace.vad(vad._trace_end)
    assert trace.participant["vad_sample_position"]["trailing_silence_samples"] == 14400
    assert trace.participant["vad_sample_position"]["last_voiced_sample"] == 12800
    assert trace.participant["vad_detected"] - trace.participant["last_voiced_estimate"] == pytest.approx(.9)
    before = trace.now()
    monkeypatch.setattr(time, "time", lambda: 0)
    assert trace.now() >= before
    assert vad._silence_ms_run == 0  # captured before the detector reset


def test_finalize_is_once_and_store_failure_is_isolated():
    runner, session, _ = runner_for("S2A")
    trace = runner._reply_trace = ReplyTrace(session.store)
    trace.create("r")
    trace.commit("a")
    trace.commit("b")
    trace.socket_closed()
    calls = []

    def event(kind, **fields):
        calls.append((kind, fields))
        if kind == "reply_trace":
            raise RuntimeError("disk error")

    session.store.event = event
    observe(runner, "flush")
    observe(runner, "flush")
    assert len(calls) == 3
    assert calls[0][1]["status"] == "socket_closed"


def test_trace_is_not_attached_to_rooms_or_other_adapters():
    runner, _, _ = runner_for("S2A")
    rt = R.RealtimeVoiceSession("x", model=MODEL, api_key="k")
    rt.ws = cascade.CascadeSocket(api_key="k")
    runner.room = object()
    bind(runner, rt)
    assert not getattr(runner, "_reply_trace", None)
    runner.room = None
    rt.ws = object()
    bind(runner, rt)
    assert not getattr(runner, "_reply_trace", None)


@in_a_loop
async def test_vad_is_stamped_before_forwarding_and_briefing_precedes_commit(monkeypatch):
    async with pipeline(monkeypatch, hearings=[Hearing(completed("Hello there."))]) as data:
        runner, rt, sock, _, page, _, _, _ = data
        forwarded, resume = asyncio.Event(), asyncio.Event()
        original = rt.send_audio

        async def send_audio(pcm):
            if runner.vad._trace_end and not resume.is_set():
                forwarded.set()
                await resume.wait()
            await original(pcm)

        async def brief(*, probing):
            assert not runner._reply_trace.commits
            assert "briefing_entered" in runner._reply_trace.participant

        monkeypatch.setattr(rt, "send_audio", send_audio)
        monkeypatch.setattr(runner, "_brief_next_beat", brief)
        page._frames = [LOUD] * 40 + [b'\0\0' * 320] * 100
        task = asyncio.create_task(runner._client_to_model())
        try:
            await asyncio.wait_for(forwarded.wait(), 2)
            assert runner._reply_trace.participant["vad_detected"] is not None
            assert not runner._reply_trace.commits
            resume.set()
            await asyncio.wait_for(task, 2)
            await until(lambda: sock._reply is not None and sock._reply.done())
            commit = next(iter(runner._reply_trace.commits.values()))
            pt = commit["participant"]
            assert pt["vad_detected"] <= pt["briefing_entered"] <= pt["briefing_completed"] <= commit["stages"]["commit_dispatched"]
            assert commit["participant_turn_id"] == record(runner)["participant_turn_id"] == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@in_a_loop
async def test_trace_attaches_before_connect_and_first_create(monkeypatch):
    runner, _, _ = runner_for("S2A")
    monkeypatch.setattr(R, "gateway_api_key", lambda: "k")
    rt = runner._new_session(instructions="x", voice="")
    assert rt.ws is None and rt._reply_trace is runner._reply_trace
    # connect uses the real CascadeSocket; no HTTP is issued by handshake.
    await rt.connect(open_conversation=False)
    try:
        assert rt.ws._reply_trace is runner._reply_trace
        await rt.request_response()
        rid = rt.ws._reply_id
        # Cancel before the reply's first task step: still a trace identity.
        await rt.cancel_response()
        await until(lambda: rt.ws._reply.done())
        assert runner._reply_trace.replies[rid]["status"] == "cancelled"
    finally:
        await rt.close()
        observe(runner, "flush")


@in_a_loop
async def test_socket_close_keeps_unfinished_stt_partial(monkeypatch):
    hearing = Hearing(completed("Hello there."), blocked=True)
    async with pipeline(monkeypatch, hearings=[hearing]) as data:
        runner, rt, _, store, _, _, _, _ = data
        await send_ms(rt, LOUD, 800)
        await rt.commit_input()
        await hearing.started.wait()
        await rt.close()
        observe(runner, "flush")  # production teardown does not await STT
        (rec,) = store.of("commit_trace")
        assert rec["stt_outcome"] == "socket_closed"
        assert "stt_started" in rec["stages"] and "stt_done" not in rec["stages"]
        assert rec["decision"] is None
    assert "stt_done" not in store.of("commit_trace")[0]["stages"]


@pytest.mark.parametrize("outcome,reason", [
    ("suppressed", "no_speech"), ("duplicate", "exact"),
    ("echo", "character_playback"), ("missing", "transcript_empty"),
])
@in_a_loop
async def test_transcript_decision_exits_are_commit_linked(monkeypatch, outcome, reason):
    text = "Could you say a little more about that?"
    hearing = Hearing(completed(None if outcome == "suppressed" else text),
                      blocked=outcome == "missing")
    hearings = [Hearing(completed(text)), hearing] if outcome == "duplicate" else [hearing]
    async with pipeline(monkeypatch, hearings=hearings) as data:
        runner, rt, sock, _, _, _, _, _ = data
        if outcome == "echo":
            runner._recent_agent_texts.append((time.time(), runner.agent_id, text))
        if outcome == "duplicate":
            await send_ms(rt, LOUD, 2000)
            await rt.commit_input()
            await until(lambda: bool(runner.session.store.of("user_turn")))
        await send_ms(rt, LOUD, 2000)
        await rt.commit_input()
        iid = runner._reply_trace.latest_commit
        if outcome == "missing":
            # The bridge's empty-transcript exit, using the real commit id.
            # Current cascade hearings map empty/null to [no speech] instead.
            await hearing.started.wait()
            sock._emit({"type": "conversation.item.input_audio_transcription.completed",
                        "item_id": iid, "transcript": ""})
        await until(lambda: runner._reply_trace.commits[iid]["decision"] is not None)
        assert runner._reply_trace.commits[iid]["decision"] == {"outcome": outcome, "reason": reason}


@in_a_loop
async def test_first_content_is_not_overwritten_until_sentence_or_final_flush(monkeypatch):
    gate = asyncio.Event()
    async with pipeline(monkeypatch, ("Okay", gate, ", go on. ", "The rest")) as data:
        runner, rt, sock, _, _, _, _, _ = data
        await rt.request_response()
        await until(lambda: "first_content" in record(runner)["stages"])
        first = record(runner)["stages"]["first_content"]
        assert "first_sentence_ready" not in record(runner)["stages"]
        gate.set()
        await until(lambda: sock._reply.done())
        assert record(runner)["stages"]["first_content"] == first
        assert record(runner)["sentence_trigger"] == "punctuation"
        assert record(runner)["stages"]["first_sentence_ready"] > first


@in_a_loop
async def test_new_socket_opener_does_not_reference_previous_socket_commit(monkeypatch):
    async with pipeline(monkeypatch, hearings=[Hearing(completed("Hello there."))]) as data:
        runner, rt, sock, _, _, _, _, _ = data
        await send_ms(rt, LOUD, 800)
        await rt.commit_input()
        old_commit = runner._reply_trace.latest_commit
        await until(lambda: runner._reply_trace.commits[old_commit]["decision"] is not None)
        next_rt = R.RealtimeVoiceSession("x", model=MODEL, api_key="k")
        next_rt.ws = cascade.CascadeSocket(api_key="k", http=sock._http)
        bind(runner, next_rt)
        try:
            await next_rt.request_response()
            rid = next_rt.ws._reply_id
            await next_rt.cancel_response()
            await until(lambda: next_rt.ws._reply.done())
            rec = runner._reply_trace.replies[rid]
            assert rec["commit_item_ids"] == [] and rec["participant_turn_id"] is None
        finally:
            await next_rt.close()
