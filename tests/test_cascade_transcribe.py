"""Cascade STT, pipeline 2026-11-12a. All HTTP and TTS stay in memory.

The gateway's silence response is a completed choice with content:null. A
malformed 200 is a failed hearing. Feed both through the adapter, the real
bridge and the runner so the annotation gate and room
arrival bookkeeping are tested together.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from server import encounter_record, llm, realtime_voice_session as rvs
from server.voice import cascade, realtime as R
from test_cascade_voice import _sse
from test_participant_turn_integrity import (
    LOUD, DeadMember, _room, in_a_loop, runner_for, send_ms,
)

MODEL = "cascade-argon-elevenlabs"
TRANSCRIBE = "nto.gemini-3.5-transcribe"
ROLLBACK = "nto.gemini-3.5-flash-lite"


def completed(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


class Wire(cascade.CascadeSocket):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.frames = []
        self.sent = []

    def _emit(self, ev):
        self.frames.append(ev)
        super()._emit(ev)

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        await super().send(raw)


class Hearing:
    def __init__(self, body, *, status=200, blocked=False):
        self.body, self.status = body, status
        self.ready = asyncio.Event()
        self.started = asyncio.Event()
        if not blocked:
            self.ready.set()


class TTS:
    """Keep a reply in flight until the test supplies audio or its end."""
    def __init__(self):
        self.out = asyncio.Queue()

    async def send(self, raw):
        pass

    def audio(self):
        self.out.put_nowait(json.dumps({"audio": base64.b64encode(
            b"\x01\x00" * 4800).decode()}))

    def finish(self):
        self.out.put_nowait(json.dumps({"is_final": True}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.out.get()

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(llm, "_FILE", {})
    monkeypatch.setattr(R, "MODEL", MODEL)
    monkeypatch.setattr(R, "RECV_POLL_S", 0.01)
    monkeypatch.delenv("CASCADE_STT_MODEL", raising=False)
    monkeypatch.setenv("PARTICIPANT_DROP_ANNOTATIONS", "1")
    monkeypatch.setenv("CANCELLED_OUTPUT_DISCARD", "1")
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.02")
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.3")


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 2)


@asynccontextmanager
async def bridge(monkeypatch, hearings):
    requests, tts = [], TTS()

    async def handle(req):
        body = json.loads(req.content)
        requests.append(body)
        if body.get("stream"):
            return _sse("Okay, go on. ")
        hearing = hearings.pop(0)
        hearing.started.set()
        await hearing.ready.wait()
        if isinstance(hearing.body, Exception):
            raise hearing.body
        return httpx.Response(hearing.status, json=hearing.body)

    async def open_tts():
        return tts

    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")
    monkeypatch.setattr(cascade, "open_tts", open_tts)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sock = Wire(api_key="k", http=client)
        rt = R.RealtimeVoiceSession("You are Morgan.", model=MODEL, api_key="k")
        rt.ws = sock
        rt.voiced_bar = lambda: 500
        try:
            yield rt, sock, tts, requests
        finally:
            tasks = list(sock._tasks)
            await rt.close()
            await asyncio.gather(*tasks, return_exceptions=True)


async def transcript(rt, *, ms=800, probe=False):
    await send_ms(rt, LOUD, ms)
    await rt.commit_input(probe=probe)
    stream = rt.events()
    try:
        while True:
            ev = await asyncio.wait_for(anext(stream), 2)
            if ev["type"] in ("user_transcript", "transcript_missing"):
                return ev
    finally:
        await stream.aclose()


async def record(runner, rt, **kw):
    ev = await transcript(rt, **kw)
    assert ev["type"] == "user_transcript"
    await runner._record_user_turn(ev["text"], **rvs._turn_meta(ev))
    return ev


@pytest.mark.parametrize("content", [None, "", " \n\t ", "[no speech]"])
@pytest.mark.parametrize("voiced_ms", [40, 800, None])
@in_a_loop
async def test_valid_silence_reaches_the_runner_as_an_annotation(monkeypatch, content, voiced_ms):
    runner, session, page = runner_for("S2A")
    async with bridge(monkeypatch, [Hearing(completed(content))]) as (rt, sock, _, requests):
        runner.rt = rt
        if voiced_ms is None:
            rt.voiced_bar = None
        ev = await record(runner, rt, ms=voiced_ms or 800)
        assert ev["text"] == "[no speech]" and ev["voiced_ms"] == voiced_ms
        (suppressed,) = session.store.of("user_turn_suppressed")
        assert suppressed["reason"] == "no_speech" and suppressed["annotation_only"]
        assert not session.store.of("user_turn") and not session.shared_history
        assert not page.frames("user_transcript")
        assert sock.items[0]["text"] == "[no speech]" and "audio" not in sock.items[0]
        assert sock._messages()[-1] == {"role": "user", "content": "[no speech]"}
        assert requests[0]["model"] == TRANSCRIBE
        assert "[no speech]" in requests[0]["messages"][0]["content"][0]["text"]


INVALID = [
    None, [], {}, {"choices": []}, {"choices": {}}, {"choices": [None]},
    {"choices": [{"finish_reason": "stop"}]},
    {"choices": [{"finish_reason": "stop", "message": None}]},
    {"choices": [{"finish_reason": "stop", "message": {}}]},
    {"choices": [{"message": {"content": None}}]},
    *[{"choices": [{"finish_reason": reason, "message": {"content": ""}}]}
      for reason in ("length", "content_filter", "tool_calls", None)],
    *[completed(content) for content in (3, False, [], {})],
    *[{"choices": [{"finish_reason": "stop", "message": {
        "content": None, "refusal": refusal}}]} for refusal in ("refused", None)],
    {**completed(None), "refusal": "refused"},
    {"choices": [{**completed(None)["choices"][0], "refusal": "refused"}]},
]


@pytest.mark.parametrize("body", INVALID)
@in_a_loop
async def test_malformed_200_is_stt_failed_never_silence(monkeypatch, body):
    async with bridge(monkeypatch, [Hearing(body)]) as (rt, sock, _, _):
        ev = await transcript(rt)
        assert ev["type"] == "transcript_missing" and ev["why"] == "transcript_failed"
        (failed,) = [f for f in sock.frames if f["type"].endswith("transcription.failed")]
        assert failed["error"]["code"] == "stt_failed"
        assert not any(f["type"].endswith("transcription.completed") for f in sock.frames)
        assert not any(i["text"] == "[no speech]" for i in sock.items)


@pytest.mark.parametrize("status", [199, 302, 400, 429, 500, 503])
@in_a_loop
async def test_non_2xx_cannot_turn_into_silence(monkeypatch, status):
    async with bridge(monkeypatch, [Hearing(completed(None), status=status)]) as (rt, sock, _, _):
        assert (await transcript(rt))["why"] == "transcript_failed"
        assert any(f.get("error", {}).get("code") == "stt_failed" for f in sock.frames)


@in_a_loop
async def test_a_timeout_is_stt_failed(monkeypatch):
    async with bridge(monkeypatch, [Hearing(httpx.ReadTimeout("synthetic timeout"))]) as (rt, sock, _, _):
        assert (await transcript(rt))["why"] == "transcript_failed"
        assert any(f.get("error", {}).get("code") == "stt_failed" for f in sock.frames)


@pytest.mark.parametrize("status", [200, 201, 202, 299])
@in_a_loop
async def test_any_valid_2xx_hearing_can_report_silence(monkeypatch, status):
    async with bridge(monkeypatch, [Hearing(completed(None), status=status)]) as (rt, _, _, _):
        ev = await transcript(rt)
        assert ev["type"] == "user_transcript" and ev["text"] == "[no speech]"


@pytest.mark.parametrize("kind", ["probe", "replay_duplicate", "replay_only"])
@in_a_loop
async def test_probe_and_replay_precede_silence_gating(monkeypatch, kind):
    runner, session, _ = runner_for("S2A")
    async with bridge(monkeypatch, [Hearing(completed(None))]) as (rt, _, _, _):
        runner.rt = rt
        if kind.startswith("replay"):
            runner._replay_marker = runner._transcripts_arrived
            if kind == "replay_duplicate":
                runner._transcripts_arrived += 1
            await rt.replay_input(LOUD * 40)
            stream = rt.events()
            try:
                while (ev := await asyncio.wait_for(anext(stream), 2))["type"] != "user_transcript":
                    pass
            finally:
                await stream.aclose()
            assert ev["replay"]
            await runner._record_user_turn(ev["text"], **rvs._turn_meta(ev))
        else:
            ev = await record(runner, rt, probe=True)
            assert ev["probe"]
        (event,) = session.store.of("user_turn_suppressed")
        assert event["reason"] == {"probe": "probe_pad", "replay_duplicate": "replay_duplicate",
                                   "replay_only": "no_speech"}[kind]
        assert not session.store.of("user_turn")
        if kind.startswith("replay"):
            assert runner._replay_marker is None


@in_a_loop
async def test_placeholder_only_text_stays_untranscribed(monkeypatch):
    runner, session, _ = runner_for("S2A")
    async with bridge(monkeypatch, [Hearing(completed("{}"))]) as (rt, sock, _, _):
        ev = await record(runner, rt)
        assert ev["text"] == "" and ev["garbled"]
        assert session.store.of("user_turn_untranscribed")
        assert not session.store.of("user_turn_suppressed")
        assert sock.items[0]["text"] == "{}"


@in_a_loop
async def test_annotation_suppression_can_still_be_disabled(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_DROP_ANNOTATIONS", "0")
    runner, session, _ = runner_for("S2A")
    async with bridge(monkeypatch, [Hearing(completed(None))]) as (rt, _, _, _):
        await record(runner, rt)
        assert session.store.of("user_turn")[0]["text"] == "[no speech]"
        assert not session.store.of("user_turn_suppressed")


@pytest.mark.parametrize("text", ["Dan?", "Morgan?", "Hello, I need more time.", "Привет."])
@in_a_loop
async def test_real_text_is_kept_in_the_cascade_context_and_record(monkeypatch, text):
    runner, session, _ = runner_for("S4A")
    async with bridge(monkeypatch, [Hearing(completed(" " + text + "\n"))]) as (rt, sock, _, _):
        await record(runner, rt)
        (turn,) = session.store.of("user_turn")
        assert turn["text"] == text and session.shared_history[-1]["text"] == text
        assert sock._messages()[-1]["content"] == text
        assert not session.store.of("user_turn_suppressed")
        if text == "Привет.":
            assert turn["language_unknown"]


def room_runner(rt):
    runner, session, page = runner_for("S4A")
    room = _room(MODEL, runner)
    room.scribe = rt
    for agent in runner._resolve_agents():
        room.sessions[agent.id] = DeadMember(MODEL)
    runner.room, runner.director = room, session.director
    session.append_agent("dan", "The date is locked.")
    return runner, session, page, room


@pytest.mark.parametrize("arrival", ["before_wait", "during_wait"])
@in_a_loop
async def test_two_rejected_room_turns_do_not_reconnect_the_scribe(monkeypatch, arrival):
    hearings = [Hearing(completed(None)), Hearing(completed(" \t"))]
    async with bridge(monkeypatch, hearings) as (rt, _, _, _):
        runner, session, _, room = room_runner(rt)
        reconnects = []

        async def reopen():
            reconnects.append(True)
            return rt
        room.reopen_scribe = reopen
        runner._turns_without_transcript = 1
        for n in range(2):
            runner._turn_end_arrivals = runner._transcripts_arrived
            runner._turn_end_speech_began = time.time()
            runner._group_turn_waiting = True
            if arrival == "before_wait":
                await record(runner, rt)
                await asyncio.wait_for(runner._run_group_turn(), 2)
            else:
                routing = asyncio.create_task(runner._run_group_turn())
                await until(lambda: runner._route_wake is not None)
                await record(runner, rt)
                await asyncio.wait_for(routing, 2)
            assert len(session.store.of("group_turn_skipped")) == n + 1
            assert runner._turns_without_transcript == 0
            assert not runner._group_turn_waiting and not runner._floor.locked()
        assert all(e["reason"] == "no_speech" for e in session.store.of("group_turn_skipped"))
        assert not session.director.calls and not reconnects
        assert not session.store.of("scribe_reconnected")
        assert not session.store.of("floor_grant_failed")


@in_a_loop
async def test_a_room_routes_real_input_beside_rejected_input(monkeypatch):
    text = "We need more time to finish the pilot."
    async with bridge(monkeypatch, [Hearing(completed(None)), Hearing(completed(text))]) as (rt, _, _, _):
        runner, session, _, _ = room_runner(rt)
        runner._turn_end_arrivals = runner._transcripts_arrived
        runner._turn_end_speech_began = time.time()
        await record(runner, rt)
        await record(runner, rt, ms=2000)
        await asyncio.wait_for(runner._run_group_turn(), 2)
        assert not session.store.of("group_turn_skipped")
        assert session.director.calls[0][1] == text
        assert [e["text"] for e in session.store.of("user_turn")] == [text]
        assert session.store.of("floor_grant_failed"), "the floor was offered"


@pytest.mark.parametrize("override", [None, ROLLBACK, "custom-transcriber"])
@in_a_loop
async def test_effective_transcriber_reaches_session_record_and_analysis(monkeypatch, tmp_path, override):
    from tools import load_analysis_db as analysis

    if override:
        monkeypatch.setenv("CASCADE_STT_MODEL", override)
    monkeypatch.setenv("INPUT_TRANSCRIPTION_MODEL", "whisper-1")
    expected = override or TRANSCRIBE
    async with bridge(monkeypatch, [Hearing(completed("Hello."))]) as (rt, sock, _, requests):
        assert (await transcript(rt))["text"] == "Hello."
        runner, session, _ = runner_for("S2A")
        monkeypatch.setattr(R, "CascadeSocket", lambda **kw: sock)
        monkeypatch.setattr(R, "gateway_api_key", lambda: "k")
        await asyncio.wait_for(runner.run(), 3)
        (event,) = session.store.of("realtime_session_started")
        assert event["input_transcription_model"] == requests[0]["model"] == expected
        assert event["pipeline_version"] == llm.PIPELINE_VERSION
        assert event["pipeline_version"] >= "2026-11-12a"
        assert "input_audio_transcription" not in rt._session_payload()
        sdir = tmp_path / "s_1_ab"
        sdir.mkdir()
        (sdir / "events.jsonl").write_text("".join(json.dumps({"t": n, **e}) + "\n"
            for n, e in enumerate(session.store.events)), encoding="utf-8")
        record = encounter_record.build(sdir)
        (sdir / "record.json").write_text(json.dumps(record), encoding="utf-8")
        saved = json.loads((sdir / "record.json").read_text(encoding="utf-8"))["provenance"]
        assert saved["input_transcription_model"] == expected
        assert analysis.pipeline_provenance(saved, {})["input_transcription_model"] == expected
        assert analysis.pipeline_provenance({}, event)["input_transcription_model"] == expected
