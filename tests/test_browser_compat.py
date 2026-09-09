"""Cross-browser regression tests for the five pages the study is delivered on.

The required matrix is Chrome, Firefox and Safari on macOS, Windows and Linux.
Chrome is the one the platform was built against and the one that works today;
everything here exists because the other six combinations were never exercised.
Participants come from CloudResearch and are paid, so a Safari user whose
recording silently fails is not a bug report, it is an encounter lost twice.

Nothing here sniffs a user agent and nothing here asserts what a particular
browser does — a user-agent string ages badly and is wrong for the long tail of
engines. What is asserted instead is that the pages ask the right *capability*
questions and behave correctly on both answers: a worklet that must be
reachable from a destination on any engine, an <audio> element whose play() may
be refused, a MediaRecorder whose flush is asynchronous, a getUserMedia that
may refuse a camera and not a microphone, an AudioBuffer rate an engine may
decline, a <video> whose container this browser may not decode.

The pages are HTML with an inline script, so they are executed in a Node vm
against a thin DOM, a stub Web Audio graph, a stub MediaRecorder and a virtual
clock, and the assertions are made by driving the page's own functions and
reading what it painted and what it sent. Skipped where node is not installed;
node is not a runtime dependency of the study.

What each part is a regression test for:

1. **The capture worklet is reachable from a destination.** `micNode.connect(
   workletNode)` was the only edge involving the worklet: it had an input and
   no path to any destination, so no engine was obliged to render it and
   `process()` might never be called. The level meter is fed by a separate
   analyser and would keep moving throughout, so every visible signal said the
   microphone was working while not one PCM byte was ever sent.
2. **Agent speech survives an <audio> element that will not play.** All agent
   audio was routed through one element whose play() rejection was discarded,
   with a fallback branch (`playDest || audioCtx.destination`) that could never
   be taken because playDest is always truthy.
3. **A refused AudioBuffer rate costs one buffer, not the encounter.** 16 kHz
   buffers were built unconditionally and played from an unguarded WebSocket
   listener, so one NotSupportedError silenced every later chunk too.
4. **A missing, denied or busy camera is said out loud and recorded.** The
   audio-only retry swallowed the error, startVideoRecording returned bare, no
   confirm event was ever generated, and Phase 2 was handed an encounter with
   no video and no explanation.
5. **The recorder's input tracks outlive its flush.** endSession stopped the
   camera track and closed the AudioContext three statements after rec.stop(),
   racing the final chunk and, on Safari's fragmented MP4, the finalization
   that makes the file playable at all.
6. **Capture failures are named apart.** Everything from a missing
   AudioWorklet to a non-secure origin was reported as "please allow microphone
   access", which is advice a participant has already taken.
7. **The rater console reports a recording it cannot play.** A wave is a mix of
   WebM and MP4 by design and nothing transcodes; an undecodable container was
   a black box with no error handler, and the rating went in anyway.
8. **The rater console's open-ended boxes come back.** They were disabled on
   the first submission and never re-enabled, so Phase 2's qualitative data was
   lost for every encounter after each rater's first.
9. **The dwell timer does not credit another tab.** The anti-skim gate, and the
   `seconds` the console posts as evidence for it, both counted time the rater
   spent away.

Run from the repo root:

    python -m pytest tests
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
V2 = STATIC / "v2.html"
PARTICIPANT = STATIC / "participant.html"
RATER = STATIC / "rater.html"
RESEARCHER = STATIC / "researcher.html"
EVIDENCE = STATIC / "evidence.html"
DIRECTOR = STATIC / "director.html"
LANDING = STATIC / "landing.html"

PAGES = [V2, PARTICIPANT, RATER, RESEARCHER, EVIDENCE, DIRECTOR, LANDING]


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structural checks. No node needed, so these hold on any machine that can run
# the suite at all — including the CI box that will never have a Mac attached.
# ---------------------------------------------------------------------------

def test_no_page_decides_anything_by_sniffing_the_user_agent():
    """The matrix is nine combinations and the long tail is larger than that.
    Every decision in these pages has to be a capability question."""
    for page in PAGES:
        src = _src(page)
        # Comments may name browsers; code may not branch on them.
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        assert "navigator.userAgent" not in code, f"{page.name} branches on the user agent"
        assert "navigator.vendor" not in code, f"{page.name} branches on the vendor string"


def test_backdrop_filter_is_always_prefixed():
    """Unprefixed backdrop-filter only reached Safari 18. Cosmetic — the rgba
    scrim underneath is the real fallback — but listed so nobody spends an
    afternoon on it during Safari testing."""
    for page in (V2, PARTICIPANT, LANDING):
        src = _src(page)
        plain = len(re.findall(r"(?<!-)\bbackdrop-filter\s*:", src))
        prefixed = len(re.findall(r"-webkit-backdrop-filter\s*:", src))
        assert plain and prefixed >= plain, (
            f"{page.name} has {plain} backdrop-filter declarations and "
            f"{prefixed} -webkit- ones")


def test_user_select_is_prefixed_on_the_rating_buttons():
    """Unprefixed user-select only reached Safari 17, and the radios under
    these labels are invisible overlays: without it a slightly-dragged click
    highlights the digit instead of scoring the item, 22 times an encounter."""
    src = _src(RATER)
    assert "-webkit-user-select: none" in src


def test_every_page_declares_a_viewport():
    """director.html had none, so its own 820px breakpoint could never fire on
    the tablet or half-screen window it was written for."""
    for page in PAGES:
        assert 'name="viewport"' in _src(page), f"{page.name} has no viewport meta"


def test_the_researcher_console_has_a_breakpoint():
    """It was the only one of the five pages with none, with a hard 380px
    sidebar, and the brief has researchers running it beside the participant's
    call — or at 150% zoom, which is the same thing to a layout."""
    assert "@media" in _src(RESEARCHER)


def test_no_console_reaches_a_third_party_on_load():
    """The evidence console pulled webfonts from Google on every view of a page
    that displays participant transcripts: a stalled render behind any proxy
    that cannot reach it, and every researcher's and rater's IP and User-Agent
    disclosed to a third party in an IRB study."""
    for page in PAGES:
        src = _src(page)
        for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
            assert host not in src, f"{page.name} still loads from {host}"
        external = re.findall(r'<(?:link|script)[^>]+(?:href|src)="(https?://[^"]+)"', src)
        assert not external, f"{page.name} loads {external} from off-origin"


def test_the_click_only_controls_are_real_buttons():
    """Session rows, conversation tabs, transcript turns, timeline diamonds and
    encounter rows were <div>/<span> with a click listener: unreachable by Tab,
    deaf to Enter and Space, invisible to a screen reader. rater.html already
    built the equivalent controls as real buttons, which is why the rating
    console never had this problem."""
    ev = _src(EVIDENCE)
    for cls in ("sess", "tab", "turn"):
        assert re.search(rf'<button type="button" class="{cls}', ev), \
            f"evidence.html still renders .{cls} as a non-button"
    assert "createElement('button')" in ev, "the timeline diamonds are still divs"
    assert re.search(r'<button type="button" class="enc', _src(DIRECTOR)), \
        "director.html still renders encounter rows as non-buttons"


def test_the_landing_modal_manages_its_own_focus():
    """A participant arriving from CloudResearch who navigates by keyboard
    could not get into the format picker: focus stayed behind the scrim, Tab
    walked the page underneath, and nothing said the background was inert."""
    src = _src(LANDING)
    assert 'aria-modal="true"' in src
    assert "modalReturnFocus" in src, "focus is not restored when the dialog closes"
    assert "document.body.style.overflow = 'hidden'" in src, "the page behind still scrolls"
    assert "if (e.key !== 'Tab') return;" in src, "Tab is not trapped inside the dialog"


def test_the_participant_pages_agree_about_the_hazards_they_share():
    """Both pages load the same worklet and both play the same 16 kHz stream.
    The legacy page had none of v2's protections; whatever is true of one has
    to be true of the other, or the next fix lands on one page again."""
    for page in (V2, PARTICIPANT):
        src = _src(page)
        assert "audioCtx.resume()" in src, \
            f"{page.name} never resumes a suspended AudioContext"
        assert "workletNode.connect(" in src, \
            f"{page.name} leaves the capture worklet with no path to a destination"
        assert "makeAgentBuffer(" in src, \
            f"{page.name} still builds agent audio at a rate an engine may refuse"
        assert "isSecureContext" in src, \
            f"{page.name} reports a non-secure origin as a microphone permission problem"


def test_the_recorder_teardown_is_not_a_race():
    """The comment already said 'tracks stop after onstop fires'; the code
    stopped them three statements later, synchronously."""
    src = _src(V2)
    body = src[src.index("function endSession("):src.index("// ---------- debrief ----------")]
    assert "finishVideoRecording(() => {" in body, \
        "endSession no longer hands finishVideoRecording a teardown callback"
    # The two lines that destroy the recorder's inputs must not run here.
    outside = body[:body.index("finishVideoRecording(() => {")] + \
        body[body.index("intentionalClose = true;"):]
    assert "getTracks().forEach(t => t.stop())" not in outside, \
        "endSession stops the recorded tracks synchronously again"
    assert "audioCtx.close()" not in outside, \
        "endSession closes the AudioContext synchronously again"


# ---------------------------------------------------------------------------
# The harnesses.
# ---------------------------------------------------------------------------

STUB_JS = r"""/* A thin browser with a Web Audio graph, media tracks, a MediaRecorder whose
   flush is asynchronous (as every real one is) and a virtual clock.

   The graph is recorded as edges so a test can ask the question the Web Audio
   rendering model asks: is this node reachable from a destination? That is the
   whole of finding 1, and it cannot be asked of a stub that only counts calls.
*/
'use strict';
const vm = require('vm');

function makeClock() {
  let now = 0, nextId = 1;
  const timers = [];
  const api = {
    now: () => now,
    setTimeout(fn, ms) { const t = { id: nextId++, at: now + (ms || 0), fn }; timers.push(t); return t.id; },
    clearTimeout(id) { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
    // A real repeating interval: the researcher console's keepalive is one,
    // and a stub that swallows it cannot show the ALB timeout being answered.
    setInterval(fn, ms) {
      const period = ms || 1;
      const id = nextId++;
      const arm = (at) => timers.push({ id, at, fn: () => { fn(); arm(now + period); } });
      arm(now + period);
      return id;
    },
    clearInterval(id) { for (let i = timers.length - 1; i >= 0; i--) if (timers[i].id === id) timers.splice(i, 1); },
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
  const docListeners = {};
  function el(id) {
    const node = {
      id: id || '', style: {}, dataset: {}, children: [],
      textContent: '', innerHTML: '', value: '', disabled: false,
      className: '', scrollTop: 0, scrollHeight: 0, onclick: null, srcObject: null,
      classList: {
        _s: new Set(),
        add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
        contains(c) { return this._s.has(c); },
        toggle(c, on) { if (on === undefined) on = !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); },
      },
      listeners: {},
      addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
      removeEventListener() {},
      fire(ev, detail) { (this.listeners[ev] || []).slice().forEach(f => f(detail || { type: ev })); },
      appendChild(c) { this.children.push(c); return c; },
      removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); },
      remove() {}, focus() {}, blur() {},
      click() { this.fire('click'); },
      _q: new Map(),
      querySelector(sel) { if (!this._q.has(sel)) this._q.set(sel, el(sel)); return this._q.get(sel); },
      querySelectorAll: () => [],
      getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
      setAttribute() {}, getAttribute: () => null, insertAdjacentHTML() {},
      scrollIntoView() {}, closest: () => null,
      play: () => Promise.resolve(), pause() {},
      get firstChild() { return this.children[0] || null; },
    };
    return node;
  }
  const document = {
    getElementById(id) { if (!byId.has(id)) byId.set(id, el(id)); return byId.get(id); },
    createElement(tag) { const n = el(''); n.tagName = String(tag).toUpperCase(); return n; },
    createTextNode(t) { const n = el(''); n.textContent = t; return n; },
    querySelector(sel) { if (!byId.has(sel)) byId.set(sel, el(sel)); return byId.get(sel); },
    querySelectorAll: () => [],
    addEventListener(ev, fn) { (docListeners[ev] = docListeners[ev] || []).push(fn); },
    removeEventListener() {},
    body: el('body'), head: el('head'),
    hidden: false, visibilityState: 'visible', activeElement: null,
  };
  return {
    document, byId, docListeners,
    $: (id) => document.getElementById(id),
    fireDoc(ev) { (docListeners[ev] || []).slice().forEach(f => f({ type: ev })); },
  };
}

/* Web Audio, media tracks and a MediaRecorder. `cfg` is how a test says what
   this engine does: refuse a rate, refuse a camera, refuse to play. */
function makeMedia(cfg, clock) {
  cfg = cfg || {};
  const edges = [];          // [fromNode, toNode]
  const buffers = [];        // every createBuffer call, so a test can read the rate
  const contexts = [];
  const recorders = [];
  const streams = [];
  let seq = 0;

  function mkNode(kind, extra) {
    const n = Object.assign({ __kind: kind, __id: kind + (seq++) }, extra || {});
    n.connect = (t) => { edges.push([n, t]); return t; };
    n.disconnect = () => {};
    return n;
  }
  function mkTrack(kind) {
    return {
      kind, readyState: 'live', stopped: false, _on: {},
      stop() { this.stopped = true; this.readyState = 'ended'; },
      addEventListener(ev, fn) { (this._on[ev] = this._on[ev] || []).push(fn); },
      removeEventListener() {},
      endIt() { this.readyState = 'ended'; (this._on.ended || []).forEach(f => f({ type: 'ended' })); },
    };
  }
  function mkStream(tracks) {
    const s = { _t: (tracks || []).slice() };
    s.getTracks = () => s._t.slice();
    s.getAudioTracks = () => s._t.filter(t => t.kind === 'audio');
    s.getVideoTracks = () => s._t.filter(t => t.kind === 'video');
    s.addTrack = (t) => { s._t.push(t); };
    streams.push(s);
    return s;
  }

  class AudioContext {
    constructor() {
      this.state = cfg.startState || 'running';
      this.sampleRate = cfg.sampleRate || 48000;
      this.currentTime = 0;
      this.closed = false;
      this.destination = mkNode('destination');
      this.audioWorklet = cfg.noAudioWorklet ? undefined : {
        addModule: async () => { if (cfg.addModuleFails) throw new Error('module 404'); },
      };
      this._on = {};
      this.resumeCalls = 0;
      contexts.push(this);
    }
    addEventListener(ev, fn) { (this._on[ev] = this._on[ev] || []).push(fn); }
    removeEventListener() {}
    setState(s) { this.state = s; (this._on.statechange || []).forEach(f => f({ type: 'statechange' })); }
    createGain() { return mkNode('gain', { gain: { value: 1 } }); }
    createAnalyser() { return mkNode('analyser', { fftSize: 512, getByteTimeDomainData() {} }); }
    createMediaStreamSource() { return mkNode('micsource'); }
    createMediaStreamDestination() { return mkNode('msdest', { stream: mkStream([mkTrack('audio')]) }); }
    createBuffer(ch, len, rate) {
      if (cfg.refuseRate && rate === cfg.refuseRate) {
        const e = new Error('rate out of range'); e.name = 'NotSupportedError'; throw e;
      }
      buffers.push({ channels: ch, length: len, rate });
      const data = new Float32Array(len);
      return { numberOfChannels: ch, length: len, sampleRate: rate, duration: len / rate,
               copyToChannel() {}, getChannelData: () => data };
    }
    createBufferSource() {
      return mkNode('bufsrc', { buffer: null, onended: null, start() {}, stop() {} });
    }
    resume() { this.resumeCalls++; if (!cfg.resumeFails) this.state = 'running'; return Promise.resolve(); }
    close() { this.closed = true; this.state = 'closed'; return Promise.resolve(); }
  }

  class AudioWorkletNode {
    constructor() {
      this.__kind = 'worklet';
      this.port = { onmessage: null, postMessage() {} };
    }
    connect(t) { edges.push([this, t]); return t; }
    disconnect() {}
  }

  function AudioEl() {
    this.srcObject = null;
    this.paused = true;
    const self = this;
    this.play = () => {
      if (cfg.playRejects) { const e = new Error('play refused'); e.name = 'NotAllowedError'; return Promise.reject(e); }
      self.paused = false;
      return Promise.resolve();
    };
    this.pause = () => { self.paused = true; };
  }

  function MediaRecorder(stream, opts) {
    this.stream = stream;
    this.mimeType = (opts || {}).mimeType;
    this.state = 'inactive';
    this.ondataavailable = null; this.onstop = null; this.onerror = null;
    recorders.push(this);
    if (cfg.recorderThrows) throw new Error('MediaRecorder refused these tracks');
  }
  MediaRecorder.prototype.start = function () { this.state = 'recording'; };
  MediaRecorder.prototype.stop = function () {
    this.state = 'inactive';
    const self = this;
    // Asynchronous, like every real engine: a final dataavailable, then onstop.
    clock.setTimeout(() => {
      if (self.ondataavailable) self.ondataavailable({ data: { size: 4096 } });
      if (self.onstop) self.onstop();
    }, cfg.flushMs === undefined ? 50 : cfg.flushMs);
  };
  MediaRecorder.isTypeSupported = (t) =>
    (cfg.supportedMime || ['video/webm;codecs=vp8,opus', 'video/webm']).indexOf(t) >= 0;

  const getUserMedia = async (c) => {
    const wantsVideo = !!(c && c.video), wantsAudio = !!(c && c.audio);
    if (wantsVideo && !wantsAudio) {
      if (cfg.cameraError) { const e = new Error('camera'); e.name = cfg.cameraError; throw e; }
      return mkStream([mkTrack('video')]);
    }
    if (cfg.micError) { const e = new Error('mic'); e.name = cfg.micError; throw e; }
    const t = [mkTrack('audio')];
    if (wantsVideo && !cfg.cameraError) t.push(mkTrack('video'));
    return mkStream(t);
  };

  function reaches(from, to) {
    const seen = new Set(); const q = [from];
    while (q.length) {
      const cur = q.shift();
      if (cur === to) return true;
      if (seen.has(cur)) continue;
      seen.add(cur);
      edges.forEach(e => { if (e[0] === cur) q.push(e[1]); });
    }
    return false;
  }

  return { AudioContext, AudioWorkletNode, AudioEl, MediaRecorder, getUserMedia,
           edges, buffers, contexts, recorders, streams, reaches, mkStream, mkTrack };
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
      if (opts.signal) {
        if (opts.signal.aborted) { const e = new Error('aborted'); e.name = 'AbortError'; return reject(e); }
        opts.signal.addEventListener('abort', () => {
          const e = new Error('aborted'); e.name = 'AbortError'; reject(e);
        });
      }
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
    countOf(sub) { return calls.filter(c => c.url.includes(sub)).length; },
    urlsOf(sub) { return calls.filter(c => c.url.includes(sub)).map(c => c.url); },
  };
}

function makeContext(cfg, extra) {
  const clock = makeClock();
  const dom = makeDom();
  const net = makeFetch();
  const media = makeMedia(cfg, clock);
  const beacons = [];
  const sandbox = {
    console, JSON, Math, Date, Promise, Object, Array, String, Number, Boolean,
    Set, Map, RegExp, Error, TypeError, isNaN, isFinite, parseInt, parseFloat,
    encodeURIComponent, decodeURIComponent, URLSearchParams, AbortController,
    TextEncoder, TextDecoder, Uint8Array, Int16Array, Float32Array, ArrayBuffer,
    DataView, atob: (s) => s, btoa: (s) => s,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    // A page in a background tab: rAF never runs. Keeps the mic-level loop from
    // spinning the virtual clock, and is a state every engine can be in.
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
    fetch: net.fetch,
    document: dom.document,
    performance: { now: () => clock.now() },
    Blob: function Blob(parts, o) {
      this.size = (parts || []).reduce((n, p) => n + (p.size || 0), 0);
      this.type = (o || {}).type || '';
    },
    MediaStream: function MediaStream(tracks) {
      const s = media.mkStream(tracks || []);
      this.getVideoTracks = s.getVideoTracks;
      this.getAudioTracks = s.getAudioTracks;
      this.getTracks = s.getTracks;
      this.addTrack = s.addTrack;
    },
    AudioContext: media.AudioContext,
    webkitAudioContext: media.AudioContext,
    AudioWorkletNode: media.AudioWorkletNode,
    Audio: media.AudioEl,
    MediaRecorder: media.MediaRecorder,
    WebSocket: function WebSocket() {
      this.readyState = 1; this.listeners = {};
      this.close = () => {}; this.send = () => {};
      this.addEventListener = (ev, fn) => { (this.listeners[ev] = this.listeners[ev] || []).push(fn); };
    },
    localStorage: { _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; },
                    setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
    CSS: { escape: (s) => s },
    Event: class { constructor(t) { this.type = t; } },
    alert() {}, confirm: () => true, scrollTo() {},
  };
  sandbox.navigator = {
    mediaDevices: cfg && cfg.noMediaDevices ? undefined : { getUserMedia: media.getUserMedia },
    sendBeacon: (url) => { beacons.push(String(url)); return true; },
  };
  sandbox.isSecureContext = cfg && cfg.insecure ? false : true;
  const winListeners = {};
  sandbox.addEventListener = (ev, fn) => { (winListeners[ev] = winListeners[ev] || []).push(fn); };
  sandbox.removeEventListener = () => {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  Object.assign(sandbox, extra || {});
  return { ctx: vm.createContext(sandbox), sandbox, clock, dom, net, media, beacons,
           fire(ev) { (winListeners[ev] || []).forEach(fn => fn({ type: ev })); },
           run(code) { return vm.runInContext(code, sandbox); } };
}

function bootPage(page, cfg, extra) {
  const src = require('fs').readFileSync(page, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
  const b = makeContext(cfg, extra);
  vm.runInContext(src, b.ctx, { filename: require('path').basename(page) });
  return b;
}

module.exports = { makeContext, bootPage, vm };
"""


CAPTURE_HARNESS = r"""/* Drives static/v2.html's own startCapture and playPcmChunk against engines
   that refuse things Chrome does not: a camera, an <audio> play(), a 16 kHz
   AudioBuffer. Nothing is asserted by reading the file. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];

function boot(cfg) {
  const b = bootPage(PAGE, cfg, {
    location: { search: '?run=r_1', href: 'http://t/v2?run=r_1', reload() {}, replace() {} },
  });
  b.net.route([{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);
  return b;
}
const transcript = (b) =>
  b.dom.document.getElementById('transcript').children.map(c => c.textContent).join(' | ');

(async () => {
  // --- 1. the capture worklet must be reachable from a destination --------
  // An island node is one an engine is not obliged to render, and if process()
  // is never called then no PCM is ever posted, ws.send is never reached and no
  // character ever answers — while the level meter (a separate analyser) keeps
  // moving, so every visible signal says the microphone is working.
  {
    const b = boot({});
    await b.ctx.startCapture();
    const w = b.run('workletNode');
    const ctx = b.run('audioCtx');
    assert(w, 'no worklet node was created');
    assert(b.media.reaches(w, ctx.destination),
      'the capture worklet has no path to a destination: an engine that renders ' +
      'only what a destination pulls will never call process() on it');
    // And the sink must be silent: a monitor path would feed the mic back.
    const sink = b.media.edges.filter(e => e[0] === w).map(e => e[1])
      .find(n => n.__kind === 'gain');
    assert(sink, 'the worklet is connected to something that is not a gain node');
    assert.strictEqual(sink.gain.value, 0, 'the worklet sink is audible: that is a feedback path');
  }

  // --- 2. a refused <audio> play() must not mean a silent encounter --------
  {
    const b = boot({ playRejects: true });
    await b.ctx.startCapture();
    await b.clock.advance(10);
    const ctx = b.run('audioCtx');
    const playDest = b.run('playDest');
    b.ctx.playPcmChunk(new ArrayBuffer(320));
    const src = b.media.edges.map(e => e[0]).find(n => n.__kind === 'bufsrc');
    assert(src, 'no buffer source was created for the agent chunk');
    const targets = b.media.edges.filter(e => e[0] === src).map(e => e[1]);
    assert(targets.indexOf(ctx.destination) >= 0,
      'the element refused to play and nothing fell back to the context destination: ' +
      'the participant hears silence while captions scroll');
    assert(targets.indexOf(playDest) < 0,
      'agent audio is still routed only through an element that will not play it');
  }

  // --- and where the element DOES play, the echo-canceller path is kept ----
  {
    const b = boot({});
    await b.ctx.startCapture();
    await b.clock.advance(10);
    const playDest = b.run('playDest');
    b.ctx.playPcmChunk(new ArrayBuffer(320));
    const src = b.media.edges.map(e => e[0]).find(n => n.__kind === 'bufsrc');
    const targets = b.media.edges.filter(e => e[0] === src).map(e => e[1]);
    assert(targets.indexOf(playDest) >= 0,
      'a working element was abandoned, losing the echo canceller reference');
  }

  // --- 3. an engine that refuses a 16 kHz AudioBuffer ---------------------
  {
    const b = boot({ refuseRate: 16000, sampleRate: 44100 });
    await b.ctx.startCapture();
    b.ctx.playPcmChunk(new ArrayBuffer(3200));   // 1600 samples at 16 kHz
    b.ctx.playPcmChunk(new ArrayBuffer(3200));   // and the next one still plays
    const made = b.media.buffers;
    assert(made.length >= 2, 'the second chunk threw: one refused rate cost the encounter');
    made.forEach(m => assert.strictEqual(m.rate, 44100,
      'a buffer was built at a rate this engine refuses: ' + m.rate));
    // Resampled up, not truncated: 1600 samples at 16 kHz is 4410 at 44.1 kHz.
    assert.strictEqual(made[0].length, 4410,
      'the resample lost or duplicated time: ' + made[0].length);
  }

  // --- 4. a camera that is missing, denied or busy -------------------------
  for (const name of ['NotFoundError', 'NotAllowedError', 'NotReadableError']) {
    const b = boot({ cameraError: name });
    await b.ctx.startCapture();
    const stream = b.run('mediaStream');
    assert(stream.getAudioTracks().length === 1,
      'a camera failure took the microphone with it: the encounter is lost, not degraded');
    assert(stream.getVideoTracks().length === 0, 'a refused camera produced a video track');
    assert(/not be captured on camera/.test(transcript(b)),
      'the participant consented to being filmed and was not told they were not: ' + transcript(b));
    assert.strictEqual(b.run('cameraFailReason'), name,
      'the camera error was swallowed, so nothing downstream can say which it was');
    // And the absence is reported, or it is invisible to server/video.py.
    b.run("sessionId = 's_test'; participantId = 'p_test';");
    b.ctx.finishVideoRecording();
    const beacon = b.beacons.find(u => u.indexOf('no_camera') >= 0);
    assert(beacon, 'an encounter with no camera generated no event at all: it cannot be told ' +
      'apart from one whose recording was made and lost');
    assert(beacon.indexOf('no_camera%3A' + name) >= 0 || beacon.indexOf('no_camera:' + name) >= 0,
      'the beacon did not carry which failure it was: ' + beacon);
  }

  // --- 5. a browser that cannot record video at all ------------------------
  {
    const b = boot({ supportedMime: [] });
    await b.ctx.startCapture();
    assert(/cannot record video/.test(transcript(b)), 'no notice: ' + transcript(b));
    assert.strictEqual(b.run('videoMime'), null);
  }

  // --- 6. a MediaRecorder that will not construct --------------------------
  {
    const b = boot({ recorderThrows: true });
    await b.ctx.startCapture();
    assert.strictEqual(b.run('videoRecorder'), null);
    assert(/not be captured on camera/.test(transcript(b)),
      'a recorder that refused to start was swallowed: ' + transcript(b));
  }

  // --- 7. a camera lost mid-encounter -------------------------------------
  {
    const b = boot({});
    await b.ctx.startCapture();
    b.run('started = true;');
    const cam = b.run('mediaStream').getVideoTracks()[0];
    cam.endIt();
    assert(/stopped part way through/.test(transcript(b)),
      'a camera that died mid-encounter changed nothing on screen: ' + transcript(b));
  }

  // --- 8. a context WebKit parks, and does not restart -----------------
  {
    const b = boot({});
    await b.ctx.startCapture();
    b.run('started = true;');
    const ctx = b.run('audioCtx');
    const before = ctx.resumeCalls;
    ctx.setState('interrupted');     // the WebKit-only fourth state
    assert(ctx.resumeCalls > before, 'nothing tried to bring the context back');
  }
  {
    // ...and one that will not come back is said out loud rather than left
    // showing "Listening, speak any time" to a page that is deaf and mute.
    const b = boot({ resumeFails: true });
    await b.ctx.startCapture();
    b.run('started = true;');
    b.run('audioCtx').setState('interrupted');
    await b.clock.advance(3000);
    assert(/paused this page/.test(transcript(b)) || /connection dropped/i.test(transcript(b)),
      'an interruption that never resolved was never mentioned: ' + transcript(b));
  }

  console.log('CAPTURE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


TEARDOWN_HARNESS = r"""/* endSession stops the camera and closes the AudioContext. Both are inputs to
   a MediaRecorder that has not finished flushing, and on Safari the fragmented
   MP4 is not merely short without its finalization, it is unplayable. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];

function boot(cfg) {
  const b = bootPage(PAGE, cfg, {
    location: { search: '?run=r_1', href: 'http://t/v2?run=r_1', reload() {}, replace() {} },
  });
  b.net.route([{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);
  return b;
}

(async () => {
  const b = boot({ flushMs: 200 });
  await b.ctx.startCapture();
  b.run("sessionId = 's_test'; participantId = 'p_test'; started = true;");
  const stream = b.run('mediaStream');
  const ctx = b.run('audioCtx');
  const cam = stream.getVideoTracks()[0];
  assert(cam, 'no camera track to race');

  b.net.route([
    { match: '/video-upload-url', fn: () => b.net.res(200, { url: 'https://s3.invalid/o' }) },
    { match: 'https://s3.invalid/', fn: () => b.net.res(200, {}) },
    { match: '/video-uploaded', fn: () => b.net.res(200, { ok: true, bytes: 4096 }) },
  ]);

  b.ctx.endSession({ debrief: false });

  // Synchronously after endSession the recorder has been asked to stop and has
  // not yet flushed. Its inputs must still be alive.
  assert.strictEqual(cam.stopped, false,
    'the camera track was stopped while the recorder was still flushing: the last chunk, ' +
    'and on Safari the MP4 finalization, race a teardown that destroys the source');
  assert.strictEqual(ctx.closed, false,
    'the AudioContext was closed while the recorder was still flushing: recDest is the ' +
    'other half of the recorded stream');

  // After the flush, nothing is left running: the camera light goes out.
  await b.clock.advance(5000);
  assert.strictEqual(cam.stopped, true, 'the camera was never released');
  assert.strictEqual(ctx.closed, true, 'the AudioContext was never closed');
  assert(b.net.countOf('https://s3.invalid/') === 1, 'the recording was not uploaded');

  // And a recorder that accepts stop() but never fires onstop must not hold the
  // camera on for the rest of the page's life.
  {
    const c = boot({ flushMs: 10 ** 9 });
    await c.ctx.startCapture();
    c.run("sessionId = 's_test2'; participantId = 'p_test'; started = true;");
    const cam2 = c.run('mediaStream').getVideoTracks()[0];
    c.net.route([
      { match: '/video-upload-url', fn: () => c.net.res(200, { url: 'https://s3.invalid/o' }) },
      { match: 'https://s3.invalid/', fn: () => c.net.res(200, {}) },
      { match: '/video-uploaded', fn: () => c.net.res(200, { ok: true }) },
    ]);
    c.ctx.endSession({ debrief: false });
    await c.clock.advance(30000);
    assert.strictEqual(cam2.stopped, true,
      'a recorder that never fired onstop left the camera running');
  }

  console.log('TEARDOWN OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


FAILURE_MESSAGE_HARNESS = r"""/* What the participant is told when capture will not start. "Please allow
   microphone access" is a dead end for every failure that is not a denial, and
   the page offers a Start button that will fail identically forever. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];

async function attempt(cfg) {
  const b = bootPage(PAGE, cfg, {
    location: { search: '?run=r_1', href: 'http://t/v2?run=r_1', reload() {}, replace() {} },
  });
  b.net.route([{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);
  b.run("participantId = 'p_test'; consentPending = false;");
  await b.ctx.startSession();
  await b.clock.advance(10);
  return b.dom.document.getElementById('transcript').children.map(c => c.textContent).join(' | ');
}

(async () => {
  const denied = await attempt({ micError: 'NotAllowedError' });
  assert(/allow microphone access/i.test(denied), 'a real denial lost its wording: ' + denied);

  const busy = await attempt({ micError: 'NotReadableError' });
  assert(/no other app|using it/i.test(busy),
    'a microphone held by another application was reported as a permission problem: ' + busy);
  assert(!/allow microphone access/i.test(busy), 'still telling them to allow what they allowed');

  const missing = await attempt({ micError: 'NotFoundError' });
  assert(/plugged in|find a working microphone/i.test(missing),
    'a missing microphone was reported as a permission problem: ' + missing);

  const noWorklet = await attempt({ noAudioWorklet: true });
  assert(/cannot run the audio/i.test(noWorklet),
    'a browser with no AudioWorklet was told to grant a permission: ' + noWorklet);

  const badModule = await attempt({ addModuleFails: true });
  assert(/cannot run the audio/i.test(badModule),
    'a worklet module that would not load was reported as a denial: ' + badModule);

  const insecure = await attempt({ insecure: true });
  assert(/secure connection/i.test(insecure),
    'a non-secure origin — a deployment mistake the participant cannot fix — was reported ' +
    'as a microphone permission problem: ' + insecure);

  const noDevices = await attempt({ noMediaDevices: true });
  assert(/cannot run the audio|secure connection/i.test(noDevices),
    'a missing navigator.mediaDevices was reported as a denial: ' + noDevices);

  console.log('MESSAGES OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


RATER_HARNESS = r"""/* The rating console: a recording this browser cannot play, the open-ended
   boxes across a sitting, and the dwell timer across a tab switch. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];
const ITEMS = [1, 2, 3].map(i => ({ id: 'i' + i, text: 'item ' + i }));
const OK_MEDIA = { video_url: 'https://s3.invalid/webcam.webm', video_available: true,
                   video_status: 'ok', expires_in: 3600, note: null };

function boot() {
  const b = bootPage(PAGE, {}, {
    location: { search: '?token=not-a-token', href: 'http://t/rate', reload() {} },
  });
  b.net.route([{ match: '/api/rater/', fn: () => b.net.res(200, {}) }]);
  return b;
}
const $ = (b, id) => b.dom.document.getElementById(id);

async function open(b, packet) {
  b.net.route([{ match: '/api/rater/packet/', fn: () => b.net.res(200, packet) }]);
  await b.ctx.openAssignment(packet.assignment_id);
  await b.clock.flush();
}

(async () => {
  // --- the open-ended boxes come back on the next encounter ---------------
  // lockDown() disabled them and openAssignment cleared the values without
  // clearing .disabled, so from encounter 2 of a rater's sitting onwards both
  // boxes were dead grey rectangles and every rating posted with empty
  // open_ended. Nobody got an error at either end.
  {
    const b = boot();
    await open(b, { assignment_id: 'a_1', status: 'submitted', items: ITEMS, media: OK_MEDIA });
    assert.strictEqual($(b, 'obBetter').disabled, true, 'a submitted rating stayed editable');
    await open(b, { assignment_id: 'a_2', status: 'assigned', items: ITEMS, media: OK_MEDIA });
    assert.strictEqual($(b, 'obBetter').disabled, false,
      '"What could the participant have done better?" is still dead on the second encounter');
    assert.strictEqual($(b, 'obNotable').disabled, false,
      '"Anything else notable?" is still dead on the second encounter');
    assert.strictEqual($(b, 'submitBtn').textContent, 'Submit rating',
      'the button still says Submitted on a fresh encounter');
  }

  // --- a recording this browser cannot decode -----------------------------
  {
    const b = boot();
    await open(b, { assignment_id: 'a_1', status: 'assigned', items: ITEMS, media: OK_MEDIA });
    const v = $(b, 'vid');
    v.error = { code: 4 };               // MEDIA_ERR_SRC_NOT_SUPPORTED
    v.fire('error');                     // first failure: re-mint the link and retry
    await b.clock.flush();
    assert.strictEqual($(b, 'submitBtn').disabled, false,
      'one error blocked the rating before the cheap, recoverable cause was tried');
    v.fire('error');                     // the fresh URL fails the same way
    await b.clock.flush();
    assert.strictEqual($(b, 'submitBtn').disabled, true,
      'an undecodable recording was rated around: this produces a gold label made without ' +
      'the artefact and indistinguishable from a good one');
    assert(/cannot be played in this browser/i.test($(b, 'videoSlot').innerHTML),
      'the rater was shown a black box with no explanation: ' + $(b, 'videoSlot').innerHTML);
    assert(/Chrome/.test($(b, 'videoSlot').innerHTML),
      'the rater was told it failed but not what to do about it');
    assert(/Do not rate/i.test($(b, 'submitMsg').textContent),
      'a rater silently blocked from submitting is worse than one told to switch browsers: ' +
      $(b, 'submitMsg').textContent);
  }

  // --- an expired playback link is re-minted, not reported as no video ----
  {
    const b = boot();
    await open(b, { assignment_id: 'a_1', status: 'assigned', items: ITEMS, media: OK_MEDIA });
    const before = b.net.countOf('/api/rater/packet/');
    const v = $(b, 'vid');
    v.error = { code: 2 };               // MEDIA_ERR_NETWORK, which is what a 403 looks like
    v.fire('error');
    await b.clock.flush();
    assert(b.net.countOf('/api/rater/packet/') > before,
      'nothing re-fetched the packet after the presigned URL aged out, so an hour into a ' +
      'sitting every encounter reads as "this one has no video"');
    assert.strictEqual($(b, 'submitBtn').disabled, false,
      'a link that had merely expired blocked the rating');
  }

  // --- a recording with no duration index cannot be seeked ----------------
  {
    const b = boot();
    await open(b, { assignment_id: 'a_1', status: 'assigned', items: ITEMS, media: OK_MEDIA });
    const v = $(b, 'vid');
    v.duration = Infinity;               // raw MediaRecorder output, which is all of them
    v.fire('loadedmetadata');
    assert(/does not\s+work reliably|no duration index/i.test($(b, 'seekHint').textContent),
      'the console still promises click-to-seek on a file that cannot honour it: ' +
      $(b, 'seekHint').textContent);
  }

  // --- the dwell timer must not credit time spent in another tab ----------
  {
    const b = boot();
    await open(b, { assignment_id: 'a_1', status: 'assigned', items: ITEMS, media: OK_MEDIA });
    // performance.now() must be non-zero when the accumulator is seeded: the
    // page uses `lastTick` truthiness as its "we are not currently visible"
    // marker, which is the whole of the fix under test.
    await b.clock.advance(1000);
    b.run('activeMs = 0; lastTick = performance.now();');
    await b.clock.advance(1000);
    b.ctx.tick();
    const worked = b.run('activeMs');
    assert(worked >= 900 && worked <= 1100, 'a second of real work was not counted: ' + worked);

    b.dom.document.hidden = true;
    b.dom.fireDoc('visibilitychange');
    await b.clock.advance(5 * 60 * 1000);      // five minutes in another tab
    b.dom.document.hidden = false;
    b.dom.fireDoc('visibilitychange');
    await b.clock.advance(1000);
    b.ctx.tick();

    const total = b.run('activeMs');
    assert(total < 10000,
      'the two-minute anti-skim gate is defeated by switching tabs, and the `seconds` the ' +
      'console posts as the evidence for that gate is inflated with it: ' + total);
    assert(total >= 1900,
      'real foreground time stopped being counted: ' + total);
  }

  console.log('RATER COMPAT OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


RESEARCHER_HARNESS = r"""/* The live researcher socket: a keepalive, a reconnect, and a way back that is
   not a page reload in the middle of an encounter. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];

function boot() {
  const sockets = [];
  const b = bootPage(PAGE, {}, {
    location: { search: '?key=k', href: 'http://t/researcher', protocol: 'http:', host: 't',
                reload() {} },
  });
  // Replace the WebSocket the page will construct with one this test can drive.
  b.run(`
    __sockets = [];
    WebSocket = function (url) {
      this.url = url; this.readyState = 0; this.sent = []; this.listeners = {};
      this.addEventListener = (ev, fn) => { (this.listeners[ev] = this.listeners[ev] || []).push(fn); };
      this.send = (m) => { this.sent.push(m); };
      this.close = () => {};
      this.open = () => { this.readyState = 1; (this.listeners.open || []).forEach(f => f({})); };
      this.drop = () => { this.readyState = 3; (this.listeners.close || []).forEach(f => f({})); };
      __sockets.push(this);
    };
  `);
  b.net.route([{ match: '/api/', fn: () => b.net.res(200, []) }]);
  return b;
}

(async () => {
  const b = boot();
  b.run("sessionsCache = [{ id: 's_1', status: 'active', title: 't', turn_count: 0 }];");
  b.net.route([
    { match: '/files', fn: () => b.net.res(200, { files: [] }) },
    { match: '/score', fn: () => b.net.res(200, {}) },
    { match: '/api/', fn: () => b.net.res(200, []) },
  ]);
  b.ctx.connect('s_1');
  await b.clock.flush();

  const socks = b.run('__sockets');
  assert.strictEqual(socks.length, 1, 'no socket was opened');
  socks[0].open();

  // --- keepalive ----------------------------------------------------------
  // The ALB in front of this socket has a 120-second idle timeout and neither
  // side pinged. A researcher who connects at launch and waits while the
  // participant works through consent, instructions and the microphone prompt
  // is easily two silent minutes.
  await b.clock.advance(121000);
  assert(socks[0].sent.length > 0,
    'nothing is sent on an idle socket, so two silent minutes close it at the load balancer');
  assert(/"type":"ping"/.test(socks[0].sent[0]), 'the keepalive is not a ping: ' + socks[0].sent[0]);

  // --- an unexpected drop is retried, not declared a closed session -------
  socks[0].drop();
  await b.clock.advance(2000);
  assert(b.run('__sockets').length > 1,
    'a dropped socket on a live session was declared closed with no attempt to reconnect: ' +
    'the steering controls vanish from a session that is still running');
  assert.strictEqual(b.run('currentSessionStatus'), 'active',
    'a transient drop marked a running session closed');

  // --- and when it really is gone, there is a way back that is not a reload
  const live = b.run('__sockets');
  for (let i = 0; i < 8; i++) { live[live.length - 1].drop(); await b.clock.advance(20000); }
  assert.strictEqual(b.dom.document.getElementById('reconnectRow').style.display, 'flex',
    'the only way back to a live session is still a full page reload mid-encounter');

  console.log('RESEARCHER OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harnesses need it")
    return node


def _run(tmp_path, harness_src, page):
    (tmp_path / "stub.js").write_text(STUB_JS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(harness_src, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(page)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_capture_survives_engines_that_refuse_what_chrome_allows(tmp_path):
    """Findings 1, 2, 5, 6, 9, 10: the worklet sink, the refused <audio>, the
    refused AudioBuffer rate, the camera that is missing or busy, the recorder
    that will not start, the camera lost mid-encounter and the parked context."""
    assert "CAPTURE OK" in _run(tmp_path, CAPTURE_HARNESS, V2)


def test_the_recording_outlives_its_own_teardown(tmp_path):
    """Finding 4: endSession stopped the recorder's input tracks and closed the
    AudioContext while MediaRecorder still owed a final chunk."""
    assert "TEARDOWN OK" in _run(tmp_path, TEARDOWN_HARNESS, V2)


def test_a_capture_failure_says_which_failure_it_was(tmp_path):
    """Finding 7: one sentence for every failure, and it was the wrong sentence
    for most of them."""
    assert "MESSAGES OK" in _run(tmp_path, FAILURE_MESSAGE_HARNESS, V2)


def test_the_rating_console_across_browsers_and_across_a_sitting(tmp_path):
    """The two console CRITICALs (the permanently disabled open-ended boxes and
    the silent undecodable recording), plus the expiring playback link, the
    unseekable container and the dwell timer that credited another tab."""
    assert "RATER COMPAT OK" in _run(tmp_path, RATER_HARNESS, RATER)


def test_the_live_researcher_socket_keeps_itself_alive(tmp_path):
    """No keepalive against a 120s ALB idle timeout, no reconnect, and no way
    back but a page reload in the middle of an encounter."""
    assert "RESEARCHER OK" in _run(tmp_path, RESEARCHER_HARNESS, RESEARCHER)
