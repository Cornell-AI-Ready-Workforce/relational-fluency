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
10. **An encounter with no camera is not reported as a lost recording.** The
   participant page POSTed the video-uploaded confirm endpoint when there was no
   recorder at all, and that endpoint's event means the opposite: "this was
   recorded and could not be stored". Every denied or busy camera became an
   encounter the rating console refuses to rate, plus a storage fault reported
   against a bucket that lost nothing. The reasons it does report are encoded so
   the server's own filter keeps them.
11. **A silent <audio> element is detected.** The fallback existed for a WebKit
   that will not sound a WebAudio MediaStream through an element, and the only
   signal read was `paused` — which such an element does not set.
12. **The rating console's playback survives a long sitting.** It used to be a
   presigned S3 GET minted for an hour, so an hour into a queue every remaining
   encounter began reading as "this one has no video" — and the machinery that
   chased that (a pre-emptive expiry swap, a re-mint on the error path, a rate
   limit and a Retry button) was a one-way latch that consumed the error path
   with it. The URL is this application's own route now, addressed by
   assignment id: it does not age, so the guarantee is kept by nothing at all
   happening, which is what part 12 asserts. Alongside it, the failures that
   were never about expiry: the seek probe disabled click-to-seek for every
   encounter in the wave, the stuck-load watchdog fired on any iPad, and the
   no-audio note fired on Safari.

Run from the repo root:

    python -m pytest tests
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
V2 = STATIC / "v2.html"
RESEARCHER = STATIC / "researcher.html"
EVIDENCE = STATIC / "evidence.html"
DIRECTOR = STATIC / "director.html"
LANDING = STATIC / "landing.html"

PAGES = [V2, RESEARCHER, EVIDENCE, DIRECTOR, LANDING]


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
    for page in (V2, LANDING):
        src = _src(page)
        plain = len(re.findall(r"(?<!-)\bbackdrop-filter\s*:", src))
        prefixed = len(re.findall(r"-webkit-backdrop-filter\s*:", src))
        assert plain and prefixed >= plain, (
            f"{page.name} has {plain} backdrop-filter declarations and "
            f"{prefixed} -webkit- ones")


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


def test_the_recorder_teardown_is_not_a_race():
    """The comment already said 'tracks stop after onstop fires'; the code
    stopped them three statements later, synchronously."""
    src = _src(V2)
    body = src[src.index("function endSession("):src.index("$('startBtn').addEventListener('click', startSession);")]
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
    let startedAt = null;
    // A real <audio> playing a live MediaStream advances currentTime whether or
    // not the stream carries sound. `silentElement` is the WebKit case the page
    // cares about and the reason this property exists here at all: the element
    // accepts the srcObject, reports itself playing, and makes nothing audible
    // — so `paused` is false and the clock never moves.
    Object.defineProperty(this, 'currentTime', {
      get() {
        if (startedAt == null) return 0;
        return cfg.silentElement ? 0 : (clock.now() - startedAt) / 1000;
      },
    });
    this.play = () => {
      if (cfg.playRejects) { const e = new Error('play refused'); e.name = 'NotAllowedError'; return Promise.reject(e); }
      self.paused = false;
      startedAt = clock.now();
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

  // --- 2b. an element that reports itself playing and makes no sound -------
  // The case startCapture's own comment names — WebKit will not always make a
  // WebAudio MediaStream audible through an <audio> — and the one the probe
  // could not see: such an element is not paused, so `paused === false` said
  // everything was fine while the participant heard nothing for a whole paid
  // encounter. The element's clock is the signal that separates them.
  {
    const b = boot({ silentElement: true });
    await b.ctx.startCapture();
    await b.clock.advance(10);
    const ctx = b.run('audioCtx');
    b.ctx.playPcmChunk(new ArrayBuffer(320));
    assert.strictEqual(b.run('playElUsable'), true,
      'the fallback fired before the probe had waited: one slow first chunk would cost the ' +
      'echo canceller for the rest of the encounter');
    await b.clock.advance(2000);
    assert.strictEqual(b.run('playElUsable'), false,
      'an element that reports itself playing and makes no sound was never noticed: the ' +
      'participant hears silence for the whole encounter while the captions scroll');
    b.ctx.playPcmChunk(new ArrayBuffer(320));
    const later = b.media.edges.map(e => e[0]).filter(n => n.__kind === 'bufsrc').pop();
    const targets = b.media.edges.filter(e => e[0] === later).map(e => e[1]);
    assert(targets.indexOf(ctx.destination) >= 0,
      'later chunks are still routed only through the element that will not sound them');
    assert(/would not play the conversation audio/.test(transcript(b)),
      'the switch happened silently, so nobody can tell this encounter from a good one: ' +
      transcript(b));
  }

  // ...and an engine whose element really is playing must NOT be switched: the
  // fallback costs Chrome's echo-canceller reference, which is a real cost.
  {
    const b = boot({});
    await b.ctx.startCapture();
    await b.clock.advance(10);
    b.ctx.playPcmChunk(new ArrayBuffer(320));
    await b.clock.advance(2000);
    assert.strictEqual(b.run('playElUsable'), true,
      'a healthy element was abandoned, and with it the echo canceller: agent speech now ' +
      'bleeds back through the open mic and is transcribed as the participant');
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
    // And the absence is reported AS an absence. A plain confirm means the
    // opposite — server/app.py writes a video_uploaded event with status
    // "failed" and server/rater_packet.py reads that as "this encounter WAS
    // recorded and could not be stored, do not rate it" — so a page that
    // reports a denied camera on that path turns every one of them into a paid
    // encounter no rater may score, plus a storage fault against a bucket that
    // lost nothing. `?no_camera=` is the parameter that says which fact this
    // is; the Python side drives the real route with the URL built here.
    b.run("sessionId = 's_1772460300_44c9a2'; participantId = 'p_1772460300_4327ae';");
    b.ctx.finishVideoRecording();
    const beacon = b.beacons.find(u => u.indexOf('/video-uploaded') >= 0);
    assert(beacon, 'an encounter with no camera generated no report at all, so nothing ' +
      'downstream can say why it has no video');
    assert(/[?&]no_camera=/.test(beacon),
      'the absence was reported on the path that means "recorded and lost": ' + beacon);
    assert(!/client_error=/.test(beacon), 'and it carried a client_error too: ' + beacon);
    assert(beacon.indexOf('no_camera=' + name) >= 0,
      'the beacon did not carry which failure it was: ' + beacon);
    console.log('NOCAMERA_BEACON ' + beacon);
  }

  // --- 4b. a recorder that ran, collected bytes, and died ------------------
  // The other half of that distinction, and the half the confirm event is for:
  // this encounter WAS captured, the bytes die with the page because the
  // recorder never reached the stop that builds the blob, and Phase 2 has to be
  // able to tell it from an encounter nobody pointed a camera at.
  {
    const b = boot({});
    await b.ctx.startCapture();
    b.run("sessionId = 's_1772460300_44c9a2'; participantId = 'p_1772460300_4327ae';");
    const rec = b.media.recorders[0];
    assert(rec, 'no recorder was constructed for a working camera');
    rec.ondataavailable({ data: { size: 4096 } });          // two seconds of video
    rec.onerror({ error: { name: 'NotSupportedError' } });
    rec.state = 'inactive';                                  // what a real engine does next
    b.ctx.finishVideoRecording();
    const beacon = b.beacons.find(u => u.indexOf('/video-uploaded') >= 0);
    assert(beacon, 'a recording that was made and then dropped left no trace at all');
    assert(!/no_camera=/.test(beacon),
      'a recording that WAS made was reported as an encounter that had no camera: ' + beacon);
    const hint = decodeURIComponent((/client_error=([^&]*)/.exec(beacon) || [])[1] || '');
    // The separator matters: server/app.py keeps client_error only when it
    // matches [A-Za-z0-9_.-], so a colon here is a reason nobody ever reads.
    assert.strictEqual(hint, 'recorder_error.NotSupportedError',
      'the reason will not survive the server filter: ' + hint);
    assert(/stopped part way through/.test(b.run('videoNotice')),
      'the participant is told in the transcript but not on the screen that follows: ' +
      b.run('videoNotice'));
    console.log('LOST_BEACON ' + beacon);
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

  // --- 9. every reason this page can report, as the page encodes it --------
  // Printed rather than asserted here: the grammar these have to satisfy
  // belongs to server/app.py, so the Python side reads the filter out of the
  // server and applies it to exactly these strings.
  {
    const b = boot({});
    const reasons = [
      'recorder_error:NotSupportedError', 'recorder_failed:NotSupportedError',
      'recorder_lost', 'track_ended', 'no_supported_mime', 'NotAllowedError',
      'NotReadableError', 'put_http_403', 'presign_http_503', 'timeout', 'network',
      'confirm_no_object', 'confirm_http_500', 'unexpected',
      'recorder_error:' + 'LongEngineSuppliedName'.repeat(4),
    ];
    console.log('TOKENS ' + JSON.stringify(reasons.map(r => b.ctx.clientErrorToken(r))));
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

  b.ctx.endSession();

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
    c.ctx.endSession();
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

  // --- and the offer belongs to the session it was made about -------------
  // connect() returns early for a session that is not active — no socket, and
  // therefore nothing that hides this row. So switching from a session whose
  // socket had dropped to a closed one left "The live connection to this
  // session ended. [Reconnect]" on screen over a transcript from a different
  // encounter, offering to reconnect a session that ended hours ago.
  b.run("sessionsCache = [{ id: 's_1', status: 'active', title: 't', turn_count: 0 }," +
        "                 { id: 's_2', status: 'closed', title: 't2', turn_count: 0 }];");
  b.ctx.connect('s_2');
  await b.clock.flush();
  assert.strictEqual(b.dom.document.getElementById('reconnectRow').style.display, 'none',
    'the reconnect offer for the previous session is still on screen, describing a session ' +
    'the researcher is no longer looking at');

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
    # encoding pinned rather than left to the machine's locale: node emits UTF-8
    # everywhere, these pages contain characters cp1252 cannot represent, and a
    # harness failure decoded through the wrong codec reaches a Windows
    # contributor as mojibake at exactly the moment they need to read it.
    proc = subprocess.run([_node(), str(harness), str(page)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_capture_survives_engines_that_refuse_what_chrome_allows(tmp_path):
    """Findings 1, 2, 5, 6, 9, 10: the worklet sink, the refused <audio>, the
    refused AudioBuffer rate, the camera that is missing or busy, the recorder
    that will not start, the camera lost mid-encounter and the parked context."""
    assert "CAPTURE OK" in _run(tmp_path, CAPTURE_HARNESS, V2)


def _beacon(out: str, marker: str) -> str:
    """The last URL the harness printed under `marker`.

    Last, not first: the camera block runs once per failure name, and the one
    this test names its expected reason after is the one it ended on.
    """
    lines = [ln for ln in out.splitlines() if ln.startswith(marker + " ")]
    assert lines, f"the harness printed no {marker}:\n{out}"
    return lines[-1][len(marker) + 1:].strip()


def test_what_the_page_reports_about_a_camera_is_what_the_server_records(tmp_path, monkeypatch):
    """The two halves of one contract, joined: the URLs the page actually builds
    are POSTed to the route that actually exists.

    This is the join that was missing. The page composed
    `client_error=no_camera:<reason>` and the test asserted only that the beacon
    said so — while the route's own filter rejected the colon and dropped the
    reason, and the event it wrote said the encounter WAS recorded and the
    recording was lost, which blocks the rating outright. Both sides passed
    their own tests and the pair was wrong.

    No AWS call: the absence branch must not touch S3 at all, so the stub client
    raises the real NoCredentialsError if anything asks it anything.
    """
    out = _run(tmp_path, CAPTURE_HARNESS, V2)
    absence_url = _beacon(out, "NOCAMERA_BEACON")
    lost_url = _beacon(out, "LOST_BEACON")

    pytest.importorskip("fastapi")
    from botocore.exceptions import NoCredentialsError
    from fastapi.testclient import TestClient
    from server import app as appmod
    from server import video

    session_id, owner = "s_1772460300_44c9a2", "p_1772460300_4327ae"
    root = tmp_path / "sessions"
    sdir = root / session_id
    sdir.mkdir(parents=True)
    (sdir / "manifest.json").write_text(
        json.dumps({"session_id": session_id, "participant_id": owner,
                    "scenario": "conflict", "status": "closed"}), encoding="utf-8")
    (sdir / "events.jsonl").write_text("", encoding="utf-8")

    class NoCredentials:
        def __getattr__(self, _name):
            def call(*a, **kw):
                raise NoCredentialsError()
            return call

    monkeypatch.setattr(appmod, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "_s3", NoCredentials())
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS", list(appmod.ALLOWED_HOSTS) + ["testserver"])
    client = TestClient(appmod.app, raise_server_exceptions=False)

    def written():
        return [json.loads(ln) for ln
                in (sdir / "events.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]

    # 1. the encounter that never had a camera
    res = client.post(absence_url)
    assert res.status_code == 200, f"{absence_url} -> {res.status_code} {res.text}"
    assert res.json().get("status") == "absent", res.text
    kinds = [e.get("type") for e in written()]
    assert "video_uploaded" not in kinds, (
        "the page's own no-camera report is recorded as a recording that was made and lost, "
        "which is the one state the rating console refuses to rate")
    absent = [e for e in written() if e.get("type") == "video_absent"]
    assert len(absent) == 1, kinds
    assert absent[0].get("reason") == "NotReadableError", (
        f"the reason the camera never ran did not survive the round trip: {absent[0]}")

    # 2. and the recording that was made and then dropped
    res = client.post(lost_url)
    assert res.status_code in (200, 503), res.text
    uploaded = [e for e in written() if e.get("type") == "video_uploaded"]
    assert len(uploaded) == 1 and uploaded[0].get("status") == "failed", written()
    assert uploaded[0].get("client_error") == "recorder_error.NotSupportedError", (
        "the reason a recorder died was dropped by the route's token filter: "
        f"{uploaded[0]}")


def _server_client_error_filter() -> tuple[str, int]:
    """The grammar server/app.py applies to `client_error`, read from the server.

    Read rather than restated so this cannot drift into agreeing with a rule the
    server stopped applying. Both shapes the route has used are accepted: the
    named constants it uses now, and the inline literals it used before.
    """
    src = _src(ROOT / "server" / "app.py")
    grammar = (re.search(r'_CLIENT_TOKEN_RE\s*=\s*re\.compile\(r"([^"]+)"\)', src)
               or re.search(r'fullmatch\(r"([^"]+)", hint\)', src))
    cap = (re.search(r"_CLIENT_TOKEN_MAX\s*=\s*(\d+)", src)
           or re.search(r"len\(hint\) <= (\d+)", src))
    assert grammar and cap, (
        "server/app.py no longer filters client_error in a shape this test can read; "
        "re-read the confirm route before trusting this assertion")
    return grammar.group(1), int(cap.group(1))


def test_every_reason_the_page_reports_survives_the_servers_filter(tmp_path):
    """The client's diagnosis is worth nothing if the server drops it.

    `client_error` is the one field that lets anyone tell a recorder that died
    from an upload that broke, and server/app.py keeps it only when it matches a
    short bare token — no colon, 40 characters or fewer — and says nothing when
    it does not. Every reason the page built for a recorder fault carried a
    colon ('recorder_error:NotSupportedError'), so the whole set was being
    discarded by a filter nobody had read.

    The grammar is read out of the server rather than restated here, so this
    fails if either side moves and the two stop agreeing.
    """
    out = _run(tmp_path, CAPTURE_HARNESS, V2)
    line = next((ln for ln in out.splitlines() if ln.startswith("TOKENS ")), None)
    assert line, f"the harness printed no tokens:\n{out}"
    tokens = json.loads(line[len("TOKENS "):])
    assert tokens, "no reasons were checked"

    pattern, limit = _server_client_error_filter()

    for token in tokens:
        assert token, "a reason encoded to nothing at all"
        assert len(token) <= limit, f"{token!r} is longer than the server's {limit}-char cap"
        assert re.fullmatch(pattern, token), (
            f"the confirm route would silently drop {token!r}, and with it the only field "
            f"that tells a lost recording apart from a camera that never ran")


def test_the_recording_outlives_its_own_teardown(tmp_path):
    """Finding 4: endSession stopped the recorder's input tracks and closed the
    AudioContext while MediaRecorder still owed a final chunk."""
    assert "TEARDOWN OK" in _run(tmp_path, TEARDOWN_HARNESS, V2)


def test_a_capture_failure_says_which_failure_it_was(tmp_path):
    """Finding 7: one sentence for every failure, and it was the wrong sentence
    for most of them."""
    assert "MESSAGES OK" in _run(tmp_path, FAILURE_MESSAGE_HARNESS, V2)


def test_the_live_researcher_socket_keeps_itself_alive(tmp_path):
    """No keepalive against a 120s ALB idle timeout, no reconnect, and no way
    back but a page reload in the middle of an encounter."""
    assert "RESEARCHER OK" in _run(tmp_path, RESEARCHER_HARNESS, RESEARCHER)
