"""The gateway bridge must never stop talking without saying so.

Three ways a live encounter used to go quiet with nothing in the record:

* B7  — one lost `response.done` latched `_response_active`, so every later
        `commit_turn()` was a silent no-op and the character never spoke again.
* B12 — a socket that ended mid-reply dropped the in-flight agent turn, while
        its audio stayed in the assistant WAV: the two halves of the record
        disagreed, and the turn's stage direction was left paired with nothing.
* B38 — a NORMAL close (1000/1001/1005, which is what a bridge recycling a
        connection or a session hitting its duration limit sends) ended
        `events()` with no event at all.

Nothing here opens a socket. The gateway is a scripted fake whose `recv()`
raises the real `websockets` close exceptions, and the stall timeout is turned
down so the watchdog fires in milliseconds instead of 45 seconds.

`{"type": "error"}` is asserted on deliberately: it is the one terminal shape
both pumps in server.realtime_voice_session already act on (a `voice_error` in
the session log, and in 1:1 an error frame on the participant's screen), so an
event of that shape is a mark an analyst can find.

Round two closed four gaps left in that contract, each pinned at the bottom of
this file:

* R15 — a truncated reply must be recognisable AS truncated. The `interrupted`
        flag rides the synthetic `response_done` and the error message that
        follows names the truncation in words. Round three closed the other
        half: the runner now reads that flag onto the turn itself, which
        tests/test_runner_blockers.py pins.
* R16 — `_closing` was a one-way latch, so a reconnected session swallowed
        every genuine gateway close: B38 again, on the hardest path to see.
* R17 — a mid-reply `error` EVENT abandoned the reply with no `response_done`,
        so the words already spoken were glued onto the character's next line.
* R18 — any exception that was not a ConnectionClosed escaped `events()` with
        no terminal event at all, which the module docstring denied.

Round three adds one more distinction, P9: an `error` that the session SURVIVED
(a discarded corrupt audio chunk) is marked `transient` so the runner can record
every one of them and still show the participant at most one notice per reply.
An error that ended something never carries it.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from websockets.frames import Close

from server.voice import realtime
from server.voice.realtime import RealtimeVoiceSession


class FakeGateway:
    """A gateway socket driven from a script.

    Each item is either a raw frame (str) or an exception to raise. When the
    script runs dry the wire simply goes quiet, which is what a stalled reply
    looks like from this side: recv() never returns and the caller's timeout is
    what has to notice.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.closed = False

    async def recv(self):
        while not self.script:
            if self.closed:
                # A socket closed under a pending read raises, as the real one
                # does; without this the reader would hang instead.
                raise closed_ok(1000, "socket closed by the client")
            await asyncio.sleep(0.005)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Faithful to the real client in the one respect that matters here:
        # iterating a websocket swallows a clean close and simply stops, which
        # is why a normal close used to leave no trace at all.
        try:
            return await self.recv()
        except websockets.ConnectionClosedOK:
            raise StopAsyncIteration

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True

    def types_sent(self):
        return [m.get("type") for m in self.sent]


def make_session(script=()) -> RealtimeVoiceSession:
    rt = RealtimeVoiceSession("be someone", api_key="test-key")
    rt.ws = FakeGateway(script)
    return rt


def delta(text="I hear you"):
    return json.dumps({
        "type": "response.output_audio_transcript.delta", "delta": text,
    })


def closed_ok(code=1001, reason="session duration limit"):
    """A polite close: a bridge recycling the connection, or a quota cut-off."""
    frame = Close(code, reason)
    return websockets.ConnectionClosedOK(frame, frame, True)


def closed_error():
    return websockets.ConnectionClosedError(None, None)


async def collect(rt, *, limit=8, timeout=5.0):
    """Pull events until the iterator ends or `limit` are in hand."""
    events = []
    agen = rt.events()
    try:
        while len(events) < limit:
            try:
                events.append(await asyncio.wait_for(agen.__anext__(), timeout))
            except StopAsyncIteration:
                break
    finally:
        await agen.aclose()
    return events


@pytest.fixture(autouse=True)
def fast_watchdog(monkeypatch):
    """45 s in production, milliseconds here. Both are read as module globals
    at call time precisely so this is possible without touching the clock."""
    # raising=False so this file can also be run against a build without the
    # watchdog, where the point is to watch these tests fail on behaviour
    # rather than error on a missing name.
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 0.15, raising=False)
    monkeypatch.setattr(realtime, "RECV_POLL_S", 0.02, raising=False)


# ── B7: a lost response.done must not mute the encounter ──────────────────

def test_stalled_reply_is_reported_and_the_latch_comes_down():
    async def go():
        rt = make_session()          # asked to speak, nothing ever comes back
        await rt.request_response()
        events = await collect(rt, limit=1)
        return rt, events

    rt, events = asyncio.run(go())
    assert [e["type"] for e in events] == ["error"]
    assert "no reply from the gateway" in events[0]["message"]
    # The latch is down, so the next turn is not swallowed.
    assert rt.responding is False
    assert rt.autofire_active is False


def test_the_turn_after_a_stall_actually_asks_for_a_reply():
    """The point of the fix: turn N+1 reaches the gateway. Before it, the
    participant could talk for the rest of the encounter into a session that
    would never send another response.create."""
    async def go():
        rt = make_session()
        await rt.request_response()
        await collect(rt, limit=1)       # the watchdog runs and clears
        rt.ws.sent.clear()
        await rt.send_audio(b"\x01\x02" * 4000)
        await rt.commit_turn()
        return rt.ws.types_sent()

    assert asyncio.run(go()) == [
        "input_audio_buffer.append", "input_audio_buffer.commit", "response.create",
    ]


def test_a_stale_latch_is_overridden_even_with_nobody_draining_events():
    """Backstop for a session whose events() is not being pumped at that
    moment: commit_turn must not obey a flag that is older than any reply."""
    async def go():
        rt = make_session()
        await rt.request_response()
        assert rt.responding is True
        # Age the request past the stall window without touching the clock.
        rt._response_started_at -= 10.0
        rt.ws.sent.clear()
        rt.pending_input = 32000
        await rt.commit_turn()
        return rt.ws.types_sent()

    assert "response.create" in asyncio.run(go())


def test_a_reply_that_is_still_streaming_is_never_called_stalled():
    """No false positives: the watchdog measures from the last sign of life, so
    a long reply that keeps producing audio is left alone."""
    async def go():
        rt = make_session()
        await rt.request_response()

        async def trickle():
            # Slower than the poll interval, so the watchdog does run between
            # deltas, and for longer overall than the stall window.
            for _ in range(6):
                rt.ws.script.append(delta("still going "))
                await asyncio.sleep(0.03)

        events = []
        agen = rt.events()
        producer = asyncio.ensure_future(trickle())
        try:
            for _ in range(6):
                events.append(await asyncio.wait_for(agen.__anext__(), 5.0))
        finally:
            producer.cancel()
            await agen.aclose()
        return events

    events = asyncio.run(go())
    assert [e["type"] for e in events] == ["agent_transcript_delta"] * 6


def test_a_stall_after_partial_speech_closes_the_turn_it_abandons():
    """Words already spoken are already in the assistant WAV. A stall must
    close that turn (response_done) before reporting, or the transcript loses a
    line the participant heard and answered."""
    async def go():
        rt = make_session([delta("So you're letting it go?")])
        await rt.request_response()
        return await collect(rt, limit=3)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == [
        "agent_transcript_delta", "response_done", "error",
    ]
    assert events[1]["interrupted"] is True


def test_clear_response_state_also_drops_the_autofire_latch():
    """The group timeouts call this to unblock a muted character. Leaving
    autofire_active up would make commit_turn() skip the very commit the call
    exists to unblock (see cancel_response)."""
    rt = make_session()
    rt._response_active = True
    rt.autofire_active = True
    rt.clear_response_state()
    assert rt.responding is False
    assert rt.autofire_active is False


# ── B12 / B38: no exit from events() is silent ────────────────────────────

@pytest.mark.parametrize("how,fragment", [
    (closed_ok, "closed by gateway"),
    (closed_error, "connection lost"),
])
def test_every_gateway_close_yields_a_terminal_event(how, fragment):
    """B38: a clean 1000/1001/1005 used to end the iterator with nothing at
    all, so a character went silent leaving no trace anywhere."""
    events = asyncio.run(collect(make_session([how()])))
    assert [e["type"] for e in events] == ["error"]
    assert fragment in events[0]["message"]


def test_a_close_mid_reply_keeps_the_words_the_participant_heard():
    """B12: the deltas were already forwarded to the browser and their audio is
    already in the WAV. Ending without closing the turn left the recording and
    the transcript disagreeing, and the beat's stage direction paired with
    nothing."""
    async def go():
        rt = make_session([delta("So you're "), delta("letting it go?"), closed_ok()])
        await rt.request_response()
        return await collect(rt)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == [
        "agent_transcript_delta", "agent_transcript_delta", "response_done", "error",
    ]
    assert events[2]["interrupted"] is True


def test_a_completed_reply_is_not_closed_a_second_time_by_the_close():
    """response.done already finalised that turn; a duplicate response_done on
    the way out would write the same words into the record twice."""
    async def go():
        rt = make_session([
            delta("done talking"),
            json.dumps({"type": "response.done", "response": {"id": "r1"}}),
            closed_ok(),
        ])
        await rt.request_response()
        return await collect(rt)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == [
        "agent_transcript_delta", "response_done", "error",
    ]


def test_every_abnormal_exit_leaves_the_flags_down():
    """The warning attached to this file's history: a latched flag deadlocks
    the encounter. Whatever ends the iterator, nothing may survive it.

    The reply here is one the bridge auto-fired — no request_response — which
    is the case that latches autofire_active. Left up, the runner's turn loop
    reads it, believes the bridge is already answering, and skips commit_turn()
    forever."""
    async def go():
        rt = make_session([delta("half a "), closed_error()])
        await collect(rt)
        return rt

    rt = asyncio.run(go())
    assert rt.responding is False
    assert rt.autofire_active is False


def test_our_own_close_is_not_reported_as_a_gateway_failure(monkeypatch):
    """A character switch closes the outgoing session on purpose. Reporting
    that would put a spurious error in the record and on the participant's
    screen at every interaction boundary — and closing out the abandoned turn
    would file the old character's words under the new one, since the runner
    has already adopted the replacement by then."""
    # Long enough that the watchdog cannot be what ends this iterator: the
    # close is.
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 30.0)

    async def go():
        rt = make_session([delta("mid-sentence ")])
        await rt.request_response()
        agen = rt.events()
        first = await asyncio.wait_for(agen.__anext__(), 5.0)
        pull = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.01)
        await rt.close()                    # what _switch_character does
        try:
            return first, await asyncio.wait_for(pull, 5.0), rt
        except StopAsyncIteration:
            return first, None, rt
        finally:
            await agen.aclose()

    first, event, rt = asyncio.run(go())
    assert first["type"] == "agent_transcript_delta"
    assert event is None                    # iterator ended, and said nothing
    assert rt.responding is False           # but still left no latch behind
    assert rt.autofire_active is False


# -- R17: a mid-reply `error` EVENT is an abandonment like any other -------

def gateway_error(message="rate limited"):
    """What the gateway sends when a reply dies on its side: a frame, on a
    socket that stays open and goes on to carry the next reply."""
    return json.dumps({"type": "error", "error": {"message": message}})


def test_a_gateway_error_mid_reply_closes_the_turn_it_abandons():
    """The third abandonment path, and the one the stall/close rescues missed.
    Without a response_done the runner never finalises this turn, so the words
    already spoken sit in its buffer and are prepended to the character's NEXT
    line: one assistant_turn holding two separate replies, paired with one
    stage direction. Observed before the fix, from these exact frames:
    assistant_turn text == "Turn one words.Turn two words."."""
    async def go():
        rt = make_session([
            delta("Turn one words."),
            gateway_error(),
            delta("Turn two words."),
            json.dumps({"type": "response.done", "response": {"id": "r2"}}),
        ])
        await rt.request_response()
        return await collect(rt, limit=5)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == [
        "agent_transcript_delta", "response_done", "error",
        "agent_transcript_delta", "response_done",
    ]
    # The first turn is closed, and closed as truncated: a rater has to be able
    # to tell a cut-off delivery from a bad one.
    assert events[1]["interrupted"] is True
    # ...and the second reply is its own turn, ending normally.
    assert events[4].get("interrupted") is not True


def test_an_error_with_no_reply_in_flight_invents_no_turn():
    """The other half: a session-level error, or one for a reply that produced
    nothing, has no words to rescue. Emitting a response_done there would write
    an empty turn into the record - the opposite failure."""
    async def go():
        rt = make_session([gateway_error("session error")])
        await rt.request_response()
        return await collect(rt, limit=1)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == ["error"]


def test_a_gateway_error_still_drops_every_in_flight_flag():
    """The rescue must not cost the clearing it replaced: an errored response
    never reaches response.done, so a latched autofire_active would deadlock
    the encounter (see cancel_response)."""
    async def go():
        rt = make_session([delta("half a "), gateway_error()])
        await rt.request_response()
        await collect(rt, limit=3)
        return rt

    rt = asyncio.run(go())
    assert rt.responding is False
    assert rt.autofire_active is False


# -- R16: _closing is scoped to one socket, not to the object -------------

def test_connect_puts_the_closing_latch_back_down(monkeypatch):
    """B38, re-created on the reconnect path. close() sets _closing so our own
    teardown is not reported as a gateway failure; nothing ever put it down
    again, so a session that was closed and reconnected would report NOTHING
    for a real gateway loss - the silent ending this module exists to remove.
    Latent today only because every call site builds a fresh session."""
    fake = FakeGateway([closed_error()])

    async def fake_connect(url, **kw):
        return fake

    monkeypatch.setattr(realtime.websockets, "connect", fake_connect)

    async def go():
        rt = make_session()
        await rt.close()
        assert rt._closing is True          # the latch, as close() leaves it
        await rt.connect()
        return rt, await collect(rt, limit=1)

    rt, events = asyncio.run(go())
    assert rt._closing is False
    assert [e["type"] for e in events] == ["error"]
    assert "connection lost" in events[0]["message"]


def test_connect_forgets_the_previous_sockets_response_ids(monkeypatch):
    """The same one-way-latch shape, one attribute over: _done_ids dedupes
    response.done within a socket, and a gateway that restarts its ids on a new
    connection would have its first response.done swallowed - the turn would
    never be finalised, and nothing would say why."""
    done = json.dumps({"type": "response.done", "response": {"id": "resp_1"}})

    async def fake_connect(url, **kw):
        return FakeGateway([done, closed_ok()])

    monkeypatch.setattr(realtime.websockets, "connect", fake_connect)

    async def go():
        rt = make_session([done, closed_ok()])
        await collect(rt, limit=2)          # burns "resp_1" on the first socket
        await rt.close()
        await rt.connect()
        return await collect(rt, limit=2)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == ["response_done", "error"]


# -- R18: no exception leaves events() without a terminal event -----------

def test_a_corrupt_audio_frame_costs_one_chunk_not_the_encounter():
    """base64.b64decode on a malformed delta raises binascii.Error. Unguarded
    that escaped events(), tripped run()'s finally in the runner and dropped
    the participant mid-conversation with no voice_error explaining it.
    Observed before the fix, from this frame: events() raised, yielding []."""
    async def go():
        rt = make_session([
            json.dumps({"type": "response.output_audio.delta",
                        "delta": "@@@@not-b64@"}),
            delta("still here"),
            json.dumps({"type": "response.done", "response": {"id": "r1"}}),
        ])
        await rt.request_response()
        return await collect(rt, limit=3)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == [
        "error", "agent_transcript_delta", "response_done",
    ]
    assert "corrupt audio frame" in events[0]["message"]


def test_an_unexpected_failure_still_ends_with_a_terminal_event():
    """The module docstring says events() never ends without saying so. It held
    only for the two ConnectionClosed subclasses: the trailing yields sit after
    the try block, so any other exception skipped them and the iterator ended
    silent."""
    async def go():
        rt = make_session([delta("mid-sentence "), RuntimeError("bridge exploded")])
        await rt.request_response()
        return await collect(rt, limit=4)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == [
        "agent_transcript_delta", "response_done", "error",
    ]
    assert events[1]["interrupted"] is True          # the words are not lost
    assert "RuntimeError" in events[2]["message"]    # and the cause is named


def test_an_unexpected_failure_leaves_no_latch_behind():
    """Same rule as every other exit: whatever ends the iterator, nothing
    in-flight may outlive it."""
    async def go():
        rt = make_session([delta("half a "), RuntimeError("bridge exploded")])
        await collect(rt, limit=3)
        return rt

    rt = asyncio.run(go())
    assert rt.responding is False
    assert rt.autofire_active is False


# -- R15: the truncation is named where a consumer will actually read it --

def test_a_truncated_turns_error_message_says_it_was_truncated():
    """`interrupted: True` on the synthetic response_done is what the runner
    carries onto the turn. The error message that follows every truncation says
    the same thing in words, on both paths, because it is the frame the
    participant's page acts on and the row an analyst reading events.jsonl finds
    first - and on the socket-death path it is the only place the REASON the
    reply stopped is recorded at all."""
    async def stall():
        rt = make_session([delta("So you are letting it ")])
        await rt.request_response()
        return await collect(rt, limit=3)

    async def socket_died():
        rt = make_session([delta("So you are letting it "), closed_ok()])
        await rt.request_response()
        return await collect(rt, limit=3)

    stalled = asyncio.run(stall())
    assert stalled[1]["interrupted"] is True
    assert "cut off" in stalled[2]["message"]

    died = asyncio.run(socket_died())
    assert died[1]["interrupted"] is True
    assert "cut off" in died[2]["message"]
    # The existing close-reason contract is unchanged, only extended.
    assert "closed by gateway" in died[2]["message"]


def test_a_stall_with_nothing_spoken_still_reads_as_no_reply_at_all():
    """The counterpart claim: when the gateway sent nothing, the message must
    NOT say a turn was cut off - there was no turn. A message that overstates
    the loss is as unusable to an analyst as one that hides it."""
    async def go():
        rt = make_session()
        await rt.request_response()
        return await collect(rt, limit=1)

    events = asyncio.run(go())
    assert "no reply from the gateway" in events[0]["message"]
    assert "cut off" not in events[0]["message"]


# -- P9: a fault the session survived is marked as one ---------------------

def test_a_corrupt_frame_error_is_marked_transient():
    """The runner shows the participant at most one notice per reply for faults
    the session survived, and every one of them still reaches events.jsonl. It
    can only tell the two apart if this module says which is which: audio deltas
    arrive every few tens of milliseconds, so an unmarked corrupt-frame error
    filled the participant's transcript with dozens of identical banners."""
    async def go():
        rt = make_session([
            json.dumps({"type": "response.output_audio.delta",
                        "delta": "@@@@not-b64@"}),
            json.dumps({"type": "response.output_audio.delta",
                        "delta": "!!!!also-not@"}),
            json.dumps({"type": "response.done", "response": {"id": "r1"}}),
        ])
        await rt.request_response()
        return await collect(rt, limit=3)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == ["error", "error", "response_done"]
    # Still one row per discarded chunk: that is how an analyst sees how much
    # audio was lost.
    for e in events[:2]:
        assert e["transient"] is True
        assert "corrupt audio frame" in e["message"]


def test_an_error_that_ended_something_is_never_transient():
    """The counterpart claim, and the one that matters more: a fault the session
    did NOT survive must never be throttled off the participant's screen. A
    stall, a gateway `error` frame and a dead socket all end a reply, so none of
    them may be marked as survivable."""
    async def stalled():
        rt = make_session()
        await rt.request_response()
        return await collect(rt, limit=1)

    async def errored():
        rt = make_session([
            delta("half a "),
            json.dumps({"type": "error", "error": {"message": "quota"}}),
        ])
        await rt.request_response()
        return await collect(rt, limit=3)

    async def socket_died():
        rt = make_session([delta("half a "), closed_error()])
        await rt.request_response()
        return await collect(rt, limit=3)

    for events in (asyncio.run(stalled()), asyncio.run(errored()),
                   asyncio.run(socket_died())):
        errors = [e for e in events if e["type"] == "error"]
        assert errors, "a reply ended with no error event"
        for e in errors:
            assert e.get("transient") is not True, (
                f"a terminal failure was marked survivable: {e['message']}"
            )
