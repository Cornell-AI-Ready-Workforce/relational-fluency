"""Review fixes on the #21-#25 branch: pipeline 2026-09-23g, room pacing
2026-09-23d.

Each test below is one reviewer finding, reproduced offline and then held
fixed. The ids are the review's (conc = concurrency lens, rec = record lens,
reg = regression lens):

  * conc F1   a refused hold's latch swallowed the fresh reply's done
  * conc F2 / rec F5   _MemberState.announced outlived its turn
  * conc F3   a cancelled reply's kept tail answered the commit-only grant
  * conc F4   a barge-in in a retry's window discarded nothing
  * rec F1    the near-duplicate filter ran on gpt rooms (one transcriber)
  * rec F2 / reg R3   native-audio input follows its row too (24 kHz)
  * rec F3 / reg R2   record.json and the analysis DB carry the knob blocks
  * rec F4    turn_timing tied a reply to the newest participant turn
  * rec F6    an interrupted turn with no text of its own read as lost
  * reg R1    Gemini rooms routed one utterance behind after a late one
  * reg R4    the director rule for short turns is a knob

Offline throughout: the bridge sessions are real RealtimeVoiceSession objects
on a fake socket; nothing connects.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from server import encounter_record  # noqa: E402
from server import llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.turn_timing import TurnTimer  # noqa: E402
from server.voice import realtime as R  # noqa: E402

import test_participant_turn_integrity as TPI  # noqa: E402
from test_bridge_correctness import (  # noqa: E402
    CANCEL_POST, GPT, LOUD, NATIVE, FakeSession, PageWS, ReplayRT, adelta,
    bridge, created, done, in_a_loop, item_added, pull, settle, tdelta,
    tdone, types as evtypes, until,
)
from test_room_reply_lifecycle import (  # noqa: E402
    committed, cut_runner, gpt_room, quick,
)

quick = quick   # the autouse fixture, re-exported so it applies here


def _room_ns(speaking):
    room = types.SimpleNamespace(speaking=speaking)

    async def hear(pcm, exclude=None):
        pass
    room.hear = hear
    room.session_for = lambda aid: None
    return room


# --------------------------------------------------------------------------
# conc F1: the refused hold's latch and the fresh reply's done
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_fresh_reply_that_ends_empty_still_releases_the_floor():
    """The grant refused a cut-short hold and asked for a fresh reply, which
    came back with nothing but its done, before the cancelled reply's own done
    (which the gateway may never send). That done was swallowed as the end of
    the refused reply: no finalize, no empty_response, and the room waited
    out the whole group turn timeout."""
    session = FakeSession("S4A")
    runner = rvs.RealtimeVoiceSessionRunner(session, PageWS())
    agent = runner._resolve_agents()[0]
    room = _room_ns("somebody_else")
    runner.room = room
    gate = asyncio.Event()

    class RT(ReplayRT):
        async def events(self):
            yield {"type": "agent_transcript_delta", "text": "Then put",
                   "response_id": "resp_old"}
            await gate.wait()
            yield {"type": "response_done", "response_id": "resp_new"}
            await asyncio.sleep(0.2)

    pump = asyncio.ensure_future(runner._pump_member(agent, RT([])))
    await until(lambda: agent.id in runner._member_states
                and runner._member_states[agent.id].mode == "holding")
    assert await runner.adopt_member(agent.id) is False     # refused
    room.speaking = agent.id
    runner._response_done.clear()
    gate.set()
    await asyncio.wait_for(pump, 2)
    await settle(runner)
    assert runner._response_done.is_set(), "the floor was never released"
    assert session.store.of("empty_response")
    assert runner._member_states[agent.id].refused is False


@in_a_loop
async def test_the_refused_replys_own_done_is_still_swallowed():
    session = FakeSession("S4A")
    runner = rvs.RealtimeVoiceSessionRunner(session, PageWS())
    agent = runner._resolve_agents()[0]
    room = _room_ns("somebody_else")
    runner.room = room
    gate = asyncio.Event()

    class RT(ReplayRT):
        async def events(self):
            yield {"type": "agent_transcript_delta", "text": "Then put",
                   "response_id": "resp_old"}
            await gate.wait()
            yield {"type": "response_done", "response_id": "resp_old"}
            await asyncio.sleep(0.2)

    pump = asyncio.ensure_future(runner._pump_member(agent, RT([])))
    await until(lambda: agent.id in runner._member_states
                and runner._member_states[agent.id].mode == "holding")
    assert await runner.adopt_member(agent.id) is False
    room.speaking = agent.id
    runner._response_done.clear()
    gate.set()
    await asyncio.wait_for(pump, 2)
    await settle(runner)
    # Not the fresh reply's end: the floor stays with the reply still coming.
    assert not runner._response_done.is_set()
    assert not session.store.of("assistant_turn")


@in_a_loop
async def test_the_bridges_response_done_names_its_reply():
    rt = bridge()
    await rt.request_response()
    rt.ws.feed([created("resp_Y"), item_added("resp_Y", "i1"),
                tdelta("resp_Y", "i1", "Fine."), adelta("resp_Y", "i1"),
                tdone("resp_Y", "i1", "Fine."), done("resp_Y")])
    agen = rt.events()
    try:
        evs = await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()
    assert evs[-1]["response_id"] == "resp_Y"


# --------------------------------------------------------------------------
# conc F2 / rec F5: the member state's `announced` ends with its turn
# --------------------------------------------------------------------------

@in_a_loop
async def test_an_adopted_streaming_hold_does_not_leave_the_member_announced():
    """A streaming hold adopted through _flush_held sets announced on the
    member state, and nothing took it down: every later barge-in on that
    member before its next reply reached the page read the previous turn's
    clock as the line cut off ("heard 1.9 of 1.9")."""
    session = FakeSession("S4A")
    runner = rvs.RealtimeVoiceSessionRunner(session, PageWS())
    runner._speech_started_at = time.time() - 5
    agent = runner._resolve_agents()[0]
    room = _room_ns("somebody_else")
    runner.room = room
    gate = asyncio.Event()

    class RT(ReplayRT):
        async def events(self):
            yield {"type": "agent_transcript_delta", "text": "We could",
                   "response_id": "resp_h"}
            yield {"type": "agent_audio", "pcm": b"\x00" * 3200,
                   "response_id": "resp_h"}
            await gate.wait()
            yield {"type": "agent_audio", "pcm": b"\x00" * 3200,
                   "response_id": "resp_h"}
            yield {"type": "agent_transcript", "text": "We could wait."}
            yield {"type": "response_done", "response_id": "resp_h"}

    rt = RT([])
    rt.model = NATIVE              # the cancel is inert: the hold is adopted
    pump = asyncio.ensure_future(runner._pump_member(agent, rt))
    await until(lambda: agent.id in runner._member_states
                and runner._member_states[agent.id].mode == "holding"
                and runner._member_states[agent.id].held_seconds() > 0)
    assert await runner.adopt_member(agent.id) is True
    st = runner._member_states[agent.id]
    assert st.announced is True           # _flush_held, mid-turn
    room.speaking = agent.id
    gate.set()
    await asyncio.wait_for(pump, 2)
    await settle(runner)
    assert session.store.of("assistant_turn")
    assert st.announced is False, "the adopted turn's mark outlived it"


@in_a_loop
async def test_a_barge_in_takes_the_holders_mark_down():
    runner, session = cut_runner(holder_announced=True)
    chris = runner._member_states["chris"]
    chris.announced = True
    chris.new_turn()
    now = time.time()
    chris.play_start, chris.play_end = now - 1, now + 2
    await runner._client_to_model()
    assert chris.announced is False


# --------------------------------------------------------------------------
# conc F3: a cancelled reply's tail is not the grant's answer
# --------------------------------------------------------------------------

async def _suppressed_then_granted(tail_after_commit, then=()):
    room, rt = gpt_room()

    async def drain():
        async for _ in rt.events():
            pass
    reader = asyncio.ensure_future(drain())
    rt.ws.feed([created("resp_X")])
    await asyncio.sleep(0.1)
    # The unsolicited reply is suppressed: cancelled, its tail KEPT.
    await rt.cancel_response(discard_tail=False)

    async def after_commit():
        await committed(rt)
        rt.ws.feed(tail_after_commit)
        await asyncio.sleep(0.1)
        rt.ws.feed(list(then))
    h = asyncio.ensure_future(after_commit())
    await asyncio.wait_for(room.give_floor("dan"), 5)
    await asyncio.wait([h], timeout=2)
    reader.cancel()
    # Bounded: a reader stuck in the bridge must fail the test, not hang it.
    await asyncio.wait([reader], timeout=2)
    return room, rt


@in_a_loop
async def test_a_suppressed_replys_tail_does_not_answer_the_commit(monkeypatch):
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.5")
    room, rt = await _suppressed_then_granted([adelta("resp_X", "ix")])
    assert room.last_grant["via"] == "commit+create_fallback", (
        "the cancelled reply's tail was read as the commit's reply")
    assert rt.ws.types().count("response.create") == 1


@in_a_loop
async def test_a_new_reply_behind_the_tail_does_answer_it(monkeypatch):
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "1.0")
    room, rt = await _suppressed_then_granted(
        [adelta("resp_X", "ix")], then=[created("resp_Y")])
    assert room.last_grant["via"] == "commit"
    assert "response.create" not in rt.ws.types()


# --------------------------------------------------------------------------
# conc F4: a barge-in in the retry's window
# --------------------------------------------------------------------------

async def _retry_then_barge(rt):
    await rt.request_response()
    rt.ws.feed([created("resp_A"), tdelta("resp_A", "a1", "Well we're")])
    agen = rt.events()
    await pull(agen, 1)
    assert await rt.retry_response() is True
    # The participant cuts in before the retry's response.created.
    assert rt._response_created_id is None
    await rt.cancel_response()
    return agen


@in_a_loop
async def test_the_retrys_reply_created_after_a_barge_in_is_discarded():
    rt = bridge()
    agen = await _retry_then_barge(rt)
    try:
        rt.ws.feed([created("resp_Z"), item_added("resp_Z", "z1"),
                    tdelta("resp_Z", "z1", "Thank you for"),
                    adelta("resp_Z", "z1"),
                    tdone("resp_Z", "z1", "Thank you for"),
                    done("resp_Z", "cancelled", "client_cancelled")])
        evs = await pull(agen, until=lambda e: e["type"] == "response_done")
        flags = (rt.autofire_active, rt._response_active,
                 rt._response_created_id)
    finally:
        await agen.aclose()
    assert "agent_audio" not in evtypes(evs), "the retry played after the cut"
    (co,) = [e for e in evs if e["type"] == "cancelled_output"]
    assert co["response_id"] == "resp_Z" and co["audio_deltas"] == 1
    assert co["transcripts"] == ["Thank you for"]
    assert evs[-1].get("stale") is True
    assert flags == (False, False, None), "the tail re-armed the reply flags"


@in_a_loop
async def test_the_abandoned_head_resuming_after_a_barge_in_is_discarded():
    rt = bridge()
    agen = await _retry_then_barge(rt)
    try:
        rt.ws.feed([adelta("resp_A", "a2"), done("resp_A")])
        evs = await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()
    assert "agent_audio" not in evtypes(evs)
    assert [e["response_id"] for e in evs if e["type"] == "cancelled_output"
            ] == ["resp_A"]


@in_a_loop
async def test_the_next_turns_reply_is_not_discarded():
    rt = bridge()
    agen = await _retry_then_barge(rt)
    try:
        await rt.commit_input()            # the participant's own turn
        rt.ws.feed([created("resp_N"), item_added("resp_N", "n1"),
                    adelta("resp_N", "n1"), done("resp_N")])
        evs = await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()
    assert "agent_audio" in evtypes(evs)
    assert not evs[-1].get("stale")


# --------------------------------------------------------------------------
# rec F1: no near-duplicate filter where the scribe is the only transcriber
# --------------------------------------------------------------------------

def _gpt_room_runner():
    runner, session, _ = TPI.runner_for("S4A")
    runner.room = object()
    runner.rt = type("RT", (), {"model": GPT})()
    return runner, session


def test_a_gpt_room_keeps_a_participant_building_on_their_own_line():
    runner, session = _gpt_room_runner()
    for line in ("Priya?", "Priya, are you there?", "No.",
                 "No, I don't think so."):
        asyncio.run(runner._record_user_turn(line))
    assert not session.store.of("user_transcript_duplicate_dropped")
    assert len(session.store.of("user_turn")) == 4


def test_a_native_room_still_merges_two_transcribers_of_one_utterance():
    runner, session = TPI._room_runner()           # NATIVE
    asyncio.run(runner._record_user_turn("We're not ready to ship."))
    asyncio.run(runner._record_user_turn("we're not ready to ship"))
    assert session.store.of("user_transcript_duplicate_dropped")


def test_the_second_source_is_on_the_record():
    assert llm.provenance(GPT)["turn_gate"]["room_dedupe_second_source"] is False
    assert llm.provenance(NATIVE)["turn_gate"]["room_dedupe_second_source"] is True


# --------------------------------------------------------------------------
# rec F2 / reg R3: native-audio sessions built without a model send 24 kHz
# --------------------------------------------------------------------------

def test_the_runners_own_session_resamples_on_native_audio(monkeypatch):
    """Before 2026-09-23c the rate came from the constructor's `model`
    argument, which the runner and the room never pass, so native-audio
    sessions sent the browser's 16 kHz raw while provenance said 24000."""
    monkeypatch.setattr(R, "MODEL", NATIVE)
    runner, _, _ = TPI.runner_for("S2A")
    rt = runner._new_session(instructions="x", voice="")
    assert rt.input_rate == 24000
    rt.ws = TPI.WireWS()
    asyncio.run(rt.send_audio(TPI.QUIET * 5))             # 100 ms at 16 kHz
    assert abs(rt.pending_input - 4800) <= 2              # 100 ms at 24 kHz
    assert llm.provenance(NATIVE)["input_rate"] == 24000


# --------------------------------------------------------------------------
# rec F3 / reg R2: the knob blocks reach record.json and the analysis DB
# --------------------------------------------------------------------------

def _encounter_dir(tmp_path):
    sdir = tmp_path / "s_1_ab"
    sdir.mkdir()
    prov = llm.provenance(GPT)
    events = [{"t": 0.0, "type": "session_start"},
              {"t": 0.1, "type": "realtime_session_started", "model": GPT,
               **prov}]
    (sdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return sdir, prov


def test_the_record_carries_every_knob_block(tmp_path):
    sdir, prov = _encounter_dir(tmp_path)
    record = encounter_record.build(sdir)
    rp = record["provenance"]
    for key in ("turn_gate", "pacing", "record", "cancelled_output",
                "agent_transcript_items", "room_memory", "pipeline_version",
                "room_pacing_version"):
        assert rp[key] == prov[key], key


def test_an_older_record_has_none_for_the_blocks(tmp_path):
    sdir = tmp_path / "s_2_cd"
    sdir.mkdir()
    (sdir / "events.jsonl").write_text(
        json.dumps({"t": 0.0, "type": "session_start"}) + "\n",
        encoding="utf-8")
    rp = encounter_record.build(sdir)["provenance"]
    assert rp["turn_gate"] is None and rp["record"] is None


class _Cursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def fetchone(self):
        return ("teamwork",)


def test_the_analysis_db_encounter_row_carries_the_pipeline(tmp_path):
    ladb = pytest.importorskip("load_analysis_db")
    sdir, prov = _encounter_dir(tmp_path)
    record = encounter_record.build(sdir)
    record["scenario"] = "S4A"
    (sdir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    cur = _Cursor()
    ladb.load_encounter(cur, sdir, "s3://x/")
    (sql, params), = [(s, p) for s, p in cur.calls
                      if "INSERT INTO encounter (" in s]
    assert sql.count("%s") == len(params)
    assert prov["pipeline_version"] in params
    assert prov["room_pacing_version"] in params
    blob = next(p for p in params if hasattr(p, "obj"))
    assert blob.obj["turn_gate"] == prov["turn_gate"]
    assert blob.obj["cancelled_output"] == prov["cancelled_output"]


def test_an_old_analysis_db_gets_the_columns():
    ladb = pytest.importorskip("load_analysis_db")
    cur = _Cursor()
    ladb.ensure_pipeline_columns(cur)
    sql = " ".join(s for s, _ in cur.calls)
    for col in ("pipeline_version", "room_pacing_version",
                "pipeline_provenance"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql


# --------------------------------------------------------------------------
# rec F4: a reply belongs to the participant turn current at its grant
# --------------------------------------------------------------------------

class _Store:
    def __init__(self):
        self.started_at = time.time()
        self.events = []

    def event(self, type_, **f):
        self.events.append(dict(type=type_, **f))


def test_a_reply_granted_before_the_participant_resumed_is_timed_on_its_turn():
    store = _Store()
    timer = TurnTimer(store)
    timer.speech_end()                     # turn A ends
    timer.commit_sent()
    timer.grant_sent("dan")                # dan is granted on A
    a_end = timer._pt.stages["vad_speech_end"]
    time.sleep(0.02)
    timer.speech_end()                     # the participant resumes: turn B
    turn = timer.started("dan")            # dan's first audio arrives now
    timer.flush()
    (row,) = [e for e in store.events if e["type"] == "turn_timing"]
    assert row["turn"] == turn
    assert row["vad_speech_end"] == a_end, "timed against the later turn"
    assert row["grant_sent"] is not None and row["reply_index"] == 0


def test_a_later_reply_on_the_new_turn_is_its_own():
    store = _Store()
    timer = TurnTimer(store)
    timer.speech_end()
    timer.grant_sent("dan")
    timer.started("dan")
    timer.speech_end()
    b_end = timer._pt.stages["vad_speech_end"]
    timer.started("priya")                 # no grant: the current turn
    timer.flush()
    rows = [e for e in store.events if e["type"] == "turn_timing"]
    assert rows[1]["vad_speech_end"] == b_end and rows[1]["grant_sent"] is None


@in_a_loop
async def test_commit_turn_says_when_it_committed_nothing():
    rt = bridge()
    await rt.request_response()            # a reply is in flight
    assert await rt.commit_turn() is False
    rt.clear_response_state()
    assert await rt.commit_turn() is True


# --------------------------------------------------------------------------
# rec F6: an interrupted turn whose words are only in the dropped tail
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_barge_in_before_any_transcript_keeps_the_line():
    """The participant cut in after the first audio and before the first
    transcript delta. The tail's transcript .done used to fill the turn; the
    23d discard drops it, and the turn was written text="" and
    transcript_missing, "the character spoke and no transcript arrived"."""
    session = FakeSession("S2A")
    ws = PageWS([LOUD] * 25)
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    rt = bridge()
    runner.rt = rt
    runner.room = None
    await rt.request_response()
    rt.ws.feed([created("resp_X"), item_added("resp_X", "item_1"),
                adelta("resp_X", "item_1"), adelta("resp_X", "item_1")])
    pump = asyncio.ensure_future(runner._pump_events(rt))
    try:
        await until(lambda: len(ws.binary) == 2)
        await runner._client_to_model()
        rt.ws.feed(CANCEL_POST)
        await until(rt.ws.q.empty)
        await asyncio.sleep(0.3)
        await settle(runner)
    finally:
        pump.cancel()
    (turn,) = session.store.of("assistant_turn")
    assert turn["interrupted"] is True
    assert turn["text"] == "Okay, let's slow this down and"
    assert turn["transcript_missing"] is False
    assert turn.get("heard_text"), "no estimate of what was heard"
    assert not session.store.of("transcript_missing")
    (fill,) = session.store.of("interrupted_text_from_cancelled_output")
    assert fill["response_id"] == "resp_X"
    (drop,) = session.store.of("cancelled_output_dropped")
    assert drop["response_id"] == "resp_X"


# --------------------------------------------------------------------------
# reg R1: a Gemini room routes each turn on its own words
# --------------------------------------------------------------------------

def _gemini_turn(model, *, old_before_speech):
    async def go():
        runner, session, _ = TPI.runner_for("S4A")
        room = TPI._room(model, runner)
        for a in runner._resolve_agents():
            room.sessions[a.id] = TPI.DeadMember(model)
        runner.room = room
        runner.director = session.director
        routed_at = []
        inner = session.director.route

        async def route(history, text):
            routed_at.append(time.time())
            return await inner(history, text)
        session.director.route = route
        session.append_agent("dan", "The date is locked.")
        if old_before_speech:
            # A transcript that landed after its own turn gave up waiting.
            await runner._record_user_turn("Old words.")
            await asyncio.sleep(0.01)
        runner._speech_started_at = time.time()      # this turn's speech
        if not old_before_speech:
            # Transcribed while the participant was still talking.
            await runner._record_user_turn("Mid-speech words.")
        runner._turn_end_arrivals = runner._transcripts_arrived
        runner._turn_end_speech_began = runner._speech_started_at

        async def late():
            await asyncio.sleep(0.4)
            await runner._record_user_turn("New words.")
        if old_before_speech:
            asyncio.ensure_future(late())
        t0 = time.time()
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        return session, routed_at[0] - t0
    return asyncio.run(go())


@pytest.mark.parametrize("model", [TPI.NATIVE, TPI.GEMINI])
def test_a_stale_utterance_does_not_route_the_next_turn_on_it(model, monkeypatch):
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "3")
    session, _ = _gemini_turn(model, old_before_speech=True)
    (_, text) = session.director.calls[0]
    assert "New words." in text, "routed one utterance behind"


@pytest.mark.parametrize("model", [TPI.NATIVE, TPI.GEMINI])
def test_a_transcript_from_while_they_spoke_routes_at_once(model, monkeypatch):
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "3")
    session, elapsed = _gemini_turn(model, old_before_speech=False)
    (_, text) = session.director.calls[0]
    assert text == "Mid-speech words."
    assert elapsed < 1.0, "it waited for a transcript that had already come"


# --------------------------------------------------------------------------
# reg R4: which short turns the director reads is a knob
# --------------------------------------------------------------------------

def _short_answer():
    runner, session, _ = TPI.runner_for("S4A")
    asyncio.run(runner._record_user_turn("No.", voiced_ms=350))
    return runner, session


def test_by_default_a_short_unnamed_answer_stays_out_of_the_director():
    runner, session = _short_answer()
    assert runner._unrouted_user_texts == []
    (turn,) = session.store.of("user_turn")
    assert turn["low_confidence"] is True


def test_the_director_can_be_given_every_short_turn(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_LOW_CONFIDENCE_DIRECTOR", "all")
    from server import director as director_mod
    from server import steering as steering_mod
    from server.session import Session
    runner, session = _short_answer()
    assert runner._unrouted_user_texts == ["No."]
    # The real Session's entry: flagged, still out of steering, but in the
    # director's transcript.
    real = types.SimpleNamespace(
        shared_history=[], store=types.SimpleNamespace(started_at=time.time()))
    Session.append_user(real, "No.", low_confidence=True, names_cast=False,
                        to_director=True)
    (entry,) = real.shared_history
    assert entry["low_confidence"] is True and entry["to_director"] is True
    assert "No." in director_mod._format_transcript(real.shared_history, {})
    assert "No." not in steering_mod._format_transcript(real.shared_history, {})
    assert llm.provenance(GPT)["turn_gate"][
        "participant_low_confidence_director"] == "all"


def test_by_default_the_real_session_keeps_the_old_rule():
    from server import director as director_mod
    from server.session import Session
    real = types.SimpleNamespace(
        shared_history=[], store=types.SimpleNamespace(started_at=time.time()))
    Session.append_user(real, "No.", low_confidence=True, names_cast=False)
    assert "No." not in director_mod._format_transcript(real.shared_history, {})
