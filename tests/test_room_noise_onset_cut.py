"""A floor holder's reply cut by a noise onset before any of it was heard is
asked for again once the onset turns out to be no speech (pipeline 2026-11-14a).

The record (S4A s_1794589855_4e0480, ac07319): Chris was granted at 345.3 s;
at 345.9 s a mic onset loud enough for the barge-in bar cut his reply before a
frame of it played (empty_response, in the same millisecond as the onset's
input_buffer_cleared), and 2 s later the onset was suppressed as [no speech]
and the turn skipped. Nobody answered; the same happened again at 354.8 s.

A heard reply is not asked again, and a real interruption routes as before.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server.voice import cascade  # noqa: E402
from server.voice import realtime as R  # noqa: E402
from cascade_room_harness import CASCADE, QUIET, WORDS, speak  # noqa: E402
from test_bridge_correctness import LOUD, settle  # noqa: E402
from test_cascade_voice import FakeTTS, _sse  # noqa: E402
from test_room_member_reconnect import (  # noqa: E402
    Room, created, in_a_loop, quick, reply, until,  # noqa: F401
)

LINE = "Chris is outside getting you."


def _asks(ws):
    return [m.get("type") for m in ws.sent].count("response.create")


async def _granted_then_cut(h, commits):
    """A member (the lead) is granted the participant's line; a loud onset
    cuts its reply before any of it reaches the page. Returns its id."""
    await h.participant(LINE)
    turn = asyncio.ensure_future(h.runner._run_group_turn())
    await until(lambda: commits)
    await until(lambda: (h.room.last_grant or {}).get("via") == "commit")
    h.page._frames = [LOUD] * 25
    await h.runner._client_to_model()
    await asyncio.wait_for(turn, 10)
    await settle(h.runner)
    assert h.of("empty_response")
    return next(a for a in h.room.sessions if h.rt(a).ws is commits[0])


async def _onset_verdict(h, text, voiced_ms):
    """The onset's turn ends, then its transcript lands with the verdict."""
    turn = asyncio.ensure_future(h.runner._run_group_turn())
    await asyncio.sleep(0.05)
    await h.runner._record_user_turn(text, voiced_ms=voiced_ms,
                                     voiced_span_ms=voiced_ms,
                                     spoken_at=time.time())
    return turn


def _counting_commits(h):
    commits = []

    def on_commit(ws, f):
        commits.append(ws)
        return [created(f"R{len(commits)}")]
    h.gateway.on_commit = on_commit
    return commits


@in_a_loop
async def test_a_reply_cut_unheard_by_a_no_speech_onset_is_asked_again(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        commits = _counting_commits(h)
        aid = await _granted_then_cut(h, commits)
        mem_ws = h.rt(aid).ws
        before = _asks(mem_ws) + len(commits)
        turn = await _onset_verdict(h, "[no speech]", 300)
        await until(lambda: _asks(mem_ws) + len(commits) > before)
        await until(lambda: h.of("cut_reply_rerequested"))
        mem_ws.feed(reply(f"R{len(commits)}", "He just walked in, actually."))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    (skip,) = h.of("group_turn_skipped")
    assert skip["reason"] == "no_speech"
    (asked,) = h.of("cut_reply_rerequested")
    assert asked["agent_id"] == aid and asked["requested"] is True
    (said,) = h.of("assistant_turn")
    assert said["agent_id"] == aid and said["text"] == "He just walked in, actually."
    assert not h.of("group_turn_timeout")


@in_a_loop
async def test_a_real_interruption_is_routed_not_asked_again(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        commits = _counting_commits(h)
        aid = await _granted_then_cut(h, commits)
        mem_ws = h.rt(aid).ws
        turn = await _onset_verdict(h, "Wait, who is outside?", 1400)
        await until(lambda: len(commits) >= 2)
        commits[-1].feed(reply(f"R{len(commits)}", "Chris is."))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    assert not h.of("group_turn_skipped")
    assert not h.of("cut_reply_rerequested")


@in_a_loop
async def test_a_reply_cut_after_it_was_heard_is_not_asked_again(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        commits = _counting_commits(h)
        aid = await _granted_then_cut(h, commits)
        # As if the cut reply had reached the page.
        assert h.runner._unheard_cut is not None
        h.runner._unheard_cut = None
        mem_ws = h.rt(aid).ws
        before = _asks(mem_ws) + len(commits)
        turn = await _onset_verdict(h, "[no speech]", 300)
        await asyncio.wait_for(turn, 10)
        await settle(h.runner)
        assert _asks(mem_ws) + len(commits) == before
    finally:
        await h.close()
    assert h.of("group_turn_skipped")
    assert not h.of("cut_reply_rerequested")


@in_a_loop
async def test_the_barge_in_records_an_unheard_cut_only(monkeypatch):
    """The cut is recorded for a reply none of which reached the page."""
    h = await Room(monkeypatch).open()
    try:
        commits = _counting_commits(h)
        aid = await _granted_then_cut(h, commits)
        cut = h.runner._unheard_cut
    finally:
        await h.close()
    assert cut is not None and cut["agent_id"] == aid
    assert cut["onset"] == h.runner._speech_started_at


@in_a_loop
async def test_on_the_cascade_route_the_cut_member_answers_after_a_noise_onset(
        monkeypatch):
    """End to end on the production route (cascade-argon-elevenlabs): real
    microphone frames, the scribe's own transcription, the member's real
    cascade reply. The first reply is held at the LLM until a loud onset has
    cut it; the onset is transcribed as [no speech]."""
    # The onset is past the split-turn window, as at 345.9 s.
    monkeypatch.setenv("ROOM_SPLIT_TURN_S", "0")
    gate = asyncio.Event()
    chats, stts = [], []
    heard = [WORDS, "[no speech]"]

    async def handle(req):
        body = json.loads(req.content)
        if body.get("stream"):
            chats.append(body)
            if len(chats) == 1:
                await gate.wait()
            return _sse("He just walked in, actually.")
        stts.append(body)
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop",
            "message": {"content": heard[min(len(stts), len(heard)) - 1]}}]})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")
    monkeypatch.setattr(R, "gateway_api_key", lambda: "k")

    async def opener():
        return FakeTTS({})
    monkeypatch.setattr(cascade, "open_tts", opener)
    real = cascade.CascadeSocket
    monkeypatch.setattr(R, "CascadeSocket", lambda **kw: real(
        http=httpx.AsyncClient(transport=transport), **kw))

    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1, 10)
        aid = h.room.speaking
        assert aid
        # A knock: loud enough and long enough for the barge-in bar.
        h.page._frames = [LOUD] * 35 + [QUIET] * 120
        await h.runner._client_to_model()
        await until(lambda: h.of("group_turn_skipped"), 10)
        await until(lambda: len(chats) == 2, 10)
        await until(lambda: h.of("assistant_turn"), 10)
        await until(lambda: not h.runner._group_turn_tasks, 10)
        await settle(h.runner)
    finally:
        gate.set()
        await h.close()
    assert h.of("empty_response")[0]["agent_id"] == aid
    (skip,) = h.of("group_turn_skipped")
    assert skip["reason"] == "no_speech"
    (asked,) = h.of("cut_reply_rerequested")
    assert asked["agent_id"] == aid and asked["requested"] is True
    (said,) = h.of("assistant_turn")
    assert said["agent_id"] == aid and said["text"] == "He just walked in, actually."
