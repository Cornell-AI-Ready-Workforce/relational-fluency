"""The turn cue (issue #49; pipeline and room pacing 2026-09-28a).

The researchers' decision of 2026-09-28: a clear, neutral sign of when the
participant can speak, the same wording and behaviour in 1:1 and in the S3 and
S4 rooms: "<Name> is speaking" while a character's audio plays, "You can speak
now" once the last queued line has finished playing and nothing else is queued
or being generated, "Listening..." while the participant speaks. "You speak
first" goes from group mode and so does the 4-second "Your turn" ->
"Listening..." switch. Room pacing itself is unchanged.

What it replaces: the pill said "Your turn" at assistant_done (generation
end), 7-17 s (median 13 s) before the character stopped talking in e80fca and
more than 1 s early on 24 of 28 turns in 77ee7e; name pills came up to 8.5 s
before that character's voice; and a room said "You speak first" although the
lead opens every group scene.

The server owns the one part the page cannot know alone (nothing else queued
or being generated) and says it with a turn_open frame, sent only after the
page's own play_end ack for the last line. The page owns the rest from its
audio clock.
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

import test_lost_participant_round as harness  # noqa: E402

V2 = ROOT / "static" / "v2.html"


def _one_to_one():
    runner, session, ws = harness.make_runner("S2A")
    runner.rt = harness.FakeRT()
    return runner, session, ws


async def _line(runner, *, audio=True, agent="morgan", name="Morgan"):
    """One character line as the runner sends it: started, audio, done.
    Returns the turn number the page's acks name."""
    await runner._send({"type": "assistant_started", "agent_id": agent,
                        "agent_name": name})
    seq = runner._cue_seq
    if audio:
        await runner._send_bytes(b"\x00\x01" * 1600)
        # The server's model of playback: this line has just finished.
        runner._play_cursor = time.time()
    await runner._send({"type": "assistant_done", "agent_id": agent})
    return seq


async def _ack_end(runner, seq, interrupted=False):
    await runner._handle_client_command(json.dumps(
        {"type": "playback", "phase": "end", "turn": seq, "lag_s": 0,
         "interrupted": interrupted}))


# --------------------------------------------------------------------------
# The server: turn_open after the real end of playback, and only then
# --------------------------------------------------------------------------

def test_the_floor_opens_at_the_pages_play_end_and_not_at_generation_end():
    async def go():
        runner, session, ws = _one_to_one()
        seq = await _line(runner)
        assert not ws.frames("turn_open"), "open at generation end (the old 'Your turn')"
        await runner._maybe_turn_open()                  # the watchdog's tick
        assert not ws.frames("turn_open"), "open before the page said it finished"
        await _ack_end(runner, seq)
        assert len(ws.frames("turn_open")) == 1
        await _ack_end(runner, seq)
        await runner._maybe_turn_open()
        assert len(ws.frames("turn_open")) == 1, "said once per opening"
        return session, ws

    session, ws = _run(go())
    assert session.store.of("turn_open"), "on the record, to read against turn_timing"
    order = [f["type"] for f in ws.json]
    assert order.index("assistant_done") < order.index("turn_open")


def test_a_line_that_played_nothing_opens_the_floor_at_its_end():
    async def go():
        runner, session, ws = _one_to_one()
        await _line(runner, audio=False)
        return ws

    assert len(_run(go()).frames("turn_open")) == 1


@pytest.mark.parametrize("busy", [
    "participant_speaking", "participant_turn_closing", "turn_end_pending",
    "reply_in_flight", "speaking", "held_call_reply", "transition",
])
def test_nothing_opens_while_anything_else_is_under_way(busy):
    async def go():
        runner, session, ws = _one_to_one()
        seq = await _line(runner)
        if busy == "participant_speaking":
            runner.vad.speaking = True
        elif busy == "participant_turn_closing":
            runner._participant_turn_closing = True
        elif busy == "turn_end_pending":
            runner._turn_end_pending_ms = 600.0
        elif busy == "reply_in_flight":
            runner.rt._responding = True
        elif busy == "speaking":
            runner._speaking = True
        elif busy == "held_call_reply":
            runner._held_call_tasks.add(asyncio.ensure_future(asyncio.sleep(5)))
        elif busy == "transition":
            runner._advancing = True
        await _ack_end(runner, seq)
        await runner._maybe_turn_open()
        return ws

    assert not _run(go()).frames("turn_open")


def test_the_participant_speaking_closes_it_and_a_turn_that_came_to_nothing_reopens_it():
    async def go():
        runner, session, ws = _one_to_one()
        await runner._maybe_turn_open()
        assert len(ws.frames("turn_open")) == 1
        await runner._send({"type": "speech_started"})
        runner.vad.speaking = True
        await runner._maybe_turn_open()
        assert len(ws.frames("turn_open")) == 1
        runner.vad.speaking = False                      # a cough: no reply came
        await runner._maybe_turn_open()
        assert len(ws.frames("turn_open")) == 2
        return ws

    _run(go())


def test_a_page_that_never_acks_does_not_hold_the_floor_shut():
    """The line is taken as played CUE_ACK_GRACE_S after ITS OWN end."""
    async def go():
        runner, session, ws = _one_to_one()
        runner.CUE_ACK_GRACE_S = 0.2
        await _line(runner)
        await runner._maybe_turn_open()
        assert not ws.frames("turn_open")
        await asyncio.sleep(0.3)
        await runner._maybe_turn_open()
        return ws

    assert len(_run(go()).frames("turn_open")) == 1


async def _overlap(runner):
    """S4A's held-reply adoption (turns 11/12 Chris, 14/15 Priya, 20/21 Dan in
    the native run of 2026-09-28): a second assistant_started 0.1 s after the
    first, before the first line's assistant_done. The page makes the second
    its currentTurn, so the first is never done there and never acked."""
    await runner._send({"type": "assistant_started", "agent_id": "chris",
                        "agent_name": "Chris"})
    first = runner._cue_seq
    await runner._send_bytes(b"\x00\x01" * 1600)
    await runner._send({"type": "assistant_started", "agent_id": "dan",
                        "agent_name": "Dan"})
    second = runner._cue_seq
    await runner._send_bytes(b"\x00\x01" * 1600)
    runner._play_cursor = time.time()
    await runner._send({"type": "assistant_done", "agent_id": "chris"})
    await runner._send({"type": "assistant_done", "agent_id": "dan"})
    return first, second


def test_a_line_the_page_never_acked_does_not_delay_every_later_opening():
    """Review of 850b08e: one un-acked line stayed in _cue_turns {audio, done}
    for the rest of the encounter and, against the GLOBAL cursor, held every
    later turn_open until 2 s past the line playing then (15 of 15 openings
    2.0-3.4 s late in s_1790638741_2e022c)."""
    async def go():
        runner, session, ws = _room_runner()
        first, second = await _overlap(runner)
        await _ack_end(runner, second)
        assert first not in runner._cue_turns, (
            "an end for a later line is an end for every earlier one")
        opened = len(ws.frames("turn_open"))
        # A whole new, normal line, acked the moment it ends.
        runner._cue_open = False
        seq = await _line(runner, agent="dan", name="Dan")
        await _ack_end(runner, seq)
        assert len(ws.frames("turn_open")) == opened + 1, (
            f"held back: {runner._turn_cue_blocker()} {runner._cue_turns}")
        return ws

    _run(go())


def test_an_orphan_nothing_later_acks_expires_after_its_own_end():
    async def go():
        runner, session, ws = _room_runner()
        runner.CUE_ACK_GRACE_S = 0.2
        first, second = await _overlap(runner)
        # The second line played nothing the page acked either (it went
        # quiet); a new line is under way and keeps the playback clock ahead.
        await asyncio.sleep(0.3)
        runner._play_cursor = time.time() - 0.01
        assert runner._turn_cue_blocker() is None, (
            f"{runner._turn_cue_blocker()}: waiting on the global cursor, not "
            f"the line's own end")
        return ws

    _run(go())


def test_a_characters_new_line_closes_its_older_one():
    """A line that never gets its assistant_done must not read as 'being
    generated' for CUE_STALE_S once the same character has started another."""
    async def go():
        runner, session, ws = _room_runner()
        await runner._send({"type": "assistant_started", "agent_id": "chris",
                            "agent_name": "Chris"})
        first = runner._cue_seq
        await runner._send_bytes(b"\x00\x01" * 1600)
        await runner._send({"type": "assistant_started", "agent_id": "chris",
                            "agent_name": "Chris"})
        assert runner._cue_turns[first]["done"] is True, "still 'generating'"
        # Its playback is still waited for, until a later end covers it.
        assert runner._turn_cue_blocker() is not None
        second = runner._cue_seq
        await runner._send_bytes(b"\x00\x01" * 1600)
        runner._play_cursor = time.time()
        await runner._send({"type": "assistant_done", "agent_id": "chris"})
        assert runner._turn_cue_blocker() == "playing"
        await _ack_end(runner, second)
        return ws

    assert len(_run(go()).frames("turn_open")) == 1


def test_a_cut_off_line_waits_for_its_interrupted_ack():
    async def go():
        runner, session, ws = _one_to_one()
        await runner._send({"type": "assistant_started", "agent_id": "morgan",
                            "agent_name": "Morgan"})
        seq = runner._cue_seq
        await runner._send_bytes(b"\x00\x01" * 1600)
        runner._play_cursor = time.time()
        await runner._send({"type": "assistant_interrupted"})
        assert not ws.frames("turn_open")
        await _ack_end(runner, seq, interrupted=True)
        return ws

    assert len(_run(go()).frames("turn_open")) == 1


def test_the_model_of_playback_holds_it_until_the_audio_would_have_ended():
    async def go():
        runner, session, ws = _one_to_one()
        seq = await _line(runner)
        runner._play_cursor = time.time() + 30          # still playing, per the server
        await _ack_end(runner, seq)
        return ws

    assert not _run(go()).frames("turn_open")


def _room_runner():
    runner, session, ws = harness.make_runner("S4A")

    class Room:
        speaking = None
        sessions = {"dan": harness.FakeRT(), "chris": harness.FakeRT()}
    runner.room = Room()
    return runner, session, ws


def test_a_room_opens_only_when_no_turn_is_being_served_and_the_last_line_played():
    async def go():
        runner, session, ws = _room_runner()
        seq = await _line(runner, agent="dan", name="Dan")
        # The director has routed a follow-up: the floor is held for it.
        async with runner._floor:
            await _ack_end(runner, seq)
            assert not ws.frames("turn_open"), "open while a follow-up is on its way"
        runner.room.speaking = "chris"
        await runner._maybe_turn_open()
        assert not ws.frames("turn_open"), "open while a member holds the floor"
        runner.room.speaking = None
        runner.room.sessions["chris"]._responding = True
        await runner._maybe_turn_open()
        assert not ws.frames("turn_open"), "open while a member is generating"
        runner.room.sessions["chris"]._responding = False
        gt = asyncio.ensure_future(asyncio.sleep(5))
        runner._group_turn_tasks.add(gt)                 # e.g. the opener, just spawned
        runner._group_turn_waiting = True
        await runner._maybe_turn_open()
        assert not ws.frames("turn_open"), "open while a room turn is queued"
        gt.cancel()
        runner._group_turn_tasks.clear()
        runner._group_turn_waiting = False
        await runner._maybe_turn_open()
        assert len(ws.frames("turn_open")) == 1
        return ws

    _run(go())


def test_the_watchdog_ticks_the_cue_and_the_encounter_opens_with_it():
    src = (ROOT / "server" / "realtime_voice_session.py").read_text(encoding="utf-8")
    wd = src[src.index("    async def _silence_watchdog"):src.index("    async def _proactive_handoff")]
    assert "await self._maybe_turn_open()" in wd
    run_src = src[src.index("    async def run(self)"):src.index("    async def _on_turn_ended")]
    assert run_src.index("self._spawn_group_turn(self._open_group_scene())") < \
        run_src.index("await self._maybe_turn_open()"), (
        "the opening cue is decided after a room's opener is spawned")


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# The page: the pill, from the audio clock and turn_open, in 1:1 and rooms
# --------------------------------------------------------------------------

PAGE_HARNESS = r"""'use strict';
const path = require('path');
const assert = require('assert');
const { bootV2, vm } = require(path.join(__dirname, 'stub.js'));
const PAGE = process.argv[2];

function page(mode, castList) {
  const b = bootV2(PAGE, '?scenario=X');
  const set = (code) => vm.runInContext(code, b.ctx);
  const get = (code) => vm.runInContext(code, b.ctx);
  const frame = (m) => b.ctx.handleServerFrame({ data: JSON.stringify(m) });
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
    playbackTime = 0; started = true;
    ws = { readyState: 1, send(m) { __sent.push(JSON.parse(m)); }, close() {} };
  `);
  frame({ type: 'session', session_id: 's_1', scenario: { title: 'T', mode: mode },
          cast: castList });
  const pill = () => b.dom.$('turnState').textContent;
  // Let the audio clock run: tickSpeech is on requestAnimationFrame.
  const at = async (t) => { set(`__now = ${t}`); await b.clock.advance(50); };
  const say = async (id, name, seq, fromT, secs) => {
    frame({ type: 'assistant_started', agent_id: id, agent_name: name, turn: seq });
    set(`for (let i = 0; i < ${secs * 10}; i++) playPcmChunk(new Int16Array(1600).buffer);`);
  };
  return { b, set, get, frame, pill, at, say };
}

(async () => {
  // ---- 1:1: the opening, a line, the gap before its end, the open floor
  {
    const p = page('single', [{ id: 'morgan', name: 'Morgan' }]);
    const lines = p.b.dom.$('transcript').children.map(c => c.textContent);
    assert(lines.some(t => /You speak first/.test(t)), '1:1 lost its opening line: ' + JSON.stringify(lines));
    assert(!/Say hello/.test(p.pill()), 'the old opening pill: ' + p.pill());
    p.frame({ type: 'turn_open' });
    assert.strictEqual(p.pill(), 'You can speak now');

    p.frame({ type: 'speech_started' });
    assert.strictEqual(p.pill(), 'Listening…');
    await p.at(0.1);
    // The reply starts generating: not yet the character's turn on screen.
    await p.say('morgan', 'Morgan', 1, 0.1, 2);        // audio 0.12 .. 2.12
    assert.strictEqual(p.pill(), 'Listening…', 'named before the voice: ' + p.pill());
    await p.at(0.5);
    assert.strictEqual(p.pill(), 'Morgan is speaking');
    // Generation ends long before playback does: nothing changes, and no
    // timer changes it later.
    p.frame({ type: 'assistant_done', agent_id: 'morgan' });
    await p.at(0.9);
    await p.b.clock.advance(5000);
    assert.strictEqual(p.pill(), 'Morgan is speaking', 'the old Your turn / 4 s switch: ' + p.pill());
    // turn_open cannot arrive before the ack; if it did, the playing line wins.
    p.frame({ type: 'turn_open' });
    assert.strictEqual(p.pill(), 'Morgan is speaking');
    await p.at(2.5);
    assert.strictEqual(p.pill(), 'You can speak now');

    // A barge-in: the participant is heard.
    await p.say('morgan', 'Morgan', 2, 2.5, 2);
    await p.at(3.0);
    assert.strictEqual(p.pill(), 'Morgan is speaking');
    p.frame({ type: 'speech_started' });
    p.frame({ type: 'assistant_interrupted' });
    assert.strictEqual(p.pill(), 'Listening…');
    const text = JSON.stringify(p.b.dom.$('transcript').children.map(c => c.textContent));
    assert(!/Your turn/.test(text));
  }

  // ---- a room: no "You speak first", the same wording, the follow-up gap
  {
    const p = page('group', [{ id: 'dan', name: 'Dan' }, { id: 'chris', name: 'Chris' }]);
    const lines = p.b.dom.$('transcript').children.map(c => c.textContent);
    assert(!lines.some(t => /You speak first/.test(t)), 'a room still says You speak first');
    assert(!/Say hello|speak first/.test(p.pill()), p.pill());
    await p.say('dan', 'Dan', 1, 0, 1);                 // the lead's opener, 0.02 .. 1.02
    await p.at(0.3);
    assert.strictEqual(p.pill(), 'Dan is speaking');
    p.frame({ type: 'assistant_done', agent_id: 'dan' });
    await p.at(1.4);
    // Dan has stopped; Chris's follow-up is still being generated, so the
    // floor is not offered (no turn_open) and the pill does not say it is.
    assert.notStrictEqual(p.pill(), 'You can speak now');
    await p.say('chris', 'Chris', 2, 1.4, 1);
    await p.at(1.8);
    assert.strictEqual(p.pill(), 'Chris is speaking');
    p.frame({ type: 'assistant_done', agent_id: 'chris' });
    await p.at(2.6);
    p.frame({ type: 'turn_open' });
    assert.strictEqual(p.pill(), 'You can speak now');
    p.frame({ type: 'speech_started' });
    assert.strictEqual(p.pill(), 'Listening…');
  }
  console.log('TURN CUE PAGE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def test_the_pill_follows_the_audio_and_the_server_in_1to1_and_rooms(tmp_path):
    from test_client_blockers import DOM_STUB      # the shared thin browser

    (tmp_path / "stub.js").write_text(DOM_STUB, encoding="utf-8")
    h = tmp_path / "harness.js"
    h.write_text(PAGE_HARNESS, encoding="utf-8")
    proc = subprocess.run([_node(), str(h), str(V2)], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "TURN CUE PAGE OK" in proc.stdout


def test_the_old_cues_are_gone():
    src = V2.read_text(encoding="utf-8")
    script = src[src.index("<script>"):]
    assert "setTurnStateThen" not in script, "the 4-second Your turn -> Listening switch"
    assert "setTurnState('Your turn'" not in script
    for said in ("You can speak now", "Listening…", "is speaking"):
        assert said in script
