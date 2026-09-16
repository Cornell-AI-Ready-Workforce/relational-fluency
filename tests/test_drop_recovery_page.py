"""The participant page after a connection loss, and the captions it paints.

Reproduced live on nto.gemini-live-2.5-flash with a hesitant participant, and
reported by the researcher running real encounters the same day:

* "connection lost with Sasha and I couldn't retry". A gateway stall or a
  gateway close arrives on the page as an ``error`` frame before the socket
  drops, and the page read *any* stated error as "this encounter cannot run":
  the first such drop offered one retry, the second hid the retry button and
  left "Finish here and get my code" as the only door, in the middle of a
  four-encounter run. A conversation that had been running for minutes before
  the gateway went is not a condition that repeats; and even when it is, the
  next door is the next encounter, not the end of the study.

* "it feels like the agent isn't hearing me" had a page-side half: the user
  caption bubble was only closed on ``assistant_started``, so while the
  character was stalled every committed participant turn accreted into one
  growing bubble ("side, and I like you Don't appreciate it. | ya | Sicher."),
  and with a real microphone the same utterance transcribed twice was painted
  as one doubled line ("...I definitely did not Yeah, I heard about what you
  said ... I definitely did not").

These tests press the page's own buttons. The page's script is run in a Node
vm against a thin DOM and a routed fetch, frames are fed to its own
``handleServerFrame``, and the drop is what its socket-close listener does.
Skipped where node is not installed, like the other page harnesses.

Set ``RF_V2_PAGE`` to run the same harness against another copy of the page
(this is how "red before, green after" was proven against the unmodified
source).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
V2 = Path(os.environ.get("RF_V2_PAGE") or (ROOT / "static" / "v2.html"))


# --------------------------------------------------------------------------
# Plain reads of the file: the invariants that need no browser.
# --------------------------------------------------------------------------

def test_the_page_never_paints_an_object_into_a_caption():
    """The "{}" the researcher saw at pause boundaries is not a page template.

    Confirmed by reading every place the page writes a participant line: there
    is no literal "{}" outside fetch bodies, and no JSON.stringify feeding a
    caption. Kept as a guard so a future template cannot start doing it.
    """
    src = V2.read_text(encoding="utf-8")
    script = src[src.index("<script>"):]
    braces = [m.start() for m in re.finditer(r"'\{\}'|\"\{\}\"", script)]
    for at in braces:
        line = script[script.rfind("\n", 0, at) + 1:script.find("\n", at)]
        assert "body:" in line, f"a literal '{{}}' outside a fetch body: {line.strip()}"
    for fn in ("function renderUserCaption(", "function appendTranscript(", "function appendNotice("):
        body = script[script.index(fn):script.index("\n  }", script.index(fn))]
        assert "JSON.stringify" not in body, f"{fn} paints an object"


def test_a_committed_participant_turn_closes_its_own_bubble():
    """One bubble per committed turn, not one bubble until the character speaks."""
    src = V2.read_text(encoding="utf-8")
    handler = src[src.index("m.type === 'user_transcript'"):src.index("m.type === 'assistant_started'")]
    assert "finalizeUserCaption()" in handler, (
        "a final user_transcript does not close the caption bubble, so while the "
        "character is stalled every later turn is appended to the same line")


def test_the_drop_card_always_has_a_door_forward():
    """The exhausted card must offer the next encounter, not only the exit."""
    src = V2.read_text(encoding="utf-8")
    exit_fn = src[src.index("function showFailureExit("):src.index("\n  $('dropReconnect').addEventListener")]
    assert "stoppedEarly" in src and "skipEncounter" in exit_fn, (
        "no door from the drop card to the next encounter")
    dropped = src[src.index("function onConnectionDropped("):src.index("function showFailureExit(")]
    assert "agentTurns" in dropped or "conversationRan" in dropped, (
        "a drop after a running conversation is still classed as an encounter that cannot run")


# --------------------------------------------------------------------------
# The harness: the page's script in a Node vm, pressed like a participant.
# --------------------------------------------------------------------------

STUB = r"""'use strict';
const vm = require('vm');

function makeClock() {
  let now = 0, nextId = 1;
  const timers = [];
  const api = {
    now: () => now,
    setTimeout(fn, ms) { const t = { id: nextId++, at: now + (ms || 0), fn }; timers.push(t); return t.id; },
    clearTimeout(id) { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
    setInterval(fn, ms) { return api.setTimeout(() => {}, ms); },
    clearInterval(id) { api.clearTimeout(id); },
    async advance(ms) {
      const target = now + ms;
      for (;;) {
        await api.flush();
        const due = timers.filter(t => t.at <= target).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        timers.splice(timers.indexOf(due), 1);
        now = due.at;
        due.fn();
      }
      now = target;
      await api.flush();
    },
    async flush() { for (let i = 0; i < 50; i++) await new Promise(r => setImmediate(r)); },
  };
  return api;
}

function makeDom() {
  const byId = new Map();
  function el(id) {
    const node = {
      id: id || '', style: {}, dataset: {}, children: [],
      textContent: '', innerHTML: '', value: '', disabled: false,
      className: '', scrollTop: 0, scrollHeight: 0, onclick: null, tagName: 'DIV',
      classList: {
        _s: new Set(),
        add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
        contains(c) { return this._s.has(c); },
        toggle(c, on) { if (on === undefined) on = !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); },
      },
      listeners: {},
      addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
      removeEventListener() {},
      appendChild(c) { this.children.push(c); c.parent = this; return c; },
      removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); },
      remove() { if (this.parent) this.parent.removeChild(this); },
      focus() {}, blur() {},
      click() { (this.listeners.click || []).forEach(f => f({})); if (this.onclick) this.onclick({}); },
      _q: new Map(),
      querySelector(sel) { if (!this._q.has(sel)) this._q.set(sel, el(sel)); return this._q.get(sel); },
      querySelectorAll: () => [],
      getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
      setAttribute() {}, getAttribute: () => null, insertAdjacentHTML() {},
      play: () => Promise.resolve(), pause() {},
      get firstChild() { return this.children[0] || null; },
      get offsetParent() { return this.style.display === 'none' ? null : {}; },
    };
    return node;
  }
  const document = {
    getElementById(id) { if (!byId.has(id)) byId.set(id, el(id)); return byId.get(id); },
    createElement(tag) { const n = el(''); n.tagName = String(tag).toUpperCase(); return n; },
    createTextNode(t) { const n = el(''); n.textContent = t; return n; },
    querySelector(sel) { if (!byId.has(sel)) byId.set(sel, el(sel)); return byId.get(sel); },
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    body: el('body'), head: el('head'),
    get hidden() { return false; }, visibilityState: 'visible',
  };
  return { document, byId, $: (id) => document.getElementById(id) };
}

function makeFetch() {
  const calls = [];
  let routes = [];
  function res(status, body) {
    return { ok: status >= 200 && status < 300, status, json: async () => body,
             text: async () => JSON.stringify(body) };
  }
  function fetchStub(url, opts) {
    opts = opts || {};
    const call = { url: String(url), method: (opts.method || 'GET') };
    calls.push(call);
    const route = routes.find(r => call.url.includes(r.match));
    return new Promise((resolve, reject) => {
      if (!route) return reject(new Error('unrouted fetch: ' + call.url));
      const out = route.fn(call, calls.filter(c => c.url.includes(route.match)).length);
      if (out === 'hang') return;
      if (out instanceof Error) return reject(out);
      resolve(out);
    });
  }
  return {
    fetch: fetchStub, calls, res,
    route(list) { routes = list; calls.length = 0; },
    urlsOf(sub) { return calls.filter(c => c.url.includes(sub)).map(c => c.url); },
  };
}

function makeContext(extra) {
  const clock = makeClock();
  const dom = makeDom();
  const net = makeFetch();
  const sockets = [];
  function WebSocket(url) {
    this.url = String(url); this.listeners = {}; this.sent = [];
    this.addEventListener = (ev, fn) => { (this.listeners[ev] = this.listeners[ev] || []).push(fn); };
    this.send = (d) => { this.sent.push(d); };
    this.close = () => {};
    sockets.push(this);
  }
  const sandbox = {
    console, JSON, Math, Date, Promise, Object, Array, String, Number, Boolean,
    Set, Map, RegExp, Error, TypeError, isNaN, parseInt, parseFloat,
    encodeURIComponent, decodeURIComponent, URLSearchParams, AbortController,
    TextEncoder, TextDecoder, Uint8Array, Int16Array, Float32Array, ArrayBuffer,
    DataView, atob: (s) => s, btoa: (s) => s,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    requestAnimationFrame: (fn) => clock.setTimeout(fn, 16),
    cancelAnimationFrame: (id) => clock.clearTimeout(id),
    fetch: net.fetch,
    document: dom.document,
    Blob: function Blob(parts, o) { this.size = 0; this.type = (o || {}).type || ''; },
    MediaStream: function MediaStream() {
      this.getVideoTracks = () => []; this.getAudioTracks = () => []; this.getTracks = () => [];
    },
    WebSocket,
    localStorage: { _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; },
                    setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
    sessionStorage: { _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; },
                      setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
    alert() {}, confirm: () => true,
    history: { replaceState() {} },
  };
  sandbox.navigator = {
    mediaDevices: { getUserMedia: async () => { throw new Error('no camera in a test'); } },
    userAgent: 'node',
    sendBeacon: () => true,
  };
  sandbox.addEventListener = () => {};
  sandbox.removeEventListener = () => {};
  sandbox.window = sandbox; sandbox.globalThis = sandbox; sandbox.self = sandbox;
  Object.assign(sandbox, extra || {});
  return { ctx: vm.createContext(sandbox), sandbox, clock, dom, net, sockets };
}

/* Boot the page on a run link, with the run refused so the boot stops before
   consent and capture; the run and the session are then set the way the
   'session' frame and loadRun would set them. */
function bootV2(page, search) {
  const src = require('fs').readFileSync(page, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
  const b = makeContext({
    location: { search: search, href: 'http://t/v2' + search, host: 't', protocol: 'http:',
                reload() {}, replace() {} },
  });
  b.net.route([{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);
  vm.runInContext(src, b.ctx, { filename: 'v2.html' });
  return b;
}

module.exports = { makeContext, bootV2, vm };
"""


HARNESS = r"""'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');
const PAGE = process.argv[2];
const set = (b, code) => vm.runInContext(code, b.ctx);

const RUN = (position, total, sid) => ({
  run_id: 'r_1', participant_id: 'P1', completion_code: 'CODE-77', position, total,
  current: { id: 'S2B' }, next: { id: 'S2C' }, done: position > total,
  completed: [], withdrawn: null,
});

function frame(b, m) {
  // The page's own handler, as the socket's message listener calls it.
  b.ctx.handleServerFrame({ data: JSON.stringify(m) });
}
function drop(b) {
  // What the socket's close listener does for an unintentional close.
  set(b, 'started = true;');
  b.ctx.onConnectionDropped();
}
function buttons(node, out) {
  out = out || [];
  for (const c of node.children || []) {
    if (c.tagName === 'BUTTON') out.push(c);
    buttons(c, out);
  }
  return out;
}
const card = (b) => {
  const dt = b.dom.$('dropText');
  // The doors live under the retry button (#dropDoors), never inside the
  // wording (#dropText): the card reads action first, alternatives second.
  const dd = b.dom.$('dropDoors');
  assert.strictEqual(buttons(dt).length, 0, 'a door was painted inside the card wording, above the retry');
  return {
    shown: b.dom.$('dropNote').classList.contains('show'),
    text: dt.innerHTML.replace(/<[^>]+>/g, ''),
    reconnect: b.dom.$('dropReconnect').style.display !== 'none' ? b.dom.$('dropReconnect').textContent : null,
    doors: buttons(dd).map(x => x.textContent),
    press(label) { const x = buttons(dd).find(y => y.textContent === label); assert(x, 'no door ' + label + ' in ' + JSON.stringify(buttons(dd).map(y => y.textContent))); x.click(); },
  };
};
const userBubbles = (b) => b.dom.$('transcript').children
  .filter(c => (c.innerHTML || '').includes('speaker self'))
  .map(c => c.querySelector('.text').textContent);

/* A page that has just connected: the 'session' frame has landed, the run is
   at encounter `position` of `total`. */
async function connected(position, total, opts) {
  opts = opts || {};
  const b = bootV2(PAGE, '?run=r_1&participant_id=P1');
  await b.clock.flush();
  b.sandbox.run = RUN(position, total);
  set(b, "run = window.run; started = true; participantId = 'P1'; cast = [{ id: 'sasha', name: 'Sasha' }];");
  b.net.route([
    { match: '/api/run/r_1/advance', fn: (c) => b.net.res(200, RUN(position + 1, total)) },
    { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '', return_label: 'Return to the survey' }) },
    { match: '/api/consent', fn: () => b.net.res(200, { contact: 'the study team' }) },
    { match: '/api/run/', fn: () => b.net.res(200, RUN(position, total)) },
  ]);
  if (!opts.noSession) set(b, "sessionId = 's_live_1';");
  // startSession's per-attempt resets, without the microphone.
  set(b, "serverStatedFailure = false; userTurns = 0; intentionalClose = false;");
  try { set(b, "agentTurns = 0;"); } catch (e) {}
  return b;
}

(async () => {
  // ---- 1. A gateway loss after minutes of conversation is a reconnect, not the end
  {
    const b = await connected(2, 4);
    frame(b, { type: 'assistant_started', agent_id: 'sasha', agent_name: 'Sasha' });
    frame(b, { type: 'assistant_done' });
    frame(b, { type: 'user_transcript', final: true, text: 'What about it?' });
    frame(b, { type: 'assistant_started', agent_id: 'sasha', agent_name: 'Sasha' });
    frame(b, { type: 'assistant_done' });
    // The runner's own words for a stall, then for the gateway closing.
    frame(b, { type: 'error', message: 'no reply from the gateway after 47s; abandoning the turn' });
    frame(b, { type: 'error', message: 'realtime session closed by gateway: received 1000 (OK) limit' });
    drop(b);
    let c = card(b);
    assert(c.shown, 'no drop card after the socket closed');
    assert.strictEqual(c.reconnect, 'Reconnect',
      'a drop after a running conversation is offered as "' + c.reconnect + '" rather than Reconnect: ' + c.text);
    assert(/start again from the beginning/.test(c.text), 'the card does not say what Reconnect does: ' + c.text);
    assert(!/could not continue/.test(c.text), 'the card calls a running conversation one that could not continue');

    // The same again: the second loss in this encounter opens the doors.
    set(b, "serverStatedFailure = false; started = true;");
    frame(b, { type: 'assistant_started', agent_id: 'sasha', agent_name: 'Sasha' });
    frame(b, { type: 'error', message: 'realtime connection lost: no close frame received or sent' });
    drop(b);
    c = card(b);
    assert.strictEqual(c.reconnect, 'Reconnect', 'the retry vanished on the second loss');
    assert(c.doors.some(d => /encounter 3 of 4/.test(d)),
      'no door to the next encounter on a repeated loss: ' + JSON.stringify(c.doors));
    assert(c.doors.includes('Finish here and get my code'), 'no finish door: ' + JSON.stringify(c.doors));
    assert(/second time/.test(c.text) && /stopped early/.test(c.text),
      'the card does not say why the doors are there: ' + c.text);

    // Press the door: the run advances on the dropped session, and the next
    // screen says the conversation stopped early rather than that it finished.
    c.press(c.doors.find(d => /encounter 3 of 4/.test(d)));
    await b.clock.advance(1000);
    const adv = b.net.urlsOf('/advance');
    assert.strictEqual(adv.length, 1, 'the door did not advance the run: ' + JSON.stringify(adv));
    assert(adv[0].includes('session_id=s_live_1'), 'advanced on the wrong session: ' + adv[0]);
    assert(!b.dom.$('dropNote').classList.contains('show'), 'the drop card is still up over the next screen');
    assert.strictEqual(b.dom.$('nextOverlay').style.display, 'flex', 'no next-encounter screen');
    assert(/stopped early/i.test(b.dom.$('nextTitle').textContent), 'the next screen calls a stopped conversation finished: ' + b.dom.$('nextTitle').textContent);
    assert(/Start encounter 3 of 4/.test(b.dom.$('nextBtn').textContent), b.dom.$('nextBtn').textContent);
    assert(/stopped early/.test(b.dom.$('nextBody').innerHTML), b.dom.$('nextBody').innerHTML);
  }

  // ---- 2. An encounter that cannot start: one retry, then the next door, never a dead end
  {
    const b = await connected(2, 4);
    frame(b, { type: 'error', message: 'No gateway API key (set LITELLM_API_KEY)' });
    drop(b);
    let c = card(b);
    assert.strictEqual(c.reconnect, 'Try once more', 'first stated failure: ' + c.reconnect);
    assert(/could not continue/.test(c.text), c.text);
    assert(c.doors.some(d => /encounter 3 of 4/.test(d)), 'no way past a failed start on the first card: ' + JSON.stringify(c.doors));

    // The retry meets the same condition.
    set(b, "serverStatedFailure = false; started = true; sessionId = 's_live_2';");
    frame(b, { type: 'error', message: 'No gateway API key (set LITELLM_API_KEY)' });
    drop(b);
    c = card(b);
    assert.strictEqual(c.reconnect, null, 'a third attempt is offered after two stated failures');
    assert(c.doors.some(d => /encounter 3 of 4/.test(d)),
      'exhausted card has no door to the next encounter: ' + JSON.stringify(c.doors));
    assert(c.doors.includes('Finish here and get my code'), JSON.stringify(c.doors));
    assert(/twice/.test(c.text) && /go on/.test(c.text), 'the card does not say why: ' + c.text);
    c.press(c.doors.find(d => /encounter 3 of 4/.test(d)));
    await b.clock.advance(1000);
    assert(b.net.urlsOf('/advance')[0].includes('session_id=s_live_2'), JSON.stringify(b.net.urlsOf('/advance')));
    assert(/stopped early/i.test(b.dom.$('nextTitle').textContent), b.dom.$('nextTitle').textContent);
  }

  // ---- 3. The last encounter of the run: the door finishes the study
  {
    const b = await connected(4, 4);
    b.net.route([
      { match: '/api/run/r_1/advance', fn: () => b.net.res(200, Object.assign(RUN(5, 4), { done: true, current: null, next: null })) },
      { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '', return_label: 'Return to the survey' }) },
      { match: '/api/consent', fn: () => b.net.res(200, { contact: 'the study team' }) },
    ]);
    frame(b, { type: 'error', message: 'x' }); drop(b);
    set(b, "serverStatedFailure = false; started = true;");
    frame(b, { type: 'error', message: 'x' }); drop(b);
    const c = card(b);
    const door = c.doors.find(d => /Finish the study without this conversation/.test(d));
    assert(door, 'no door to finish on the last encounter: ' + JSON.stringify(c.doors));
    c.press(door);
    await b.clock.advance(1000);
    assert.strictEqual(b.net.urlsOf('/advance').length, 1);
    assert(/All encounters complete/.test(b.dom.$('nextTitle').textContent), b.dom.$('nextTitle').textContent);
    assert(/CODE-77/.test(b.dom.$('nextBody').innerHTML), 'no completion code: ' + b.dom.$('nextBody').innerHTML);
  }

  // ---- 4. No session ever opened: nothing to advance, so the finish door carries the code
  {
    const b = await connected(2, 4, { noSession: true });
    set(b, "sessionId = null;");
    frame(b, { type: 'error', message: 'x' }); drop(b);
    set(b, "serverStatedFailure = false; started = true;");
    frame(b, { type: 'error', message: 'x' }); drop(b);
    const c = card(b);
    assert(!c.doors.some(d => /encounter 3 of 4/.test(d)), 'a door that /advance answers 400 to: ' + JSON.stringify(c.doors));
    assert(c.doors.includes('Finish here and get my code'), JSON.stringify(c.doors));
    c.press('Finish here and get my code');
    await b.clock.advance(1000);
    assert(!b.dom.$('dropNote').classList.contains('show'));
    assert.strictEqual(b.dom.$('nextTitle').textContent, 'Finishing here');
    assert(/CODE-77/.test(b.dom.$('nextBody').innerHTML), b.dom.$('nextBody').innerHTML);
  }

  // ---- 5. A plain first drop (no stated reason) is the simple reconnect card
  {
    const b = await connected(1, 4);
    frame(b, { type: 'assistant_started', agent_id: 'sasha', agent_name: 'Sasha' });
    drop(b);
    const c = card(b);
    assert.strictEqual(c.reconnect, 'Reconnect');
    assert(/start again from the beginning/.test(c.text), c.text);
  }

  // ---- 6. Captions: one bubble per committed turn while the character is stalled
  {
    const b = await connected(1, 4);
    frame(b, { type: 'user_transcript', final: true, text: 'side, and I like you Don\'t appreciate it.' });
    frame(b, { type: 'user_transcript', final: true, text: 'ya' });
    frame(b, { type: 'user_transcript', final: true, text: 'Sicher.' });
    const bubbles = userBubbles(b);
    assert.deepStrictEqual(bubbles, ['side, and I like you Don\'t appreciate it.', 'ya', 'Sicher.'],
      'participant turns accreted into one bubble: ' + JSON.stringify(bubbles));
    assert.strictEqual(set(b, 'userTurns'), 3, 'a committed turn was not counted');
  }

  // ---- 7. Captions: the same utterance transcribed twice is painted once
  {
    const b = await connected(1, 4);
    const line = "Yeah, I heard about what you said and how you're handling the situation, and I definitely did not";
    // (a) doubled inside one transcript
    frame(b, { type: 'user_transcript', final: true, text: line + ' ' + line });
    // (b) the same line committed again a moment later
    frame(b, { type: 'user_transcript', final: true, text: line });
    // (c) a genuinely different next turn, and a short repeat that people do say
    frame(b, { type: 'user_transcript', final: true, text: "Ya. I'll work on it." });
    frame(b, { type: 'user_transcript', final: true, text: 'Okay.' });
    frame(b, { type: 'user_transcript', final: true, text: 'no no no no' });
    const bubbles = userBubbles(b);
    assert.deepStrictEqual(bubbles, [line, "Ya. I'll work on it.", 'Okay.', 'no no no no'],
      'doubled transcript painted: ' + JSON.stringify(bubbles));
    // An unclear turn is never mistaken for a repeat of the last one.
    frame(b, { type: 'user_transcript', final: true, text: 'xx', unclear: true });
    frame(b, { type: 'user_transcript', final: true, text: 'yy', unclear: true });
    assert.strictEqual(userBubbles(b).length, 6, JSON.stringify(userBubbles(b)));
  }

  // ---- 8a. A notice the encounter survived is not a stated failure, and a
  //          rebuilt gateway session is announced once, in the transcript
  {
    const b = await connected(2, 4);
    frame(b, { type: 'assistant_started', agent_id: 'sasha', agent_name: 'Sasha' });
    frame(b, { type: 'voice_notice', message: 'no reply from the gateway after 47s; abandoning the turn', transient: false });
    frame(b, { type: 'voice_notice', message: 'no reply from the gateway after 47s; abandoning the turn', transient: false });
    frame(b, { type: 'voice_notice', kind: 'reconnected', agent_id: 'sasha', attempt: 1 });
    assert.strictEqual(set(b, 'serverStatedFailure'), false, 'a survivable notice was read as a stated failure');
    const notes = b.dom.$('transcript').children.filter(c => c.className === 'system-note').map(c => c.textContent);
    assert.strictEqual(notes.filter(n => /broke up/.test(n)).length, 1, 'the same notice painted more than once: ' + JSON.stringify(notes));
    assert(notes.some(n => /line to Sasha dropped for a moment and is back/.test(n) && /say it again/.test(n)),
      'the rebuilt session is not announced: ' + JSON.stringify(notes));
    assert(!notes.some(n => /Something went wrong/.test(n)), 'a notice was painted as an error: ' + JSON.stringify(notes));
    // A later plain drop is still the reconnect card, not "could not continue".
    drop(b);
    assert.strictEqual(card(b).reconnect, 'Reconnect', card(b).text);
  }

  // ---- 8. A finished encounter clears the drop count for the next one
  {
    const b = await connected(1, 4);
    frame(b, { type: 'error', message: 'x' }); drop(b);
    set(b, "serverStatedFailure = false; started = true;");
    frame(b, { type: 'encounter_complete' });
    await b.clock.advance(1000);
    assert.strictEqual(set(b, 'fatalDrops'), 0);
    assert.strictEqual(set(b, 'encounterDrops'), 0, 'the per-encounter drop count follows the participant into the next encounter');
  }

  console.log('DROP RECOVERY OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def test_the_drop_card_and_the_captions_by_pressing_them(tmp_path):
    (tmp_path / "stub.js").write_text(STUB, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(V2)], capture_output=True, text=True,
                          encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DROP RECOVERY OK" in proc.stdout
