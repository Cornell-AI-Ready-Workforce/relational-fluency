"""The end policy of 2026-09-28 (issue #34; pipeline 2026-09-28a).

The researchers' decision: from 7:00 into an encounter the participant CAN
move on to the next conversation (End unlocks, with a neutral notice that they
may move on whenever they are ready), and they can keep talking until 12:00.
Nothing ends the encounter automatically before the 12:00 ceiling: no
auto-advance at 7:00 once the beats are spent, and a character's
end_conversation call before 12:00 does not end it; the call is answered with
a function_call_output saying the conversation goes on, and on gpt the
character is then asked to carry on. A warning comes shortly before the
automatic end. The gate and End work on every link type (study, internal
/test, direct researcher links). Moving between interactions inside an
encounter is unchanged.

What it replaces, from the archive:
* s_1790273626_376454 (S2A, direct link): the character called
  end_conversation at 318.8 s, was held at the floor, and the first turn to
  finish past 420 s ended the encounter (429.0 s); the reporter saw the camera
  go off mid-conversation, with no ring or signal on a direct link.
* s_1790278762_09bcbb (S2A, gpt, 2026-09-24c): end_conversation 1.5 s before
  the floor, held, and then no event at all for 140 s: the held call left the
  character silent and nothing re-checked the clock.
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

from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import realtime as R  # noqa: E402

import test_lost_participant_round as harness  # noqa: E402
from test_participant_turn_integrity import GPT, GEMINI, bridge  # noqa: E402

V2 = ROOT / "static" / "v2.html"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _study_numbers(monkeypatch):
    for k in ("ENCOUNTER_MIN_SECONDS", "ENCOUNTER_WRAP_SECONDS", "ENCOUNTER_MAX_SECONDS"):
        monkeypatch.delenv(k, raising=False)


def _last(runner, *, ago):
    runner.segment = len(runner.interactions) - 1
    runner._series_idx = 0
    # The participant spoke as the encounter opened: the floor counts from
    # their first line since pipeline 2026-09-28b, the ceiling from the start.
    runner._encounter_started_at = runner._first_line_at = time.time() - ago


def _spent(runner, monkeypatch):
    """Every beat fired, and the interaction well past its own minimums."""
    monkeypatch.setattr(runner, "_next_trigger", lambda: None)
    runner._turns_this_interaction = 40
    runner._interaction_started_at = time.time() - 400
    calls = []

    async def advance():
        calls.append(1)
        return False
    monkeypatch.setattr(runner, "_advance_segment", advance)
    return calls


# --------------------------------------------------------------------------
# 1. Nothing automatic ends the last interaction before the ceiling
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ago", [100, 425, 700])
def test_spent_beats_no_longer_end_the_last_interaction(monkeypatch, ago):
    runner, session, ws = harness.make_runner("S2A")
    _last(runner, ago=ago)
    calls = _spent(runner, monkeypatch)
    _run(runner._maybe_advance())
    _run(runner._maybe_advance())
    assert calls == [] and not ws.frames("encounter_complete")
    assert not session.store.of("interaction_complete")
    (held,) = session.store.of("auto_end_held")
    assert held["reason"] == "auto_advance" and held["seconds_to_ceiling"] > 0
    assert not ws.frames("floor_held"), "a hold nobody asked for is not announced"


def test_the_ceiling_still_ends_it(monkeypatch):
    runner, session, ws = harness.make_runner("S2A")
    _last(runner, ago=721)
    _spent(runner, monkeypatch)
    _run(runner._maybe_advance())
    (done,) = ws.frames("encounter_complete")
    assert done["reason"] == "ceiling" and session.store.of("ceiling_reached")


def test_moving_between_interactions_is_unchanged(monkeypatch):
    runner, session, ws = harness.make_runner("S2A")      # i1 -> i2, one character
    assert not runner._is_last_segment()
    runner._encounter_started_at = time.time() - 200
    calls = _spent(runner, monkeypatch)
    _run(runner._maybe_advance())
    assert calls == [1] and session.store.of("interaction_complete")
    assert not session.store.of("auto_end_held")


def test_the_participants_move_on_still_opens_at_the_floor(monkeypatch):
    runner, session, ws = harness.make_runner("S2A")
    _last(runner, ago=100)
    calls = _spent(runner, monkeypatch)
    _run(runner._handle_client_command(json.dumps({"type": "advance_interaction"})))
    assert calls == [] and session.store.of("floor_held")[0]["reason"] == "move_on"
    runner._encounter_started_at = runner._first_line_at = time.time() - 425
    _run(runner._handle_client_command(json.dumps({"type": "advance_interaction"})))
    assert calls == [1] and ws.frames("encounter_complete")


# --------------------------------------------------------------------------
# 2. A held end_conversation is answered, and on gpt the character goes on
# --------------------------------------------------------------------------

CALL = {"type": "tool_call", "name": "end_conversation", "call_id": "call_E1",
        "arguments": "{}"}


def _gpt_runner(*, ago=500, scenario="S2A"):
    runner, session, ws = harness.make_runner(scenario)
    rt = bridge(GPT)
    runner.rt = rt
    _last(runner, ago=ago)
    return runner, session, ws, rt


def _outputs(rt):
    return [m["item"] for m in rt.ws.sent
            if m["type"] == "conversation.item.create"
            and m["item"].get("type") == "function_call_output"]


def test_the_row_says_how_a_held_call_is_answered():
    assert R.tool_output_for(GPT) == "request"
    assert R.tool_output_for(GEMINI) is None
    assert R.tool_output_for("nto.gemini-live-2.5-flash-native-audio") is None
    assert R.tool_output_for("no-such-model") is None


@pytest.mark.parametrize("ago", [300, 500, 700])
def test_a_call_before_the_ceiling_is_answered_and_the_character_goes_on(ago):
    async def go():
        runner, session, ws, rt = _gpt_runner(ago=ago)
        rt._response_active = True               # the reply that made the call
        completed = await runner._on_tool_call(rt, dict(CALL))
        assert completed is False
        (out,) = _outputs(rt)
        assert out["call_id"] == "call_E1" and out["output"] == R.TOOL_CALL_CONTINUES
        assert "Do not call end_conversation again yet" in out["output"]
        await asyncio.sleep(0.15)
        assert "response.create" not in rt.ws.types(), "refused while the reply runs"
        rt._response_active = False              # its response.done
        await asyncio.sleep(0.2)
        assert rt.ws.types().count("response.create") == 1
        return runner, session, ws

    runner, session, ws = _run(go())
    assert not ws.frames("encounter_complete")
    (ans,) = session.store.of("tool_call_answered")
    assert ans["reason"] == "held_to_ceiling" and ans["reply_requested"] is True
    (rep,) = session.store.of("held_call_reply")
    assert rep["requested"] is True
    (held,) = session.store.of("auto_end_held")
    assert held["reason"] == "end_conversation"
    assert bool(ws.frames("floor_held")) is (ago < 420), (
        "'keep going' before the floor, and nothing after it")


def test_a_participant_who_speaks_first_is_answered_instead():
    async def go():
        runner, session, ws, rt = _gpt_runner()
        rt._response_active = True
        await runner._on_tool_call(rt, dict(CALL))
        rt._last_commit_at = time.time() + 0.01  # their commit draws the reply
        rt._response_active = False
        await asyncio.sleep(0.2)
        assert "response.create" not in rt.ws.types()
        return session

    session = _run(go())
    (rep,) = session.store.of("held_call_reply")
    assert rep["requested"] is False and rep["skipped"] == "participant_turn"


def test_a_second_call_in_that_reply_is_answered_but_not_asked_again():
    async def go():
        runner, session, ws, rt = _gpt_runner()
        await runner._on_tool_call(rt, dict(CALL))
        await asyncio.sleep(0.2)
        rt._response_active = False
        await runner._on_tool_call(rt, dict(CALL, call_id="call_E2"))
        await asyncio.sleep(0.2)
        assert rt.ws.types().count("response.create") == 1, "two calls cannot loop"
        assert [o["call_id"] for o in _outputs(rt)] == ["call_E1", "call_E2"]
        return session

    session = _run(go())
    reps = session.store.of("held_call_reply")
    assert [r["requested"] for r in reps] == [True, False]
    assert reps[1]["skipped"] == "already_asked"


def test_a_route_whose_row_says_nothing_is_left_as_it_was():
    async def go():
        runner, session, ws = harness.make_runner("S2A")
        rt = bridge(GEMINI)
        runner.rt = rt
        _last(runner, ago=500)
        assert await runner._on_tool_call(rt, dict(CALL)) is False
        assert not _outputs(rt)
        return session, ws

    session, ws = _run(go())
    assert not ws.frames("encounter_complete"), "held all the same"
    assert not session.store.of("tool_call_answered")


def test_between_interactions_the_call_advances_as_before(monkeypatch):
    runner, session, ws = harness.make_runner("S2A")
    rt = bridge(GPT)
    runner.rt = rt
    calls = []

    async def advance():
        calls.append(1)
        return True
    monkeypatch.setattr(runner, "_advance_segment", advance)
    assert _run(runner._on_tool_call(rt, dict(CALL))) is False
    assert calls == [1] and not _outputs(rt)


def test_at_the_ceiling_the_call_completes_and_nothing_reconnects(monkeypatch):
    runner, session, ws, rt = _gpt_runner(ago=721)

    async def advance():
        return False
    monkeypatch.setattr(runner, "_advance_segment", advance)
    assert _run(runner._on_tool_call(rt, dict(CALL))) is True
    assert ws.frames("encounter_complete") and runner._closed is True


def test_a_room_members_call_is_answered_without_asking_for_a_reply():
    async def go():
        runner, session, ws = harness.make_runner("S4A")
        member = bridge(GPT)
        _last(runner, ago=500)
        await runner._advance_from_tool("dan", member, dict(CALL))
        await asyncio.sleep(0.1)
        assert [o["call_id"] for o in _outputs(member)] == ["call_E1"]
        assert "response.create" not in member.ws.types(), "the floor decides who speaks"
        return session, ws

    session, ws = _run(go())
    (ans,) = session.store.of("tool_call_answered")
    assert ans["agent_id"] == "dan" and ans["reply_requested"] is False
    assert not ws.frames("encounter_complete")


# --------------------------------------------------------------------------
# 3. The clock on its own tick: move-on at the floor, the warning, the stop
# --------------------------------------------------------------------------

def test_the_clock_announces_the_floor_the_warning_and_the_stop():
    runner, session, ws = harness.make_runner("S2A")
    # The participant spoke as the encounter opened (the floor's clock).
    runner._encounter_started_at = runner._first_line_at = time.time() - 100
    assert _run(runner._encounter_clock_tick()) is False
    assert not ws.frames("move_on_open")
    runner._encounter_started_at = runner._first_line_at = time.time() - 421
    assert _run(runner._encounter_clock_tick()) is False
    assert _run(runner._encounter_clock_tick()) is False
    assert len(ws.frames("move_on_open")) == 1 and len(session.store.of("move_on_open")) == 1
    assert not ws.frames("wrap_up")
    runner._encounter_started_at = time.time() - 661
    assert _run(runner._encounter_clock_tick()) is False
    (wrap,) = ws.frames("wrap_up")
    assert 50 <= wrap["seconds_left"] <= 60
    runner._encounter_started_at = time.time() - 721
    assert _run(runner._encounter_clock_tick()) is True
    assert _run(runner._encounter_clock_tick()) is True
    (done,) = ws.frames("encounter_complete")
    assert done["reason"] == "ceiling"


def test_the_watchdog_ends_a_silent_encounter_at_the_ceiling(monkeypatch):
    """s_1790278762: a quiet last interaction had no upper bound on a direct
    link; the stop waited for a turn to finish."""
    monkeypatch.setattr(R, "probe_tick_s", lambda: 0.01)
    runner, session, ws = harness.make_runner("S2A")
    runner._encounter_started_at = time.time() - 721
    _run(asyncio.wait_for(runner._silence_watchdog(), timeout=2))
    assert ws.frames("encounter_complete")


def test_the_page_is_told_the_clock_on_every_link(monkeypatch):
    monkeypatch.setenv("ENCOUNTER_MIN_SECONDS", "60")
    runner, session, ws = harness.make_runner("S2A")
    runner._encounter_started_at = time.time() - 1.5
    _run(runner._announce_clock())
    (clock,) = ws.frames("encounter_clock")
    assert clock["min_seconds"] == 60.0 and clock["max_seconds"] == 720.0
    assert 1.4 <= clock["elapsed_s"] <= 3
    src = (ROOT / "server" / "realtime_voice_session.py").read_text(encoding="utf-8")
    run_src = src[src.index("    async def run(self)"):src.index("    async def _on_turn_ended")]
    assert run_src.index("_announce_opening()") < run_src.index("_announce_clock()")


# --------------------------------------------------------------------------
# 4. The page: the gate on every link type, the notice, the warning
# --------------------------------------------------------------------------

PAGE_HARNESS = r"""'use strict';
const path = require('path');
const assert = require('assert');
const { bootV2, vm } = require(path.join(__dirname, 'stub.js'));
const PAGE = process.argv[2];

function page(search, runObj) {
  const b = bootV2(PAGE, search);
  const set = (code) => vm.runInContext(code, b.ctx);
  const get = (code) => vm.runInContext(code, b.ctx);
  const frame = (m) => b.ctx.handleServerFrame({ data: JSON.stringify(m) });
  b.sandbox.__run = runObj || null;
  set(`run = window.__run; started = true; sessionId = 's_x'; cast = [{ id: 'morgan', name: 'Morgan' }];
       ws = { readyState: 1, send() {}, close() {} };
       timerStartMs = Date.now(); sessionStartedAt = Date.now();
       $('stopBtn').style.display = 'inline-block';`);
  const notices = () => b.dom.$('transcript').children
    .filter(c => c.className === 'system-note').map(c => c.textContent);
  // The participant spoke as the conversation opened: the ring and End count
  // from their first line (floorStartMs) since pipeline 2026-09-28b.
  const at = (s) => set(`timerStartMs = floorStartMs = Date.now() - ${s} * 1000; renderTimer();`);
  return { b, set, get, frame, notices, at };
}

(async () => {
  for (const [label, search, runObj] of [
    ['direct link', '?scenario=S2A', null],
    ['internal run', '?run=r_1&participant_id=P1',
      { run_id: 'r_1', cohort: 'internal', position: 1, total: 4, done: false }],
    ['study run', '?run=r_1&participant_id=P1',
      { run_id: 'r_1', cohort: 'study', position: 4, total: 4, done: false }],
  ]) {
    const p = page(search, runObj);
    const $ = p.b.dom.$;
    let ended = 0;
    p.set('endSession = () => { __ended = (typeof __ended === "number" ? __ended : 0) + 1; };');
    p.set('__ended = 0;');

    // Before the floor: the ring is up and End is held, with a reason.
    p.at(100);
    assert($('gate').classList.contains('show'), label + ': no ring');
    assert($('stopBtn').classList.contains('locked'), label + ': End is not held before 7:00');
    $('stopBtn').click();
    assert.strictEqual(p.get('__ended'), 0, label + ': End finished the conversation before 7:00');
    // Worded as when they CAN move on, never as the conversation ending: it
    // runs to 12:00 unless they choose to (review of 850b08e).
    const early = $('gateNote').textContent;
    assert(/can (move on to the next conversation|end this conversation) in about 6 minutes/.test(early),
      label + ': ' + early);
    assert(!/more minutes|runs about/.test(early), label + ': counts down to an end: ' + early);
    assert(/can move on in about 6 minutes/.test($('gateLabel').textContent),
      label + ': ' + $('gateLabel').textContent);
    // The character saying goodbye early: the conversation carries on.
    p.frame({ type: 'floor_held', seconds_left: 180 });
    const held = $('gateNote').textContent;
    assert(/^Keep going — you can (move on to the next conversation|end this conversation) in about 3 minutes\.$/.test(held),
      label + ': ' + held);
    assert.strictEqual(p.notices().length, 0, label + ': a notice before the floor');

    // At the floor: unlocked, and told once, neutrally.
    p.at(421);
    p.at(430);
    assert(!$('stopBtn').classList.contains('locked'), label + ': End still held after 7:00');
    const said = p.notices();
    assert.strictEqual(said.length, 1, label + ': move-on notice ' + JSON.stringify(said));
    assert(/whenever you are ready/.test(said[0]), said[0]);
    assert(/ready/.test($('gateLabel').textContent), $('gateLabel').textContent);
    p.frame({ type: 'move_on_open' });
    assert.strictEqual(p.notices().length, 1, label + ': the server frame repeated the notice');

    // Shortly before the stop: a visible warning, once.
    p.frame({ type: 'wrap_up', seconds_left: 60 });
    p.at(662);
    const warned = p.notices().filter(t => /ends automatically/.test(t));
    assert.strictEqual(warned.length, 1, label + ': warning ' + JSON.stringify(p.notices()));
    assert(/about 1 minute/.test(warned[0]), warned[0]);
    assert.strictEqual($('gateLabel').textContent, 'Wrapping up');

    // End now finishes the conversation.
    $('stopBtn').click();
    assert.strictEqual(p.get('__ended'), 1, label + ': End after 7:00 did nothing');
  }

  // The runner's clock: its numbers and where it stands, on a direct link.
  {
    const p = page('?scenario=S2A', null);
    p.frame({ type: 'encounter_clock', min_seconds: 60, wrap_seconds: 100, max_seconds: 120, elapsed_s: 2 });
    assert.strictEqual(p.get('MIN_S'), 60);
    assert.strictEqual(p.get('MAX_S'), 120);
    const lag = p.get('(Date.now() - timerStartMs) / 1000');
    assert(lag >= 1.9 && lag < 3, 'timer not lined up with the server: ' + lag);
    // The server's floor opening moves a page clock that is behind up to it.
    p.set('timerStartMs = Date.now() - 30 * 1000;');
    p.frame({ type: 'move_on_open' });
    assert(!p.b.dom.$('stopBtn').classList.contains('locked'), 'End held after the server floor');
    assert.strictEqual(p.notices().length, 1);
  }
  // The first screen (its text: test_the_first_screen_says_how_long_...):
  // the page's own numbers, and the study's stop control named only where
  // there is one.
  for (const [label, search, runObj] of [
    ['direct link', '?scenario=S2A', null],
    ['study run', '?run=r_1&participant_id=P1',
      { run_id: 'r_1', cohort: 'study', position: 1, total: 4, done: false }],
  ]) {
    const p = page(search, runObj);
    p.set('MIN_S = 5 * 60; MAX_S = 10 * 60; describeEncounterLength();');
    assert.strictEqual(p.b.dom.$('fictionMin').textContent, '5', label);
    assert.strictEqual(p.b.dom.$('fictionMax').textContent, '10', label);
    const leave = p.b.dom.$('fictionLeave').style.display;
    assert.strictEqual(leave, runObj ? 'inline' : 'none', label + ': the stop sentence ' + leave);
  }
  console.log('END POLICY PAGE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def test_the_page_gate_notice_and_warning_on_every_link_type(tmp_path):
    from test_client_blockers import DOM_STUB      # the shared thin browser

    (tmp_path / "stub.js").write_text(DOM_STUB, encoding="utf-8")
    h = tmp_path / "harness.js"
    h.write_text(PAGE_HARNESS, encoding="utf-8")
    proc = subprocess.run([_node(), str(h), str(V2)], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "END POLICY PAGE OK" in proc.stdout


def test_the_first_screen_says_how_long_a_conversation_runs():
    """The fiction card, the screen the #37 quiet-room notice opens, said "You
    can end a conversation at any time", while End is held until 7:00 on
    every link type (review of 850b08e). It says what is true now, and it is
    filled from the page's clock before it is shown."""
    import re

    src = V2.read_text(encoding="utf-8")
    card = src[src.index('id="fictionOverlay"'):src.index('id="fictionAck"')]
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", card))
    assert "end a conversation at any" not in text and "at any time." not in text.replace(
        "stop taking part in the study at any time", ""), text
    assert "at least 7 minutes" in text and "at 12 minutes" in text, text
    for span in ('id="fictionMin"', 'id="fictionMax"', 'id="fictionLeave"'):
        assert span in card
    boot = src[src.index("if (needsFictionAck()) {"):][:200]
    assert "describeEncounterLength();" in boot


def test_the_page_defaults_are_the_new_numbers():
    src = V2.read_text(encoding="utf-8")
    assert "let MIN_S = 7 * 60, WRAP_S = 11 * 60, MAX_S = 12 * 60;" in src
    gate = src[src.index("function renderGate(s)"):src.index("let gateNoteTimer")]
    assert "gateActive()" not in gate, "the 7:00 gate is back to study links only"


def test_the_versions_move():
    from server import llm
    assert llm.PIPELINE_VERSION >= "2026-09-28a"
    assert llm.ROOM_PACING_VERSION >= "2026-09-28a"
    prov = llm.provenance("gpt-realtime-2.1")
    assert prov["pipeline_version"] == llm.PIPELINE_VERSION
    assert prov["room_pacing_version"] == llm.ROOM_PACING_VERSION
