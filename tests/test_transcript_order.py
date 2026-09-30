"""Issue #50: the transcript in the order things were said and heard.

Both surfaces ordered lines by when they were LOGGED. A participant line is
logged when its transcript arrives, a second or more after they stop; a
character line is logged when it is closed (generation end plus transcript
settle), and put on the page when its audio starts. So on the S4A room
s_1790278989_77ee7e (pipeline 2026-09-24c):

* the record put the participant's line, said while Priya was speaking
  (transcript 203.39), under Dan's follow-up, closed at 202.23 and first
  heard at 208.27: a rater reads it as the answer to a line they had not
  heard;
* the page drew three participant lines under a character line granted for
  their previous turn whose audio began while the newest one was still being
  transcribed (speech end 153.38, the character's audio 154.35, transcript
  154.56).

Now the bridge dates each commit by the runner VAD's speech start for the
turn it closes (else by its first voiced frame), the runner writes that on
user_turn (`spoken_at`) and sends the page how long ago it was
(`spoken_ago_s`), steering_pair names the page turn its line played as
(`page_turn`), and encounter_record sorts by `spoken_at` / `heard_at`,
keeping `t`. The page puts a participant line above any character line whose
audio began after they began. The timings below are the 77ee7e and 09bcbb
ones; the words are made up.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import encounter_record  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.turn_timing import TurnTimer  # noqa: E402
from server.voice import realtime as R  # noqa: E402

from test_participant_turn_integrity import (  # noqa: E402
    GPT, LOUD, QUIET, WireWS, bridge, in_a_loop, next_transcript, runner_for,
    send_ms,
)
from test_turn_instrumentation import (  # noqa: E402
    CL_AUDIO, FakeRoom, FakeRT, FakeSession, FakeWS, Store, one_to_one, settle,
)

V2 = ROOT / "static" / "v2.html"


# --------------------------------------------------------------------------
# 1. The bridge: when the participant began what a commit holds
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_commit_is_dated_by_the_vads_speech_start_for_the_turn_it_closes():
    """S2A 09bcbb at 60-89 s: voice again 0.4 s after one commit, Morgan's
    14 s reply, then the participant's next line, all in one 27.7 s buffer.
    Its first voiced frame is before the reply they were answering; the VAD's
    speech start for the turn the commit closes is after it."""
    rt = bridge()
    await send_ms(rt, LOUD, 300)                    # soon after the last commit
    await send_ms(rt, QUIET, 14000)                 # Morgan's reply plays
    vad_began = time.time()
    rt.speech_began = lambda: vad_began
    await send_ms(rt, LOUD, 3000)
    await send_ms(rt, QUIET, 900)
    await rt.commit_input()
    tag = rt._commit_tags[-1]
    assert tag["spoken_at"] == vad_began
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="x", transcript="I would like a promotion.")
    ev = await next_transcript(rt)
    assert ev["spoken_at"] == vad_began
    assert rvs._turn_meta(ev)["spoken_at"] == vad_began


@in_a_loop
async def test_without_a_speech_start_since_the_last_commit_the_first_voiced_frame():
    """Counted back from the commit on the buffer's own audio: 400 ms of voice
    and 900 ms of quiet followed the first voiced frame."""
    rt = bridge()
    await rt.commit_input()
    stale = time.time() - 60.0                      # before that commit
    rt.speech_began = lambda: stale
    await send_ms(rt, QUIET, 300)
    await send_ms(rt, LOUD, 400)
    await send_ms(rt, QUIET, 900)
    await rt.commit_input()
    tag = rt._commit_tags[-1]
    assert tag["spoken_at"] == pytest.approx(tag["committed_at"] - 1.3, abs=1e-6)


@in_a_loop
async def test_the_preroll_a_restart_resends_is_dated_where_it_was_said():
    """restart_input appends the pre-roll in one instant; its voiced frames
    were said before the VAD confirmed the speech, and the count-back puts
    them there, not at the moment they were re-sent."""
    rt = bridge()
    await rt.restart_input(QUIET * 15 + LOUD * 15)     # 300 ms quiet, 300 ms voice
    await send_ms(rt, LOUD, 500)
    await send_ms(rt, QUIET, 900)
    await rt.commit_input()
    tag = rt._commit_tags[-1]
    assert tag["spoken_at"] == pytest.approx(tag["committed_at"] - 1.7, abs=1e-6)


@in_a_loop
async def test_nothing_voiced_or_nothing_counted_is_not_dated():
    rt = bridge()
    await send_ms(rt, QUIET, 600)
    await rt.commit_input()
    assert rt._commit_tags[-1]["spoken_at"] is None
    rt = bridge(bar=None)
    await send_ms(rt, LOUD, 600)
    await rt.commit_input()
    assert rt._commit_tags[-1]["spoken_at"] is None


@in_a_loop
async def test_the_runners_own_bridge_is_dated_by_its_vad(monkeypatch):
    """The whole 1:1 path on the gpt route: the page's frames through the
    runner's VAD, its turn end, and the commit it sends."""
    monkeypatch.setattr(R, "MODEL", GPT)
    frames = [QUIET] * 50 + [LOUD] * 40 + [QUIET] * 120
    runner, session, ws = runner_for("S2A", frames)
    rt = runner._new_session(instructions="x", voice="")
    rt.ws = WireWS()
    runner.rt = rt
    await runner._client_to_model()
    assert rt.commits == 1
    assert runner._speech_started_at > 0
    assert rt._commit_tags[-1]["spoken_at"] == runner._speech_started_at


# --------------------------------------------------------------------------
# 2. The runner: user_turn.spoken_at, the page's spoken_ago_s, page_turn
# --------------------------------------------------------------------------

def test_the_participant_line_carries_when_it_was_begun():
    runner, session, ws = runner_for("S2A")
    session.store.started_at = time.time() - 20.0
    began = session.store.started_at + 12.5             # 7.5 s ago
    asyncio.run(runner._record_user_turn(
        "Well, I think it does.", voiced_ms=1500, voiced_span_ms=1600,
        spoken_at=began))
    (turn,) = session.store.of("user_turn")
    assert turn["spoken_at"] == 12.5
    (frame,) = ws.frames("user_transcript")
    assert frame["spoken_ago_s"] == pytest.approx(7.5, abs=0.5)


def test_a_line_the_bridge_could_not_date_keeps_the_old_shape():
    runner, session, ws = runner_for("S2A")
    asyncio.run(runner._record_user_turn("Well, I think it does.",
                                         voiced_ms=1500, voiced_span_ms=1600))
    assert session.store.of("user_turn")[0]["spoken_at"] is None
    assert "spoken_ago_s" not in ws.frames("user_transcript")[0]


def test_turn_of_names_the_turn_the_next_done_closes():
    tt = TurnTimer(Store())
    assert tt.turn_of("dan") is None
    a = tt.started("dan")
    assert tt.turn_of("dan") == a
    b = tt.started("dan")              # a held reply announced a second time
    assert tt.turn_of("dan") == b
    tt.done("dan")
    assert tt.turn_of("dan") is None, "a line closed with nothing announced"


@in_a_loop
async def test_the_1to1_line_names_its_page_turn(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    rt.feed({"type": "agent_transcript_delta", "text": "Okay."})
    rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed({"type": "response_done", "audio_unterminated": False,
             "retried": False, "retry_reason": None, "retryable": False})
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)
    (started,) = ws.frames("assistant_started")
    (pair,) = session.store.of("steering_pair")
    assert pair["page_turn"] == started["turn"]


@in_a_loop
async def test_a_1to1_line_that_never_reached_the_page_names_none(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt, ["Well I"],
                                None, interrupted=True)
    assert session.store.of("steering_pair")[0]["page_turn"] is None


@in_a_loop
async def test_the_room_line_names_its_page_turn(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    session = FakeSession("S4A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    agent = runner._resolve_agents()[0]
    runner.room = FakeRoom(speaking=agent.id)
    rt = FakeRT()
    rt.feed({"type": "agent_transcript_delta", "text": "We ship Friday."})
    for _ in range(3):
        rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed({"type": "response_done", "audio_unterminated": False,
             "retried": False, "retry_reason": None, "retryable": False})
    rt.end()
    await runner._pump_member(agent, rt)
    await settle(runner)
    (started,) = ws.frames("assistant_started")
    (pair,) = session.store.of("steering_pair")
    assert pair["page_turn"] == started["turn"]


# --------------------------------------------------------------------------
# 3. The record: sorted by spoken_at / heard_at, `t` kept
# --------------------------------------------------------------------------

def _write(root: Path, events) -> Path:
    sdir = root / "s_1790000000_abcdef"
    sdir.mkdir(parents=True)
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return sdir


def _pair(t, agent_id, text, **extra):
    return {"t": t, "type": "steering_pair", "direction": None,
            "actor": {"agent_id": agent_id, "text": text}, **extra}


def _user(t, text, **extra):
    return {"t": t, "type": "user_turn", "text": text, **extra}


def _order(rec):
    return [(t["agent_id"] if t["role"] == "agent" else "you")
            for t in rec["transcript"]]


def test_a_line_begun_under_a_colleague_goes_above_the_follow_up_heard_after_it(tmp_path):
    """77ee7e at 190-214 s: Priya's line plays from 194.07; Dan's follow-up
    is closed at 202.23, held behind her and first heard at 208.27; the
    participant's line lands at 203.39. Here they began it under Priya, at
    199.5. Chris's line was queued behind Dan's and cut before it played."""
    sdir = _write(tmp_path, [
        {"t": 0.0, "type": "session_start"},
        {"t": 194.07, "type": "play_start", "turn": 21, "agent_id": "priya", "at": 194.07},
        _pair(197.15, "priya", "No. Last year it all went at once.", page_turn=21),
        _pair(202.226, "dan", "That is the model, not the date.", page_turn=22),
        _user(203.388, "I wonder if you could...", spoken_at=199.5),
        {"t": 208.272, "type": "play_start", "turn": 22, "agent_id": "dan", "at": 208.272},
        _pair(212.402, "chris", "Let us keep this tight.", page_turn=23),
        {"t": 214.49, "type": "turn_timing", "turn": 23, "agent_id": "chris",
         "first_audio_played": None, "first_audio_to_client": 208.268,
         "play_end": 214.49, "play_end_interrupted": True},
        _user(214.514, "I want to do.", spoken_at=213.4),
    ])
    rec = encounter_record.build(sdir)
    assert _order(rec) == ["priya", "you", "dan", "chris", "you"], rec["transcript"]
    by_text = {t["text"]: t for t in rec["transcript"]}
    # The logged time stays on every turn; the new fields sit beside it.
    assert by_text["I wonder if you could..."]["t"] == 203.388
    assert by_text["I wonder if you could..."]["spoken_at"] == 199.5
    assert by_text["That is the model, not the date."]["t"] == 202.226
    assert by_text["That is the model, not the date."]["heard_at"] == 208.272
    # Cut before it played: never heard, so it keeps its logged place.
    assert by_text["Let us keep this tight."]["heard_at"] is None


def test_a_line_the_page_played_after_they_began_goes_under_theirs(tmp_path):
    """77ee7e at 150-157 s: Dan's line, granted for their previous turn, starts
    playing at 154.35 while their next line (begun 151.5, turn end 153.38) is
    still being transcribed; it lands at 154.56."""
    sdir = _write(tmp_path, [
        {"t": 0.0, "type": "session_start"},
        _user(151.218, "That went well, I thought.", spoken_at=142.3),
        {"t": 154.351, "type": "play_start", "turn": 16, "agent_id": "dan", "at": 154.35},
        _user(154.558, "Very well deserved.", spoken_at=151.5),
        _pair(156.733, "dan", "Hold that. Last year does not help.", page_turn=16),
    ])
    assert _order(encounter_record.build(sdir)) == ["you", "you", "dan"]


def test_heard_at_is_the_ack_then_turn_timing_then_the_first_audio_sent(tmp_path):
    sdir = _write(tmp_path, [
        {"t": 0.0, "type": "session_start"},
        {"t": 4.0, "type": "play_start", "turn": 1, "at": 4.0},
        {"t": 9.0, "type": "turn_timing", "turn": 1, "first_audio_played": 4.0,
         "first_audio_to_client": 3.9, "play_end": 9.0},
        # The page's start ack lost, its turn_timing kept.
        {"t": 19.0, "type": "turn_timing", "turn": 2, "first_audio_played": 14.0,
         "first_audio_to_client": 13.9, "play_end": 19.0},
        # The page said nothing: written at encounter end.
        {"t": 90.0, "type": "turn_timing", "turn": 3, "first_audio_played": None,
         "first_audio_to_client": 23.9, "play_end": None},
        # Acked as ended with no start: cut before it played.
        {"t": 34.0, "type": "turn_timing", "turn": 4, "first_audio_played": None,
         "first_audio_to_client": 33.9, "play_end": 34.0},
        _pair(6.0, "a", "one", page_turn=1),
        _pair(16.0, "a", "two", page_turn=2),
        _pair(26.0, "a", "three", page_turn=3),
        _pair(36.0, "a", "four", page_turn=4),
        _pair(46.0, "a", "nothing on record", page_turn=5),
        _pair(56.0, "a", "never announced", page_turn=None),
    ])
    heard = [t["heard_at"] for t in encounter_record.build(sdir)["transcript"]]
    assert heard == [4.0, 14.0, 23.9, None, None, None]


def test_an_older_record_keeps_the_order_it_always_had(tmp_path):
    """No spoken_at, no page_turn: sorted by `t`, as before, even with the
    acks present to join on."""
    events = [
        {"t": 0.0, "type": "session_start"},
        {"t": 194.07, "type": "play_start", "turn": 21, "at": 194.07},
        _pair(197.15, "priya", "No. Last year it all went at once."),
        _pair(202.226, "dan", "That is the model, not the date."),
        _user(203.388, "I wonder if you could...", voiced_span_ms=11000),
        {"t": 208.272, "type": "play_start", "turn": 22, "at": 208.272},
    ]
    rec = encounter_record.build(_write(tmp_path, events))
    assert _order(rec) == ["priya", "dan", "you"]
    assert [t["t"] for t in rec["transcript"]] == [197.15, 202.226, 203.388]
    assert all(t.get("spoken_at") is None and t.get("heard_at") is None
               for t in rec["transcript"])


# --------------------------------------------------------------------------
# 4. The page: the participant's caption above a line begun after them
# --------------------------------------------------------------------------

PAGE_HARNESS = r"""'use strict';
const path = require('path');
const assert = require('assert');
const { bootV2, vm } = require(path.join(__dirname, 'stub.js'));
const PAGE = process.argv[2];

function page(mode, castList) {
  const b = bootV2(PAGE, '?scenario=X');
  const set = (code) => vm.runInContext(code, b.ctx);
  const frame = (m) => b.ctx.handleServerFrame({ data: JSON.stringify(m) });
  set(`
    __now = 0; __sent = [];
    audioCtx = {
      get currentTime() { return __now; },
      sampleRate: 16000, baseLatency: 0.005, outputLatency: 0.02,
      state: 'running', destination: {},
      createBuffer(ch, len, rate) {
        return { duration: len / rate, length: len, sampleRate: rate,
                 copyToChannel() {}, getChannelData: () => new Float32Array(len) };
      },
      createBufferSource() {
        return { buffer: null, connect() {}, start(t) { this.at = t; }, stop() {}, onended: null };
      },
      close() {}, addEventListener() {},
    };
    playDest = { stream: {} };
    playEl = { pause() {}, srcObject: {}, paused: false, currentTime: 1 };
    playElUsable = true; playbackChecked = true;
    playbackTime = 0; started = true;
    ws = { readyState: 1, send(m) { __sent.push(JSON.parse(m)); }, close() {} };
  `);
  // The thin DOM has no insertBefore; this is the browser's, for one parent.
  const tr = b.dom.$('transcript');
  tr.insertBefore = function (c, ref) {
    const at = this.children.indexOf(c);
    if (at >= 0) this.children.splice(at, 1);
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    return c;
  };
  frame({ type: 'session', session_id: 's_1', scenario: { title: 'T', mode: mode },
          cast: castList });
  // Let the audio clock run: tickSpeech is on requestAnimationFrame.
  const at = async (t) => { set(`__now = ${t}`); await b.clock.advance(50); };
  const say = (id, name, seq, secs, text) => {
    frame({ type: 'assistant_started', agent_id: id, agent_name: name, turn: seq });
    frame({ type: 'assistant_text_delta', agent_id: id, text: text });
    set(`for (let i = 0; i < ${secs * 10}; i++) playPcmChunk(new Int16Array(1600).buffer);`);
    frame({ type: 'assistant_done', agent_id: id });
  };
  const heard = (text, ago) => frame(Object.assign(
    { type: 'user_transcript', final: true, text: text },
    ago == null ? {} : { spoken_ago_s: ago }));
  const lines = () => tr.children.filter(c => /class="speaker/.test(c.innerHTML || ''))
    .map(c => /speaker self/.test(c.innerHTML) ? 'You: ' + c.querySelector('.text').textContent
                                              : c.innerHTML.match(/>([^<]+):<\/span>/)[1]);
  return { b, set, frame, at, say, heard, lines };
}

(async () => {
  // ---- a room: the 77ee7e shapes
  {
    const p = page('group', [{ id: 'priya', name: 'Priya' }, { id: 'dan', name: 'Dan' },
                             { id: 'chris', name: 'Chris' }]);
    p.say('priya', 'Priya', 1, 3, 'Last year it all went at once.');   // plays 0.02 .. 3.02
    await p.at(0.3);
    // Dan's follow-up, granted for their previous turn, queued behind her.
    await p.at(1.0);
    p.say('dan', 'Dan', 2, 2, 'That is the model, not the date.');     // plays 3.02 .. 5.02
    // They begin at 2.0, under Priya; Dan's audio starts at 3.02, and their
    // transcript lands at 3.6.
    await p.at(3.5);
    assert.deepStrictEqual(p.lines(), ['Priya', 'Dan']);
    p.heard('I wonder if you could...', 1.6);
    assert.deepStrictEqual(p.lines(), ['Priya', 'You: I wonder if you could...', 'Dan'],
      'drawn under a line begun after they began: ' + JSON.stringify(p.lines()));

    // A frame with no date (an older server) is appended, as it always was.
    await p.at(6.0);
    p.heard('Okay.');
    // A line begun after the character's audio started stays under it.
    await p.at(7.0);
    p.say('chris', 'Chris', 3, 1, 'Let us keep this tight.');          // plays 7.02 .. 8.02
    await p.at(8.5);
    p.heard('Fine by me.', 1.0);                                        // began 7.5
    // Never above another line of theirs: began at 1.0, it steps over Dan's
    // newest line and stops at their own.
    await p.at(9.0);
    p.say('dan', 'Dan', 4, 1, 'So the date stands.');                   // plays 9.02 .. 10.02
    await p.at(10.5);
    p.heard('Wait, one more thing.', 9.5);
    assert.deepStrictEqual(p.lines(), [
      'Priya', 'You: I wonder if you could...', 'Dan', 'You: Okay.', 'Chris',
      'You: Fine by me.', 'You: Wait, one more thing.', 'Dan'], JSON.stringify(p.lines()));
  }

  // ---- 1:1: the same rule
  {
    const p = page('single', [{ id: 'morgan', name: 'Morgan' }]);
    p.say('morgan', 'Morgan', 1, 2, 'What are you asking for?');        // plays 0.02 .. 2.02
    await p.at(0.5);
    p.heard('A raise.', 0.4);                                           // began 0.1
    assert.deepStrictEqual(p.lines(), ['Morgan', 'You: A raise.'], JSON.stringify(p.lines()));
    await p.at(3.0);
    p.say('morgan', 'Morgan', 2, 2, 'Tell me more.');                   // plays 3.02 .. 5.02
    await p.at(3.4);
    p.heard('Or more leave.', 1.4);                                     // began 2.0
    assert.deepStrictEqual(p.lines(), ['Morgan', 'You: A raise.', 'You: Or more leave.', 'Morgan'],
      JSON.stringify(p.lines()));
  }
  console.log('TRANSCRIPT ORDER PAGE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def test_the_page_puts_a_line_above_one_begun_after_it(tmp_path):
    from test_client_blockers import DOM_STUB      # the shared thin browser

    (tmp_path / "stub.js").write_text(DOM_STUB, encoding="utf-8")
    h = tmp_path / "harness.js"
    h.write_text(PAGE_HARNESS, encoding="utf-8")
    proc = subprocess.run([_node(), str(h), str(V2)], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "TRANSCRIPT ORDER PAGE OK" in proc.stdout
