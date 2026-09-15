"""The realtime bridge has to be correct on whichever family REALTIME_MODEL names.

The study's consent form says the participant is talking to a Google model, so
the model is not ours to change (config/consent.yaml). But "not ours to change"
is not the same as "the only one the code may assume", and until this round the
bridge assumed one family's answers for both: a Gemini voice as the hardcoded
default, a session dict with no room in it for what the other family needs, and
a steering call that reported success the moment the bytes left the process.

Every claim asserted here was probed live against api.ai.it.cornell.edu on
2026-09-10, one short socket per case. Nothing in this file opens one: the
gateway is a scripted fake. What the probes found, and what these tests pin:

* A voice is only ever correct with respect to a model. "Puck" is refused by
  gpt-realtime-2.1 with `invalid_value`, and the gateway then discards the
  WHOLE session.update -- no `session.updated`, no character brief, an actor
  that answers fluently as the gateway's stock assistant. Gemini refuses an
  unknown voice by saying nothing at all: an ElevenLabs voice id (which is what
  scenarios/g1..g5 carry) produced no ack, no error, no audio and no transcript
  for the whole life of the socket.
* gpt-realtime-2.1 returns NO participant transcript unless the session dict
  asks for one. That channel is the study's primary measurement.
* Server VAD on the gpt family does not merely fire early, it cuts the turn up:
  one 4.7 s utterance came back as two participant transcripts and an empty
  reply. `turn_detection: null` made it one transcript and one reply.
* Mid-session `session.update` is acknowledged and obeyed on gpt-realtime-2.1
  (38 ms) and neither on nto.gemini-live-2.5-flash (three frames, no acks, and
  an actor that went on ignoring the direction). Every stage direction the
  platform issues travels by that frame, so on Gemini the steering record was
  describing deliveries that never happened.

So: one named table, `REALTIME_FAMILIES`, that a person can read in one place,
and a bridge that builds its session from it, refuses a voice that family will
not take, and tells its callers the truth about what arrived.

Async tests are written the way the rest of this suite writes them -- a nested
`go()` under `asyncio.run` -- because pytest-asyncio is not a dependency of
this project and a test that needs one silently does not run.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets
from websockets.frames import Close

from server.voice import realtime
from server.voice.realtime import (
    REALTIME_FAMILIES,
    RealtimeVoiceSession,
    UnknownRealtimeModel,
    UnsupportedVoice,
    capabilities_for,
    family_of,
    resolve_voice,
)

GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"


class FakeGateway:
    """A gateway socket driven from a script, as in test_voice_blockers.

    `acks_updates` is the one new knob: a family that answers a session.update
    with a session.updated frame, which is the only evidence anywhere that a
    stage direction arrived.
    """

    def __init__(self, script=(), acks_updates=False):
        self.script = list(script)
        self.sent = []
        self.closed = False
        self.acks_updates = acks_updates
        self.send_error = None

    async def recv(self):
        while not self.script:
            if self.closed:
                frame = Close(1000, "socket closed by the client")
                raise websockets.ConnectionClosedOK(frame, frame, True)
            await asyncio.sleep(0.005)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, payload):
        if self.send_error is not None:
            raise self.send_error
        msg = json.loads(payload)
        self.sent.append(msg)
        if self.acks_updates and msg.get("type") == "session.update":
            self.script.append(json.dumps({"type": "session.updated",
                                           "session": {"id": "s1"}}))

    async def close(self):
        self.closed = True

    def updates(self):
        return [m for m in self.sent if m.get("type") == "session.update"]


def patch_connect(monkeypatch, gateway):
    """Stand in for websockets.connect and record whether it was reached."""
    opened = []

    async def fake_connect(url, **kwargs):
        opened.append(url)
        return gateway

    monkeypatch.setattr(realtime.websockets, "connect", fake_connect)
    return opened


async def collect(rt, *, limit=8, timeout=5.0):
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


# -- the table -------------------------------------------------------------

def test_the_two_families_are_answered_separately_not_from_one_default():
    """S1.1. Four questions, two families, and no answer shared by assumption.

    This is the whole point of the table. Before it, the bridge's answers were
    Gemini's answers whatever REALTIME_MODEL said, so pointing the study at the
    other family produced a session with no participant transcription, server
    VAD chopping the turns up, and a voice the gateway refuses.
    """
    gemini = capabilities_for(GEMINI)
    gpt = capabilities_for(GPT)
    assert gemini is not None and gpt is not None
    assert gemini.family == "gemini-live" and gpt.family == "gpt-realtime"

    # Every one of the four differs. A row that agreed with the other row on
    # all of them would not need to exist.
    assert gemini.needs_input_transcription is False
    assert gpt.needs_input_transcription is True
    assert gemini.needs_turn_detection_null is False
    assert gpt.needs_turn_detection_null is True
    assert gemini.honours_session_update is False
    assert gpt.honours_session_update is True
    assert set(gemini.voices).isdisjoint(gpt.voices)


def test_a_voice_is_only_correct_with_respect_to_a_model():
    """S1.2. The rosters do not overlap, so there is no such thing as a safe
    default voice for "the realtime model" in the abstract. Probed both ways:
    Puck acked on Gemini and was refused by gpt; alloy acked on gpt and was
    ignored by Gemini."""
    assert resolve_voice(GEMINI) in REALTIME_FAMILIES["gemini-live"].voices
    assert resolve_voice(GPT) in REALTIME_FAMILIES["gpt-realtime"].voices
    assert resolve_voice(GEMINI) != resolve_voice(GPT)

    with pytest.raises(UnsupportedVoice):
        resolve_voice(GPT, "Puck")
    with pytest.raises(UnsupportedVoice):
        resolve_voice(GEMINI, "alloy")
    # The one that actually reached a live session: a v1 ElevenLabs voice id
    # out of scenarios/g1_hidden_profile_vendor.yaml.
    with pytest.raises(UnsupportedVoice):
        resolve_voice(GEMINI, "EXAVITQu4vr4xnSDxMaL")


def test_a_model_the_table_does_not_cover_is_refused_rather_than_guessed():
    """S1.3. Guessing a family is how a study runs on a model whose behaviour
    nobody checked. The lookup other modules read the table through answers
    None for it; the one this module connects through refuses."""
    assert family_of("claude-opus-4") == ""
    assert capabilities_for("claude-opus-4") is None
    with pytest.raises(UnknownRealtimeModel):
        realtime.require_capabilities("claude-opus-4")

    # Both members of each family resolve, including the ones the gateway
    # offers beside the two the study has used.
    assert family_of("gpt-realtime-2.1-mini") == "gpt-realtime"
    # THE NATIVE-AUDIO SIBLING IS ITS OWN FAMILY, and this line used to assert
    # the opposite. origin/main measured why (2026-09-08): folded into the
    # gemini-live row it is fed 16 kHz input, which it accepts and then ignores
    # forever -- session open, no transcription, no reply, no error. It also
    # differs on the autofire wait, on how the floor is granted, on whether
    # colleagues arrive as text, and on whether room members may hold tools.
    # Six columns is a family, not a member. Production runs this route.
    assert (family_of("nto.gemini-live-2.5-flash-native-audio")
            == "gemini-live-native-audio")
    # ...and the plain route is untouched by the narrower test going first.
    assert family_of("nto.gemini-live-2.5-flash") == "gemini-live"


# -- the session dict ------------------------------------------------------

def test_the_session_dict_is_flat_and_carries_only_what_the_family_needs():
    """S1.4. Flat, because a nested `audio: {...}` block is what leaves a
    Gemini session alive and permanently mute; and per-family, because the two
    keys the gpt family cannot work without are the two Gemini has never been
    shown to want."""
    rt = RealtimeVoiceSession("be dana", model=GPT, voice="alloy",
                              api_key="test-key")
    sent = rt._session_payload()
    assert sent["instructions"] == "be dana"
    assert sent["voice"] == "alloy"
        # {"language": "en"} rides on the same dict: TRANSCRIPTION_LANG,
        # default "en", from origin/main cabc1dd. It is not decoration --
        # without it the transcriber returned a Russian word and Japanese
        # syllables from an English-speaking participant, and the
        # participant transcript is the measurement. Verified 2026-09-08
        # not to mute either Gemini route.
    assert sent["input_audio_transcription"] == {"model": "whisper-1",
                                                "language": "en"}
    assert "turn_detection" in sent and sent["turn_detection"] is None
    assert "audio" not in sent and "modalities" not in sent

    gem = RealtimeVoiceSession("be dana", model=GEMINI, voice="Puck",
                               api_key="test-key")
    # Still flat, still only what the row asks for -- and the language hint,
    # which on this family is the WHOLE of input_audio_transcription (the row
    # says the transcript arrives unasked, so no model key is sent).
    # (No turn_detection: a bare session built by nobody in particular leaves
    # it UNSET and sends no such key, exactly as before. The runner and the
    # room set the family's measured window on the sessions they open.)
    assert gem._session_payload() == {
        "instructions": "be dana", "voice": "Puck",
        "input_audio_transcription": {"language": "en"},
    }

    # And TRANSCRIPTION_LANG= (blank) still means "send no hint at all", which
    # is the escape hatch if a transcriber is ever measured to mind it.
    import os
    old = os.environ.get("TRANSCRIPTION_LANG")
    os.environ["TRANSCRIPTION_LANG"] = ""
    try:
        bare = RealtimeVoiceSession("be dana", model=GEMINI, voice="Puck",
                                    api_key="test-key")._session_payload()
        assert "input_audio_transcription" not in bare
    finally:
        if old is None:
            os.environ.pop("TRANSCRIPTION_LANG", None)
        else:
            os.environ["TRANSCRIPTION_LANG"] = old


def test_connect_sends_the_table_row_not_a_hardcoded_shape(monkeypatch):
    """S1.5. The dict that actually goes on the wire at connect comes from the
    row, so a change of REALTIME_MODEL changes the session with it."""
    gw = FakeGateway()
    patch_connect(monkeypatch, gw)

    async def go():
        rt = RealtimeVoiceSession("be dana", model=GPT, voice="cedar",
                                  api_key="test-key")
        await rt.connect()

    asyncio.run(go())
    update = gw.updates()[0]["session"]
    assert update["voice"] == "cedar"
    assert update["input_audio_transcription"] == {"model": "whisper-1",
                                                  "language": "en"}
    assert update["turn_detection"] is None


def test_an_empty_voice_setting_becomes_the_models_own_default(monkeypatch):
    """S1.6. REALTIME_VOICE used to default to "Puck" -- one family's voice
    standing in as the answer for both, which on the other family means the
    character brief is discarded at every connect."""
    gw = FakeGateway()
    patch_connect(monkeypatch, gw)

    async def go():
        rt = RealtimeVoiceSession("be dana", model=GPT, voice="",
                                  api_key="test-key")
        await rt.connect()
        return rt

    rt = asyncio.run(go())
    assert rt.voice == REALTIME_FAMILIES["gpt-realtime"].default_voice
    assert gw.updates()[0]["session"]["voice"] == rt.voice


# -- the voice, before and after the socket --------------------------------

def test_a_voice_the_family_refuses_never_reaches_a_socket(monkeypatch):
    """S1.7. Checked BEFORE connecting, because on Gemini there is nothing to
    check afterwards: an unknown voice produced no ack, no error and no audio
    at all. A session that cannot be right is not opened."""
    gw = FakeGateway()
    opened = patch_connect(monkeypatch, gw)

    async def go():
        rt = RealtimeVoiceSession("be dana", model=GEMINI,
                                  voice="EXAVITQu4vr4xnSDxMaL",
                                  api_key="test-key")
        with pytest.raises(UnsupportedVoice):
            await rt.connect()

    asyncio.run(go())
    assert opened == []
    assert gw.sent == []


def test_a_rejected_voice_is_fatal_rather_than_a_stock_assistant():
    """S1.8. The live shape of the gpt refusal, replayed: an `invalid_value`
    on session.audio.output.voice and then silence where the session.updated
    should be. The brief did not land, so anything this session says next is
    the gateway's stock persona wearing the study's name. It must not be
    survivable: one fatal error, and the session goes."""
    async def go():
        rt = RealtimeVoiceSession("be dana", model=GPT, voice="alloy",
                                  api_key="test-key")
        rt.ws = FakeGateway([json.dumps({"type": "error", "error": {
            "type": "invalid_request_error", "code": "invalid_value",
            "message": ("Invalid value: 'Puck'. Supported values are: 'alloy', "
                        "'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', "
                        "'verse', 'marin', and 'cedar'."),
            "param": "session.audio.output.voice",
        }})])
        return rt, await collect(rt)

    rt, events = asyncio.run(go())
    assert [e["type"] for e in events] == ["error"]
    assert events[0].get("fatal") is True
    assert not events[0].get("transient")
    assert "voice" in events[0]["message"]
    assert rt.ws is None


# -- did the brief arrive? -------------------------------------------------

def test_a_brief_is_delivered_only_when_the_platform_says_so():
    """S1.9. True means a session.updated came back for THIS update, which on
    gpt-realtime-2.1 it does, in tens of milliseconds. Nothing weaker counts:
    the old return happened when ws.send did, which is a statement about this
    process and not about the actor."""
    async def go():
        rt = RealtimeVoiceSession("be dana", model=GPT, voice="alloy",
                                  api_key="test-key")
        rt.ws = FakeGateway(acks_updates=True)
        drained = []

        async def drain():
            async for ev in rt.events():
                drained.append(ev)

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.05)
        try:
            acked = await rt.update_instructions(
                "be dana. STAGE DIRECTION: push back")
        finally:
            task.cancel()
        return rt, acked, drained

    rt, acked, drained = asyncio.run(go())
    assert acked is True
    assert rt.last_update_acked is True
    assert rt.instructions.endswith("push back")
    # The ack is evidence for update_instructions, not an event for the runner.
    assert [e["type"] for e in drained] == []


def test_the_family_that_ignores_steering_says_so_at_once():
    """S1.10. Gemini answers no mid-session update, so there is nothing to wait
    for. False, immediately: an encounter that steers on every turn cannot
    spend the ack timeout per beat to be told what the table already knows."""
    async def go():
        rt = RealtimeVoiceSession("be dana", model=GEMINI, voice="Puck",
                                  api_key="test-key")
        rt.ws = FakeGateway()
        started = time.time()
        acked = await rt.update_instructions(
            "be dana. STAGE DIRECTION: push back")
        return rt, acked, time.time() - started

    rt, acked, elapsed = asyncio.run(go())
    assert acked is False
    assert rt.last_update_acked is False
    assert rt.unacked_updates == 1
    assert elapsed < 0.5
    # It still went out. The brief is written to the record either way, and a
    # gateway that starts honouring these will be believed the moment it does.
    assert rt.ws.updates()


def test_nobody_listening_is_reported_as_unknown_not_as_refused():
    """S1.11. events() is the only reader of the socket, so it is the only
    place an ack can be seen. With no one draining it, "no ack observed" is a
    fact about this process; recording it as a refusal would put a false
    finding in the steering log, which is the exact failure this whole change
    exists to end."""
    async def go():
        rt = RealtimeVoiceSession("be dana", model=GPT, voice="alloy",
                                  api_key="test-key")
        rt.ws = FakeGateway(acks_updates=True)
        return rt, await rt.update_instructions("be dana. DIRECTION: push back")

    rt, acked = asyncio.run(go())
    assert acked is None
    assert rt.last_update_acked is None
    assert rt.unacked_updates == 0


def test_a_brief_that_never_left_is_not_reported_as_unknown():
    """S1.11b. A frame that could not be sent at all is a direction that
    certainly did not arrive, which is a finding; "nobody was listening for the
    ack" is a different one. Seen live at the end of the fatal-voice path,
    where the session had already been closed underneath the director."""
    async def go():
        rt = RealtimeVoiceSession("be dana", model=GPT, voice="alloy",
                                  api_key="test-key")
        rt.ws = FakeGateway(acks_updates=True)
        rt.ws.send_error = websockets.ConnectionClosedError(None, None)
        dead = await rt.update_instructions("be dana. DIRECTION: push back")
        rt.ws = None
        gone = await rt.update_instructions("be dana. DIRECTION: push back")
        return rt, dead, gone

    rt, dead, gone = asyncio.run(go())
    assert dead is False and gone is False
    assert rt.last_update_acked is False
    assert rt.unacked_updates == 2


def test_steering_does_not_hand_server_vad_back_mid_encounter(monkeypatch):
    """S1.12. A session.update is a whole-session statement, not a patch. The
    re-brief used to send instructions, voice and tools alone, so on the gpt
    family the first stage direction of an encounter would have dropped
    `turn_detection: null` and started cutting the participant's turns in half
    from that beat onwards."""
    gw = FakeGateway(acks_updates=True)
    patch_connect(monkeypatch, gw)

    async def go():
        rt = RealtimeVoiceSession("be dana", model=GPT, voice="alloy",
                                  api_key="test-key")
        await rt.connect()
        await rt.update_instructions("be dana. STAGE DIRECTION: interrupt")

    asyncio.run(go())
    first, second = (m["session"] for m in gw.updates()[:2])
    assert second["turn_detection"] is None
    assert second["input_audio_transcription"] == first["input_audio_transcription"]
    assert second["voice"] == first["voice"]


# -- errors that are not losses --------------------------------------------

def test_a_refused_second_response_does_not_abandon_the_first():
    """S1.13. Both families start a reply off the commit alone, so
    commit_turn's response.create arrives a beat late and is refused with
    `conversation_already_has_active_response`. Seen live on gpt-realtime-2.1,
    mid-reply, twice. Treated as a loss it was actively harmful: the turn would
    be closed out as interrupted while the gateway was still speaking it, and
    the rest of the sentence filed under whatever the character said next."""
    async def go():
        rt = RealtimeVoiceSession("be dana", model=GPT, voice="alloy",
                                  api_key="test-key")
        rt.ws = FakeGateway([
            json.dumps({"type": "response.output_audio_transcript.delta",
                        "delta": "Okay, let us"}),
            json.dumps({"type": "error", "error": {
                "type": "invalid_request_error",
                "code": "conversation_already_has_active_response",
                "message": ("Conversation already has an active response in "
                            "progress: resp_EMhA7lH9hfyqPQGiw6rse."),
                "param": None,
            }}),
            json.dumps({"type": "response.output_audio_transcript.delta",
                        "delta": " focus on what slipped."}),
            json.dumps({"type": "response.output_audio_transcript.done",
                        "transcript": "Okay, let us focus on what slipped."}),
            json.dumps({"type": "response.done", "response": {"id": "r1"}}),
        ])
        return await collect(rt, limit=5)

    events = asyncio.run(go())
    kinds = [e["type"] for e in events]
    assert kinds == ["agent_transcript_delta", "error", "agent_transcript_delta",
                     "agent_transcript", "response_done"]
    assert events[1]["transient"] is True
    # The reply was never abandoned: one response_done, and not an interrupted
    # one, so the turn closes once and holds the whole sentence.
    assert kinds.count("response_done") == 1
    assert not events[-1].get("interrupted")
    assert events[3]["text"] == "Okay, let us focus on what slipped."


# -- the escape path -------------------------------------------------------

def test_a_socket_dying_mid_append_does_not_take_the_microphone_with_it():
    """S1.14. `send_audio` raised straight out into the runner's
    participant->model pump, whose one broad `except` records the fault and
    RETURNS -- ending the only coroutine that relays the participant's
    microphone, for the rest of the encounter, across the character switch that
    installs a healthy new session. In a group room `GroupRoom.hear` gathers a
    send per member, so one dead member socket stopped the room hearing
    anything at all.

    A dead socket is not news that has to travel by exception: events() already
    reports the close in words. So the send is counted and dropped, and every
    part of the encounter that still works keeps working."""
    async def go():
        rt = RealtimeVoiceSession("be dana", model=GEMINI, voice="Puck",
                                  api_key="test-key")
        rt.ws = FakeGateway()
        rt.ws.send_error = websockets.ConnectionClosedError(None, None)
        await rt.send_audio(b"\x01\x02" * 800)
        appended = rt.last_send_error
        await rt.commit_input()
        return rt, appended

    rt, appended = asyncio.run(go())
    assert rt.send_failures == 2
    assert appended.startswith("input_audio_buffer.append")
    assert rt.last_send_error.startswith("input_audio_buffer.commit")


def test_the_close_the_gateway_reports_is_still_the_one_that_speaks():
    """S1.15. The counterpart of the test above: dropping the send must not
    cost the report. The socket's death is still announced, once, by events(),
    which is the module's single channel for it."""
    async def go():
        rt = RealtimeVoiceSession("be dana", model=GEMINI, voice="Puck",
                                  api_key="test-key")
        rt.ws = FakeGateway([websockets.ConnectionClosedError(None, None)])
        return await collect(rt, limit=3)

    events = asyncio.run(go())
    assert [e["type"] for e in events] == ["error"]
    assert "connection lost" in events[0]["message"]
