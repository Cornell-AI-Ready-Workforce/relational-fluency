"""Phase 0 of the #21-#25 fixes: instrumentation, and nothing else.

Everything here pins a fact being WRITTEN DOWN, and the one rule that goes
with it: none of it may change what a participant hears. Four things:

  1. Provenance. The record has to say what the pipeline did to the audio
     (input rate, transcriber, reply cap, resamplers) and which pipeline /
     room-pacing version ran, because the fixes land mid-study and the archive
     has to be split at each one.
  2. The browser's microphone. getSettings() and the user agent, with the
     device's identity left out.
  3. Where the time goes (#25): one turn_timing event per agent turn, from the
     VAD's turn end to the page's own "this started playing" ack.
  4. cap_truncated (#23): the gateway's response.done says the session's
     max_output_tokens ran out. Recorded on the turn, and NEVER a retry,
     because re-asking would speak the line twice.

No network. The gateway frames are the shapes measured by the #23 probe
(scratchpad diag/track3/probe_tokens.json): status "incomplete",
status_details {"type": "incomplete", "reason": "max_output_tokens"}, two
output items.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import encounter_record, llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.turn_timing import MAX_OPEN, NullTimer, TurnTimer  # noqa: E402
from server.voice import realtime  # noqa: E402
from server.voice.realtime import RealtimeVoiceSession  # noqa: E402

V2 = ROOT / "static" / "v2.html"
GPT = "gpt-realtime-2.1"
NATIVE = "nto.gemini-live-2.5-flash-native-audio"


def in_a_loop(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


# --------------------------------------------------------------------------
# 1. Provenance
# --------------------------------------------------------------------------

def test_provenance_names_what_the_bridge_does_to_the_audio_per_model():
    gpt = llm.provenance(GPT)
    # The values the gpt row sends since pipeline 2026-09-23b (issues #21 and
    # #23); before it they were 16000, whisper-1 and 380, and this is where
    # that change is visible on every record.
    assert gpt["input_rate"] == 24000
    assert gpt["input_transcription_model"] == "gpt-4o-transcribe"
    assert gpt["max_output_tokens"] == 1200
    assert gpt["resampler"] == realtime.resampler_name(
        realtime.GATEWAY_OUTPUT_RATE, realtime.CLIENT_RATE)
    assert gpt["resampler"] in ("audioop.ratecv", "linear-py")
    assert gpt["input_resampler"] in ("audioop.ratecv", "linear-py"), (
        "the browser's 16 kHz is resampled up to the 24 kHz the gateway reads")
    native = llm.provenance(NATIVE)
    assert native["input_rate"] == 24000
    assert native["input_resampler"] is not None
    # Gemini transcribes on its own; nothing is asked for, and the record
    # must not claim a transcriber that was never requested.
    assert native["input_transcription_model"] is None
    assert native["max_output_tokens"] is None
    for prov in (gpt, native):
        assert prov["pipeline_version"] == llm.PIPELINE_VERSION
        assert prov["room_pacing_version"] == llm.ROOM_PACING_VERSION
        # The keys that were there before are all still there.
        for key in ("gateway", "text_model", "realtime_model",
                    "steering_model", "director_model"):
            assert key in prov


def test_the_versions_are_plain_dated_strings():
    for v in (llm.PIPELINE_VERSION, llm.ROOM_PACING_VERSION):
        assert isinstance(v, str) and len(v) >= 10 and v[:4].isdigit()


def test_provenance_agrees_with_the_session_the_bridge_actually_sends():
    """The record must state what went on the wire, so it is read from the
    same row the session payload is built from, and they cannot drift."""
    rt = RealtimeVoiceSession("x", model=GPT, voice="cedar", api_key="k")
    payload = rt._session_payload()
    prov = realtime.audio_provenance(GPT)
    assert payload["max_output_tokens"] == prov["max_output_tokens"]
    assert payload["input_audio_transcription"]["model"] == prov["input_transcription_model"]
    assert rt.input_rate == prov["input_rate"]


def test_the_session_started_event_is_stamped_for_the_running_model():
    """Read off the source: run() opens the session and then stamps the event
    with provenance() for THAT session's model, not the configured one (and,
    from 10-01a's review, whether the encounter has a room)."""
    src = (ROOT / "server" / "realtime_voice_session.py").read_text(encoding="utf-8")
    at = src.index('"realtime_session_started", model=self.rt.model')
    assert "**provenance(self.rt.model, room=" in src[at:at + 700]


def _write(root: Path, events) -> Path:
    sdir = root / "s_1790000000_abcdef"
    sdir.mkdir(parents=True)
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return sdir


def test_the_record_carries_the_new_provenance_and_the_microphone(tmp_path):
    sdir = _write(tmp_path, [
        {"t": 0.0, "type": "session_start", "participant_id": "p"},
        {"t": 0.1, "type": "realtime_session_started", "model": GPT,
         "gateway": "g", **llm.provenance(GPT)},
        {"t": 0.2, "type": "client_audio_settings",
         "settings": {"sampleRate": 44100}, "user_agent": "UA/1"},
        {"t": 9.0, "type": "client_audio_settings",
         "settings": {"sampleRate": 48000}, "user_agent": "UA/2"},
        {"t": 5.0, "type": "steering_pair",
         "actor": {"agent_id": "a", "text": "and so the date is", "voice": "cedar",
                   "transcript_missing": False, "interrupted": False,
                   "cap_truncated": True},
         "direction": None},
    ])
    rec = encounter_record.build(sdir)
    prov = rec["provenance"]
    assert prov["input_rate"] == 24000
    assert prov["input_transcription_model"] == "gpt-4o-transcribe"
    assert prov["max_output_tokens"] == 1200
    assert prov["pipeline_version"] == llm.PIPELINE_VERSION
    assert prov["room_pacing_version"] == llm.ROOM_PACING_VERSION
    assert prov["resampler"] and "input_resampler" in prov
    # The later report wins: that is the device the rest was captured on.
    assert rec["client_audio"]["settings"] == {"sampleRate": 48000}
    assert rec["client_audio"]["user_agent"] == "UA/2"
    agent = [t for t in rec["transcript"] if t["role"] == "agent"]
    assert agent[0]["cap_truncated"] is True


def test_an_old_record_says_unknown_not_uncut(tmp_path):
    sdir = _write(tmp_path, [
        {"t": 0.0, "type": "session_start", "participant_id": "p"},
        {"t": 0.1, "type": "realtime_session_started", "model": GPT},
        {"t": 5.0, "type": "steering_pair",
         "actor": {"agent_id": "a", "text": "hi"}, "direction": None},
    ])
    rec = encounter_record.build(sdir)
    assert rec["provenance"]["pipeline_version"] is None
    assert rec["client_audio"] is None
    assert rec["transcript"][0]["cap_truncated"] is None


# --------------------------------------------------------------------------
# 4. cap_truncated at the bridge (before the runner, because the runner test
#    below reads what this produces)
# --------------------------------------------------------------------------

class FakeGateway:
    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.closed = False

    async def recv(self):
        while not self.script:
            if self.closed:
                import websockets
                from websockets.frames import Close
                frame = Close(1000, "closed")
                raise websockets.ConnectionClosedOK(frame, frame, True)
            await asyncio.sleep(0.005)
        return self.script.pop(0)

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


AUDIO = base64.b64encode(b"\x01\x00" * 4800).decode("ascii")   # 0.2 s at 24 kHz


def _reply(rid, *, status, reason=None, items=2, with_output=True):
    frames = [{"type": "response.created", "response": {"id": rid}}]
    for i in range(items):
        frames.append({"type": "response.output_item.added", "response_id": rid,
                       "item": {"id": f"item_{i}"}})
        for w in ("Let us walk ", "through this ", "step by step "):
            frames.append({"type": "response.output_audio_transcript.delta",
                           "response_id": rid, "delta": w})
            for _ in range(3):
                frames.append({"type": "response.output_audio.delta",
                               "response_id": rid, "delta": AUDIO})
        frames.append({"type": "response.output_audio.done", "response_id": rid})
    response = {"id": rid, "status": status,
                "status_details": ({"type": status, "reason": reason}
                                   if reason else None),
                "usage": {"output_tokens": 380}}
    if with_output:
        response["output"] = [{"id": f"item_{i}"} for i in range(items)]
    frames.append({"type": "response.done", "response": response})
    return [json.dumps(f) for f in frames]


async def _dones(rt, limit=80):
    out = []
    agen = rt.events()
    try:
        while True:
            ev = await asyncio.wait_for(agen.__anext__(), 5)
            if ev["type"] == "response_done":
                out.append(ev)
                return out
            if len(out) > limit:
                return out
    finally:
        await agen.aclose()


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(realtime, "RECV_POLL_S", 0.02)
    monkeypatch.setattr(realtime, "AUDIO_RETRY_QUIET_S", 0.05, raising=False)


@in_a_loop
async def test_a_capped_reply_is_flagged_and_never_retried():
    rt = RealtimeVoiceSession("x", model=GPT, voice="cedar", api_key="k")
    rt.ws = FakeGateway(_reply("resp_cap", status="incomplete",
                               reason="max_output_tokens"))
    rt._requested = True
    rt._response_active = True
    before = time.time()
    done = (await _dones(rt))[-1]
    assert done["cap_truncated"] is True
    assert done["response_status"] == "incomplete"
    assert done["status_reason"] == "max_output_tokens"
    assert done["output_items"] == 2
    assert done["output_tokens"] == 380
    # Record-only: not a retry reason, and nothing was asked of the gateway.
    assert not done.get("retry_reason") and not done.get("retryable")
    assert rt.ws.sent == [], rt.ws.sent
    assert rt.first_audio_at is not None and rt.first_audio_at >= before


@in_a_loop
async def test_a_completed_reply_is_not_flagged_and_items_are_counted_without_output():
    rt = RealtimeVoiceSession("x", model=GPT, voice="cedar", api_key="k")
    rt.ws = FakeGateway(_reply("resp_ok", status="completed", items=3,
                               with_output=False))
    done = (await _dones(rt))[-1]
    assert done["cap_truncated"] is False
    assert done["response_status"] == "completed"
    assert done["output_items"] == 3, "counted from output_item.added"


@in_a_loop
async def test_a_route_that_names_no_items_reports_none_not_zero():
    """Measured on the local S4A sim (native-audio): no output_item frames and
    no `output` list. 0 would read as an empty reply; it is unknown."""
    rt = RealtimeVoiceSession("x", model=NATIVE, voice="Puck", api_key="k")
    frames = [f for f in _reply("resp_g", status="completed", items=1,
                                with_output=False)
              if json.loads(f)["type"] != "response.output_item.added"]
    rt.ws = FakeGateway(frames)
    done = (await _dones(rt))[-1]
    assert done["output_items"] is None
    assert done["cap_truncated"] is False


def test_a_held_verdict_keeps_the_reply_end_fields():
    rt = RealtimeVoiceSession("x", model=GPT, voice="cedar", api_key="k")
    now = time.time()
    rt._deferred = {"verdict": {"retry_reason": "truncated", "retryable": True},
                    "retried": False, "unterminated": True, "since": now,
                    "quiet_until": now - 1, "why": "cancelled",
                    "info": {"cap_truncated": True, "output_items": 2}}
    ev = rt._deferral_verdict()
    assert ev["cap_truncated"] is True and ev["output_items"] == 2


def test_cap_truncated_is_not_a_retry_reason_anywhere():
    """The plan's one hard rule for this flag, read off the source: the
    verdict function never sees it."""
    src = (ROOT / "server" / "voice" / "realtime.py").read_text(encoding="utf-8")
    body = src[src.index("    def _retry_verdict"):]
    body = body[:body.index("\n    async def ")]
    assert "cap_truncated" not in body and "_done_info" not in body


# --------------------------------------------------------------------------
# runner fakes (the shapes tests/test_audio_recovery.py uses)
# --------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.events = []
        self.started_at = time.time()

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def append_assistant_audio(self, pcm, agent_id=None):
        pass

    def append_user_audio(self, pcm):
        pass

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return "SYSTEM"


class FakeSession:
    def __init__(self, scenario_id):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = None
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []

    def append_user(self, text):
        self.shared_history.append({"speaker": "user", "text": text})

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        pass

    async def auto_steer(self, *, delivered=None):
        return None


class FakeWS:
    def __init__(self, headers=None):
        self.json = []
        self.binary = []
        self.headers = headers or {}

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append(payload)

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


class FakeRT:
    def __init__(self):
        self.ws = object()
        self.voice = "Puck"
        self.model = "fake"
        self.pending_input = 0
        self.autofire_active = False
        self.retries = 0
        self.first_audio_at = None
        self._q: asyncio.Queue = asyncio.Queue()

    @property
    def responding(self):
        return False

    async def retry_response(self, nudge=None):
        self.retries += 1
        return True

    async def cancel_response(self):
        pass

    def feed(self, ev):
        self._q.put_nowait(ev)

    def end(self):
        self._q.put_nowait(None)

    async def events(self):
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev


class FakeRoom:
    def __init__(self, speaking=None):
        self.speaking = speaking

    async def hear(self, pcm, exclude=None):
        pass

    def session_for(self, agent_id):
        return None


CL_AUDIO = b"\x01\x02" * 3200          # 0.2 s at the client's 16 kHz

CAPPED_DONE = {"type": "response_done", "audio_unterminated": False,
               "retried": False, "retry_reason": None, "retryable": False,
               "cap_truncated": True, "response_status": "incomplete",
               "status_reason": "max_output_tokens", "output_items": 2,
               "output_tokens": 380}


async def settle(runner):
    for _ in range(30):
        await asyncio.sleep(0)
    if runner._finalize_tasks:
        await asyncio.wait(list(runner._finalize_tasks), timeout=5)


def one_to_one(monkeypatch, headers=None):
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    session = FakeSession("S1A")
    ws = FakeWS(headers)
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    rt = FakeRT()
    runner.rt = rt
    runner.room = None
    return runner, session, ws, rt


@in_a_loop
async def test_the_1to1_turn_records_the_cap_and_is_not_re_spoken(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    rt.feed({"type": "agent_transcript_delta", "text": "The date is safe because the scope is"})
    for _ in range(10):
        rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed(dict(CAPPED_DONE))
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1
    assert turns[0]["cap_truncated"] is True
    assert turns[0]["status_reason"] == "max_output_tokens"
    assert turns[0]["output_items"] == 2
    assert session.store.of("steering_pair")[0]["actor"]["cap_truncated"] is True
    assert rt.retries == 0 and ws.frames("assistant_retry") == []
    assert session.store.of("audio_retry") == []


@in_a_loop
async def test_a_barged_turn_has_no_reply_end_to_report(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt, ["Well I"],
                                None, interrupted=True)
    turn = session.store.of("assistant_turn")[0]
    assert turn["cap_truncated"] is False and turn["response_status"] is None


@in_a_loop
async def test_the_room_turn_records_the_cap_and_is_not_re_spoken(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    session = FakeSession("S4A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    agent = runner._resolve_agents()[0]
    runner.room = FakeRoom(speaking=agent.id)
    rt = FakeRT()
    rt.feed({"type": "agent_transcript_delta", "text": "We ship Friday and"})
    for _ in range(6):
        rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed(dict(CAPPED_DONE))
    rt.end()
    await runner._pump_member(agent, rt)
    await settle(runner)
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1 and turns[0]["cap_truncated"] is True
    assert turns[0]["output_items"] == 2
    assert session.store.of("steering_pair")[0]["actor"]["cap_truncated"] is True
    assert rt.retries == 0


# --------------------------------------------------------------------------
# 3. The turn clock
# --------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.started_at = time.time()
        self.events = []

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


def test_one_turn_timing_per_agent_turn_with_every_stage_in_order():
    store = Store()
    tt = TurnTimer(store)
    tt.speech_end()
    tt.commit_sent()
    tt.transcript_arrived()
    tt.director_decided()
    tt.grant_sent("dan")
    gateway_at = time.time()
    seq = tt.started("dan")
    tt.audio_to_client(gateway_at)
    tt.audio_to_client(time.time() + 99)          # only the first chunk counts
    tt.done("dan")
    tt.ack({"phase": "start", "turn": seq, "lag_s": 0.0, "output_latency_s": 0.02})
    assert store.of("turn_timing") == [], "written at play_end, not before"
    tt.ack({"phase": "end", "turn": seq, "lag_s": 0.0})
    (ev,) = store.of("turn_timing")
    order = ["vad_speech_end", "commit_sent", "transcript_arrived",
             "director_decided", "grant_sent", "first_audio_from_gateway",
             "first_audio_to_client", "assistant_done", "first_audio_played",
             "play_end"]
    values = [ev[k] for k in order]
    assert all(isinstance(v, float) for v in values), ev
    assert values == sorted(values), ev
    assert ev["turn"] == seq and ev["agent_id"] == "dan" and ev["reply_index"] == 0
    assert ev["play_end_interrupted"] is False and ev["written_at"] == "play_end"
    assert ev["output_latency_s"] == 0.02
    # The per-turn playback events, on their own.
    assert [e["turn"] for e in store.of("play_start")] == [seq]
    assert [e["turn"] for e in store.of("play_end")] == [seq]


def test_an_ack_is_corrected_by_the_lag_the_page_reports():
    store = Store()
    tt = TurnTimer(store)
    seq = tt.started("a")
    now = time.time() - store.started_at
    tt.ack({"phase": "start", "turn": seq, "lag_s": 2.5})
    played = store.of("play_start")[0]["at"]
    assert now - 2.6 < played < now - 2.4
    # A lag no page can honestly have is clamped, not believed.
    tt.ack({"phase": "end", "turn": seq, "lag_s": 1e9})
    assert store.of("play_end")[0]["lag_s"] == 60.0


def test_follow_ups_share_the_participant_stages_and_are_numbered():
    store = Store()
    tt = TurnTimer(store)
    tt.speech_end()
    tt.grant_sent("dan")
    a = tt.started("dan")
    tt.done("dan")
    tt.grant_sent("chris")
    b = tt.started("chris")
    tt.done("chris")
    tt.flush()
    evs = {e["turn"]: e for e in store.of("turn_timing")}
    assert evs[a]["reply_index"] == 0 and evs[b]["reply_index"] == 1
    assert evs[a]["vad_speech_end"] == evs[b]["vad_speech_end"]
    assert evs[a]["grant_sent"] is not None and evs[b]["grant_sent"] is not None
    assert evs[a]["grant_sent"] <= evs[b]["grant_sent"]
    assert all(e["written_at"] == "encounter_end" for e in evs.values())
    assert all(e["first_audio_played"] is None for e in evs.values())


def test_an_opener_before_any_speech_has_no_participant_stages():
    store = Store()
    tt = TurnTimer(store)
    tt.started("dan")
    tt.flush()
    ev = store.of("turn_timing")[0]
    assert ev["vad_speech_end"] is None and ev["reply_index"] is None


def test_a_late_transcript_goes_to_the_commit_it_came_from():
    store = Store()
    tt = TurnTimer(store)
    tt.speech_end()
    tt.commit_sent()
    first = tt._pt
    tt.speech_end()          # the participant is talking again already
    tt.transcript_arrived()  # ...and the first turn's transcript lands now
    assert "transcript_arrived" in first.stages
    assert "transcript_arrived" not in tt._pt.stages


def test_the_open_set_is_bounded():
    store = Store()
    tt = TurnTimer(store)
    for _ in range(MAX_OPEN + 3):
        tt.started("a")
        tt.done("a")
    evicted = store.of("turn_timing")
    assert len(evicted) == 3 and all(e["written_at"] == "evicted" for e in evicted)
    assert len(tt._open) == MAX_OPEN


@pytest.mark.parametrize("bad", [
    {"phase": "start"}, {"phase": "start", "turn": "7"},
    {"phase": "start", "turn": True}, {"phase": "sideways", "turn": 1},
    {"phase": "start", "turn": 1, "lag_s": "soon"},
])
def test_a_malformed_ack_is_dropped_or_sanitised_and_never_raises(bad):
    store = Store()
    tt = TurnTimer(store)
    tt.started("a")
    tt.ack(bad)
    for e in store.events:
        assert isinstance(e.get("turn"), int)
        assert isinstance(e.get("lag_s"), float)


def test_a_broken_store_never_reaches_the_caller():
    class Boom:
        started_at = 0.0

        def event(self, *a, **k):
            raise RuntimeError("disk full")

    tt = TurnTimer(Boom())
    seq = tt.started("a")
    tt.ack({"phase": "end", "turn": seq})
    tt.flush()
    NullTimer().anything("at", all=True)


@in_a_loop
async def test_the_runner_numbers_turns_and_times_them_from_the_frames_it_sends(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    runner._timing.speech_end()
    runner._timing.commit_sent()
    rt.first_audio_at = time.time()
    rt.feed({"type": "agent_transcript_delta", "text": "Okay."})
    rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed({"type": "response_done", "audio_unterminated": False,
             "retried": False, "retry_reason": None, "retryable": False})
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)
    started = ws.frames("assistant_started")
    assert len(started) == 1 and isinstance(started[0]["turn"], int)
    seq = started[0]["turn"]
    # The page's acks come back over the same socket as text frames.
    await runner._handle_client_command(json.dumps(
        {"type": "playback", "phase": "start", "turn": seq, "lag_s": 0.01}))
    await runner._handle_client_command(json.dumps(
        {"type": "playback", "phase": "end", "turn": seq, "lag_s": 0.0}))
    (ev,) = session.store.of("turn_timing")
    assert ev["agent_id"] == runner.agent_id
    for k in ("vad_speech_end", "commit_sent", "first_audio_from_gateway",
              "first_audio_to_client", "assistant_done", "first_audio_played",
              "play_end"):
        assert isinstance(ev[k], float), (k, ev)
    assert ev["first_audio_from_gateway"] <= ev["first_audio_to_client"]
    # What the participant hears is unchanged: same bytes, same frames.
    assert ws.binary == [CL_AUDIO, CL_AUDIO]
    assert [f["type"] for f in ws.json].count("assistant_done") == 1


@in_a_loop
async def test_the_microphone_report_is_whitelisted_and_names_the_browser(monkeypatch):
    runner, session, ws, rt = one_to_one(
        monkeypatch, headers={"user-agent": "Mozilla/5.0 (Macintosh) Safari/605"})
    await runner._handle_client_command(json.dumps({
        "type": "client_audio_settings",
        "settings": {"sampleRate": 48000, "channelCount": 1,
                     "echoCancellation": True, "noiseSuppression": True,
                     "autoGainControl": False, "deviceId": "abc123",
                     "groupId": "g1", "label": "Jane's AirPods",
                     "contextSampleRate": 48000, "baseLatency": 0.0053},
    }))
    (ev,) = session.store.of("client_audio_settings")
    assert ev["settings"]["sampleRate"] == 48000
    assert ev["settings"]["autoGainControl"] is False
    # A key the browser did not report is absent, not null: "not reported"
    # and "reported as nothing" are different facts about a device.
    assert "latency" not in ev["settings"] and "outputLatency" not in ev["settings"]
    blob = json.dumps(ev)
    for leaked in ("abc123", "g1", "AirPods", "deviceId", "groupId", "label"):
        assert leaked not in blob, leaked
    assert ev["user_agent"].startswith("Mozilla/5.0")
    # Bounded: a page cannot fill the trail with these.
    for _ in range(10):
        await runner._handle_client_command(json.dumps(
            {"type": "client_audio_settings", "settings": {}}))
    assert len(session.store.of("client_audio_settings")) == 4


# --------------------------------------------------------------------------
# 2 + 3. The page: the microphone report and the playback acks
# --------------------------------------------------------------------------

ACK_HARNESS = r"""'use strict';
const path = require('path');
const assert = require('assert');
const { bootV2, vm } = require(path.join(__dirname, 'stub.js'));

(async () => {
  const b = bootV2(process.argv[2], '?session=x');
  const set = (code) => vm.runInContext(code, b.ctx);
  const get = (code) => vm.runInContext(code, b.ctx);
  const frame = (m) => b.ctx.handleServerFrame({ data: JSON.stringify(m) });
  const sent = (type) => JSON.parse(get('JSON.stringify(__sent)')).filter(m => m.type === type);

  set(`
    __now = 0; __sent = []; __srcs = [];
    audioCtx = {
      get currentTime() { return __now; },
      sampleRate: 16000, baseLatency: 0.005, outputLatency: 0.02,
      state: 'running', destination: {},
      createBuffer(ch, len, rate) {
        return { duration: len / rate, length: len, sampleRate: rate,
                 copyToChannel() {}, getChannelData: () => new Float32Array(len) };
      },
      createBufferSource() {
        const s = { buffer: null, connect() {}, start(t) { this.at = t; }, stop() {}, onended: null };
        __srcs.push(s); return s;
      },
      close() {}, addEventListener() {},
    };
    playDest = { stream: {} };
    playEl = { pause() {}, srcObject: {}, paused: false, currentTime: 1 };
    playElUsable = true; playbackChecked = true;
    playbackTime = 0; started = true; sessionId = 's_test'; sessionMode = 'single';
    ws = { readyState: 1, send(m) { __sent.push(JSON.parse(m)); }, close() {} };
    mediaStream = {
      getAudioTracks: () => [{ getSettings: () => ({
        sampleRate: 48000, channelCount: 1, echoCancellation: true,
        noiseSuppression: true, autoGainControl: false,
        deviceId: 'SECRET-DEVICE', groupId: 'SECRET-GROUP', label: "Jane's AirPods" }) }],
      getTracks: () => [],
    };
  `);

  // ---- the microphone, once, without the device's identity
  set('reportClientAudio()');
  const rep = sent('client_audio_settings');
  assert.strictEqual(rep.length, 1);
  assert.strictEqual(rep[0].settings.sampleRate, 48000);
  assert.strictEqual(rep[0].settings.autoGainControl, false);
  assert.strictEqual(rep[0].settings.contextSampleRate, 16000);
  const blob = JSON.stringify(rep[0]);
  for (const s of ['SECRET-DEVICE', 'SECRET-GROUP', 'AirPods']) assert(!blob.includes(s), 'leaked ' + s);

  // ---- a whole turn: start ack after the audio clock reached it, end ack at the real end
  frame({ type: 'assistant_started', agent_id: 'a', agent_name: 'A', turn: 7 });
  set('for (let i = 0; i < 10; i++) playPcmChunk(new Int16Array(1600).buffer);');   // 1 s from 0.02
  assert.strictEqual(sent('playback').length, 0, 'acked before anything played');
  set('__now = 0.5');                  // the timer fires late, as a background tab's does
  await b.clock.advance(100);
  let pb = sent('playback');
  assert.strictEqual(pb.length, 1, JSON.stringify(pb));
  assert.strictEqual(pb[0].phase, 'start');
  assert.strictEqual(pb[0].turn, 7);
  assert(Math.abs(pb[0].lag_s - 0.48) < 1e-6, 'lag not reported: ' + pb[0].lag_s);
  assert.strictEqual(pb[0].output_latency_s, 0.02);
  frame({ type: 'assistant_done', agent_id: 'a' });
  assert.strictEqual(sent('playback').length, 1, 'ended while still playing');
  set('__now = 1.1; for (const s of __srcs.slice()) if (s.onended) s.onended();');
  pb = sent('playback');
  assert.strictEqual(pb.length, 2, JSON.stringify(pb));
  assert.strictEqual(pb[1].phase, 'end');
  assert.strictEqual(pb[1].interrupted, false);
  assert(Math.abs(pb[1].lag_s - 0.08) < 1e-6, 'end lag: ' + pb[1].lag_s);

  // ---- a turn the participant cut off mid-line
  set('__now = 2; __srcs = [];');
  frame({ type: 'assistant_started', agent_id: 'a', agent_name: 'A', turn: 8 });
  set('for (let i = 0; i < 10; i++) playPcmChunk(new Int16Array(1600).buffer);');
  set('__now = 2.3');
  await b.clock.advance(100);
  frame({ type: 'assistant_interrupted' });
  pb = sent('playback').filter(m => m.turn === 8);
  assert.deepStrictEqual(pb.map(m => m.phase), ['start', 'end'], JSON.stringify(pb));
  assert.strictEqual(pb[1].interrupted, true);

  // ---- a turn stopped before its first chunk was reached was never heard
  set('__now = 4; playbackTime = 4.5;');
  frame({ type: 'assistant_started', agent_id: 'a', agent_name: 'A', turn: 9 });
  set('playPcmChunk(new Int16Array(1600).buffer);');   // scheduled for 4.5
  frame({ type: 'assistant_interrupted' });
  set('__now = 5');
  await b.clock.advance(1000);
  pb = sent('playback').filter(m => m.turn === 9);
  assert.deepStrictEqual(pb.map(m => m.phase), ['end'], 'a start ack for audio that never played: ' + JSON.stringify(pb));
  assert.strictEqual(pb[0].interrupted, true);

  // ---- a server that numbers nothing gets nothing back
  set('__now = 6');
  const before = sent('playback').length;
  frame({ type: 'assistant_started', agent_id: 'a', agent_name: 'A' });
  set('playPcmChunk(new Int16Array(1600).buffer);');
  set('__now = 7');
  await b.clock.advance(500);
  frame({ type: 'assistant_done', agent_id: 'a' });
  set('for (const s of __srcs.slice()) if (s.onended) s.onended();');
  assert.strictEqual(sent('playback').length, before);

  console.log('ACKS OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def test_the_page_acks_playback_and_reports_its_microphone(tmp_path):
    from test_client_blockers import DOM_STUB      # the shared thin browser

    (tmp_path / "stub.js").write_text(DOM_STUB, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(ACK_HARNESS, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(V2)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ACKS OK" in proc.stdout


def test_the_page_reports_on_every_connection():
    src = V2.read_text(encoding="utf-8")
    assert "ws.addEventListener('open', reportClientAudio);" in src
