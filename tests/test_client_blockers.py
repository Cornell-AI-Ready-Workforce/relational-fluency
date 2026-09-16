"""Tests for the three browser pages: `static/v2.html` (the participant),
`static/researcher.html` (the launch card) and `static/rater.html` (the rating
console).

These pages are HTML with an inline script, so there is no Python module to
import — but they are also where three of the study's quietest failures lived,
and reading them twice is how those failures survived review in the first
place. So this file runs them: each page's script is executed in a Node vm
against a thin DOM, a routed `fetch` and a virtual clock, and the assertions are
made by pressing the page's own buttons and reading what it painted.

The virtual clock is what makes the timing testable. The upload chain has a
15-second presign deadline, a 180-second PUT deadline and a 45-second bound on
how long a participant may be held on the completion overlay; a real sleep
cannot assert on any of those, and a clock the test advances can assert on all
three exactly.

What is covered, and the defect each part is a regression test for:

1. **The upload chain** (`finishVideoRecording` / `uploadRecording`) — every
   failure path used to resolve a bare `false` that the only caller threw away,
   with no retry and no deadline anywhere. An encounter whose webcam recording
   never reached the bucket was indistinguishable, from every surface the study
   has, from one whose recording did. Worse: a PUT that landed and a confirm
   that did not left the object in S3 with no `video_uploaded` event, which is
   how a rater comes to be told to score from the transcript alone for an
   encounter that has a video.
2. **The completion overlay** — it hid every control and then awaited the
   upload with no timeout and no abort, so a slow bucket cost the participant
   the rest of the study and their completion code.
3. **What the bound then did** — the first bounded version dropped the promise
   and let the next button unload the page, so a slow-but-healthy upload (45 MB
   is more than 45 seconds on any uplink under about 8 Mbps) died with no
   notice, no confirm and no events line: "captured, upload lost" became
   indistinguishable from "this encounter never had a camera", which is
   precisely the distinction part 1 exists to preserve. And the `closing` check
   ran only after the race, so a participant who withdrew while /advance was in
   flight had their goodbye card replaced by a modal whose only button was a
   skip handler the race had already consumed.
4. **The launch card** — it appended SESSION_KEY, the researcher credential, to
   the URL it loads in the participant's tab; and the probe that replaced that
   line was an unbounded fetch on the critical path of handing the participant
   their link.
5. **The rating console** — the submit button was re-enabled unconditionally
   after the packet itself had said this encounter must not be rated.
6. **The fixture wave** — the chain is driven once per real encounter in the
   27-encounter wave, under the credential failures that will actually occur
   when the AWS keys arrive, to show that none of them is silent.

No network call is made and no server is started: every request the pages make
is answered by the test's own router. Skipped where node is not installed; it
is not a runtime dependency of the study.

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
V2 = ROOT / "static" / "v2.html"
RESEARCHER = ROOT / "static" / "researcher.html"
RATER = ROOT / "static" / "rater.html"

# The fixture wave is resolved once for the whole suite, in tests/conftest.py,
# and reached here through its `wave_encounters` fixture. What was here before
# was `DATA_DIR or <one machine's scratchpad path, session UUID and all>`, which
# is the pattern five sibling modules already document as fixed: on any other
# machine the fallback is missing, so the wave-backed test below quietly stopped
# testing anything — and with DATA_DIR pointed at a fresh temp directory it went
# red instead, because server.storage creates DATA_DIR/sessions on import and an
# existing-but-empty sessions/ got past the is_dir() guard. conftest resolves
# RF_FIXTURE_DIR, RF_FIXTURE and DATA_DIR alike, and counts a directory as a
# wave only when it holds at least one */record.json.


# --------------------------------------------------------------------------
# Structural checks. These need no node, so the invariants they guard hold on
# any machine that can run the suite at all.
# --------------------------------------------------------------------------

def test_the_launch_card_does_not_append_the_session_key():
    """B48 / contract 6, as a plain read of the file.

    `key` on the researcher page is SESSION_KEY — it is what `check_key` gates
    /researcher, /director, /evidence and every per-session download.zip on —
    and the launch card loads its URL in the PARTICIPANT's tab. The old line
    was an unconditional `+ (key ? '&key=' + ... : '')`, which is the exact
    opposite of the rule /start applies.
    """
    src = RESEARCHER.read_text(encoding="utf-8")
    leak = re.compile(r"participant_url\s*\+\s*\(\s*key\s*\?")
    assert not leak.search(src), (
        "the launch card appends the researcher key to the participant URL "
        "unconditionally again")
    # And the decision is made in one named place, so the next reader finds it.
    assert "async function participantUrl(" in src


def test_every_upload_request_carries_a_deadline():
    """B4/B46. Three bare fetches with no AbortController is how a hung socket
    became a participant who could not reach their completion code."""
    src = V2.read_text(encoding="utf-8")
    assert "function fetchWithDeadline(" in src
    assert "new AbortController()" in src
    chain = src[src.index("async function uploadRecording("):src.index("function finishVideoRecording(")]
    # Every request in the chain goes through the deadline wrapper. A bare
    # `fetch(` here would be a request with no bound again.
    assert not re.search(r"(?<!With)(?<!\w)fetch\(", chain.replace("fetchWithDeadline(", "")), \
        "a request in the upload chain bypasses fetchWithDeadline"
    assert chain.count("fetchWithDeadline(") == 3, "the chain is no longer presign -> PUT -> confirm"


def test_the_completion_overlay_wait_is_bounded():
    """B46. `await videoUpload` behind a modal with no buttons."""
    src = V2.read_text(encoding="utf-8")
    assert "try { await videoUpload; } catch (e) {}" not in src, \
        "the unbounded await is back"
    assert "VIDEO_UPLOAD_WAIT_MS" in src and "Promise.race([" in src


def test_the_withdrawal_guard_runs_before_the_overlay_is_painted():
    """R19, as a plain read of the file.

    A `closing` check that runs only after the upload race cannot stop the wait
    screen being painted over somebody's goodbye card — by the time it fires the
    participant is already looking at a modal whose primary button is the skip
    handler the race consumed. The guard has to sit above the paint.
    """
    src = V2.read_text(encoding="utf-8")
    body = src[src.index("async function onEncounterComplete("):]
    advanced = body.index("run = advanced;")
    guard = body.index("if (closing) return;", advanced)
    paint = body.index("$('nextTitle').textContent = 'Saving your recording", advanced)
    assert guard < paint, "the closing guard is still checked only after the wait is painted"


def test_a_bounded_upload_is_never_a_silently_dropped_one():
    """R20 / B4 / B47. The bound stays; what it must not do is lose the
    recording without a trace. Three things make that impossible: the outcome
    is `pending` rather than `silent`, something outlives the cleared
    `videoUpload`, and every way out of the page reports an upload it kills."""
    src = V2.read_text(encoding="utf-8")
    assert "reason: 'slow', pending: true" in src, \
        "the deadline marks a still-running upload as silent again"
    assert "reason: 'skipped', pending: true" in src, \
        "the skip marks a still-running upload as silent again"
    assert "let liveUpload = null;" in src
    assert "function reportAbandonedUpload(" in src
    assert "client_error=abandoned" in src
    assert "'pagehide'" in src, "nothing records an upload killed by leaving the page"


def test_the_launch_probe_is_bounded():
    """R21. Every other request this branch added got an AbortController; the
    one on the critical path of handing a participant their link did not."""
    src = RESEARCHER.read_text(encoding="utf-8")
    probe = src[src.index("async function participantUrl("):]
    probe = probe[:probe.index("\n  }")]
    assert "new AbortController()" in probe and "signal: ac.signal" in probe, \
        "the participant-key probe is an unbounded fetch again"
    assert "clearTimeout(timer)" in probe


def test_the_rater_console_does_not_re_enable_a_blocked_submit():
    """R32. renderVideo/renderItems disable the button; openAssignment ran
    afterwards and turned it back on."""
    src = RATER.read_text(encoding="utf-8")
    assert "if (blockedReason) {" in src
    assert re.search(r"} else \{\s*\$\('submitBtn'\)\.disabled = false;", src), \
        "the submit button is re-enabled without checking blockedReason again"


# --------------------------------------------------------------------------
# The harnesses. Each is a self-contained Node program; the shared browser stub
# is written alongside them.
# --------------------------------------------------------------------------

DOM_STUB = r"""/* A thin browser: a DOM, a routed fetch and a virtual clock.

   Thin on purpose. The pages under test reach for element ids, set textContent
   and innerHTML, toggle style.display and hang onclick handlers off buttons,
   and that is all this reproduces. The clock is virtual so a 45-second wait and
   a 180-second upload deadline can be exercised in a millisecond and asserted
   on exactly, rather than approximated with a sleep. */
'use strict';
const vm = require('vm');

function makeClock() {
  let now = 0, nextId = 1;
  const timers = [];
  const api = {
    now: () => now,
    setTimeout(fn, ms) { const t = { id: nextId++, at: now + (ms || 0), fn }; timers.push(t); return t.id; },
    clearTimeout(id) { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
    setInterval(fn, ms) { return api.setTimeout(() => {}, ms); },   // nothing under test polls
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
    // Let every already-resolved promise chain run to a standstill.
    async flush() { for (let i = 0; i < 50; i++) await new Promise(r => setImmediate(r)); },
  };
  return api;
}

function makeDom() {
  const byId = new Map();
  function el(id) {
    return {
      id: id || '', style: {}, dataset: {}, children: [],
      textContent: '', innerHTML: '', value: '', disabled: false,
      className: '', scrollTop: 0, scrollHeight: 0, onclick: null,
      classList: {
        _s: new Set(),
        add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
        contains(c) { return this._s.has(c); },
        toggle(c, on) { if (on === undefined) on = !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); },
      },
      listeners: {},
      addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
      removeEventListener() {},
      appendChild(c) { this.children.push(c); return c; },
      removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); },
      remove() {}, focus() {}, blur() {},
      click() { (this.listeners.click || []).forEach(f => f({})); },
      _q: new Map(),
      querySelector(sel) { if (!this._q.has(sel)) this._q.set(sel, el(sel)); return this._q.get(sel); },
      querySelectorAll: () => [],
      getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
      setAttribute() {}, getAttribute: () => null, insertAdjacentHTML() {},
      play: () => Promise.resolve(), pause() {},
      get firstChild() { return this.children[0] || null; },
    };
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

/* A fetch whose every route is declared by the test. Routes match on a
   substring of the URL; a handler returns a response stub, an Error to reject
   with, or the string 'hang' for a socket that never answers — the case the
   participant page had no defence against at all. */
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

function makeContext(extra) {
  const clock = makeClock();
  const dom = makeDom();
  const net = makeFetch();
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
    Blob: function Blob(parts, o) {
      this.size = (parts || []).reduce((n, p) => n + (p.size || 0), 0);
      this.type = (o || {}).type || '';
    },
    MediaStream: function MediaStream() {
      this.getVideoTracks = () => []; this.getAudioTracks = () => []; this.getTracks = () => [];
    },
    WebSocket: function WebSocket() { this.close = () => {}; this.send = () => {}; },
    localStorage: { _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; },
                    setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
    alert() {}, confirm: () => true,
  };
  // The beacons the page fires when a participant leaves mid-upload are
  // recorded rather than sent: "did this loss get reported, and against which
  // session" is the assertion, and sendBeacon is the only request in the page
  // that survives the page.
  const beacons = [];
  sandbox.navigator = {
    mediaDevices: { getUserMedia: async () => { throw new Error('no camera in a test'); } },
    userAgent: 'node',
    sendBeacon: (url) => { beacons.push(String(url)); return true; },
  };
  // Window-level events the page registers for. `pagehide` is the one that
  // matters: it is the page's last chance to say that a recording was lost.
  const winListeners = {};
  sandbox.addEventListener = (ev, fn) => { (winListeners[ev] = winListeners[ev] || []).push(fn); };
  sandbox.removeEventListener = () => {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  Object.assign(sandbox, extra || {});
  return { ctx: vm.createContext(sandbox), sandbox, clock, dom, net, beacons,
           fire(ev) { (winListeners[ev] || []).forEach(fn => fn({ type: ev })); } };
}

/* Boot the participant page with its opening sequence stopped. The page
   resolves the run first and gives up if it cannot, so one refused
   /api/run/... keeps participant creation, the brief and the audio check out
   of the way of what is being tested. */
function bootV2(page, search) {
  const src = require('fs').readFileSync(page, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
  const b = makeContext({
    location: { search: search, href: 'http://t/v2' + search, reload() {}, replace() {} },
  });
  b.net.route([{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);
  vm.runInContext(src, b.ctx, { filename: 'v2.html' });
  return b;
}

module.exports = { makeContext, bootV2, vm };
"""


UPLOAD_HARNESS = r"""/* Drives static/v2.html's own upload chain: presign -> PUT -> confirm, under
   each way it can fail once real S3 credentials exist. Nothing is inspected by
   reading the file; the page's finishVideoRecording is called with a recorder
   that stops, and what the page then does to the network and to the transcript
   is what is asserted. */
'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
const set = (b, code) => vm.runInContext(code, b.ctx);

// A MediaRecorder that is already recording and hands back one chunk on stop.
function armRecorder(b, bytes) {
  b.sandbox.__rec = { state: 'recording', onstop: null,
                      stop() { const f = this.onstop; if (f) f(); } };
  b.sandbox.__chunk = { size: bytes === undefined ? 4096 : bytes };
  set(b, "videoRecorder = __rec; videoChunks = [__chunk];" +
         "videoMime = 'video/webm;codecs=vp8,opus';" +
         "sessionId = 's_1772460300_44c9a2'; participantId = 'p_test';");
}

const transcript = (b) =>
  b.dom.document.getElementById('transcript').children.map(c => c.textContent).join(' | ');

async function upload(b) {
  const p = b.ctx.finishVideoRecording();
  assert(p, 'finishVideoRecording returned null with a live recorder');
  let out = null;
  p.then(v => { out = v; });
  await b.clock.advance(400000);   // past every deadline in the chain
  assert(out !== null, 'the upload promise never settled — the caller waits forever');
  return out;
}

(async () => {
  const PRESIGN = '/video-upload-url', CONFIRM = '/video-uploaded', S3 = 'https://s3.invalid/';

  // --- a confirm lost to a network blip after a landed PUT is retried ------
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(200, { url: S3 + 'obj' }) },
      { match: S3, fn: () => b.net.res(200, {}) },
      { match: CONFIRM, fn: (c, n) => (n === 1 ? new Error('network')
                                              : b.net.res(200, { ok: true, bytes: 4096 })) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, true, 'a retried confirm did not report success: ' + JSON.stringify(out));
    assert.strictEqual(b.net.countOf(CONFIRM), 2, 'the confirm was not retried');
    assert.strictEqual(b.net.countOf(S3), 1, 'the recording was re-sent when only the confirm had failed');
    assert(!/could not be saved/.test(transcript(b)), 'a recovered upload told the participant it failed');
  }

  // --- PUT landed, confirm never will: reported, not swallowed -------------
  // This is the case that makes a record misstate what happened: the object is
  // in the bucket, no receipt is written, and the rater is told there is no
  // video to play.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: (c, n) => (n === 1 ? b.net.res(200, { url: S3 + 'obj' }) : b.net.res(409, {})) },
      { match: S3, fn: () => b.net.res(200, {}) },
      { match: CONFIRM, fn: () => b.net.res(500, {}) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, false, 'a lost confirm was reported as success');
    assert.strictEqual(out.reason, 'confirm_http_500', 'wrong reason: ' + out.reason);
    assert.strictEqual(b.net.countOf(CONFIRM), 3, 'the confirm was not retried three times');
    assert(/could not be saved/.test(transcript(b)), 'the participant was not told');
  }

  // --- the PUT failed: the confirm still goes, carrying the reason ---------
  // Contract 2: a failed upload is not a missing video, and the event is what
  // makes the difference recoverable.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(200, { url: S3 + 'obj' }) },
      { match: S3, fn: () => b.net.res(403, {}) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, false, 'a failed PUT was reported as success');
    assert.strictEqual(out.reason, 'put_http_403', 'wrong reason: ' + out.reason);
    assert.strictEqual(b.net.countOf(S3), 2, 'the PUT was not retried');
    assert.strictEqual(b.net.countOf(CONFIRM), 1, 'no confirm was sent for a failed upload');
    const url = b.net.urlsOf(CONFIRM)[0];
    assert(/client_error=put_http_403/.test(url), 'the confirm did not carry the reason: ' + url);
    assert(/participant_id=p_test/.test(url), 'the confirm lost its participant_id: ' + url);
  }

  // --- a 409 on the second presign means the object is already there -------
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: (c, n) => (n === 1 ? b.net.res(200, { url: S3 + 'obj' }) : b.net.res(409, {})) },
      { match: S3, fn: () => new Error('connection reset') },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: true, bytes: 4096 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, true, '409-means-landed was not honoured: ' + JSON.stringify(out));
    assert.strictEqual(b.net.countOf(S3), 1, 'the second attempt re-sent the blob after a 409');
    assert(!/client_error/.test(b.net.urlsOf(CONFIRM)[0]), 'a landed upload was confirmed as an error');
  }

  // --- a 200 whose body carries no url: the two sides have drifted ---------
  // The server cannot produce this any more (the route returns the presign dict
  // or raises), which is why the branch no longer claims it is an unconfigured
  // bucket — it described a response that never existed. It stays because
  // reading `.url` off a null body would surface a broken contract as a bare
  // 'network' and send a researcher to the wrong seam entirely.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(200, null) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.reason, 'presign_no_url', 'wrong reason: ' + out.reason);
    assert(/could not be saved/.test(transcript(b)), 'a broken presign contract was silent');
  }

  // --- 503 "cannot confirm" is NOT a landed upload -------------------------
  // P10. The server answers 409 only when S3 confirmed the object is in the
  // bucket, and 503 when it is refusing on the strength of its own earlier
  // receipt while S3 will not answer. Reading the second as "the PUT landed" is
  // how a retry after a failed PUT comes to decline to re-send the recording on
  // nobody's word. The page must treat it as the failure it is, and say so.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(503, { detail: 'cannot confirm the existing recording (SlowDown)' }) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, false, 'an unconfirmed refusal was reported as a landed upload');
    assert.strictEqual(out.reason, 'presign_http_503', 'wrong reason: ' + out.reason);
    assert.strictEqual(b.net.countOf(S3), 0, 'no URL was issued, so nothing may be PUT');
    assert(/could not be saved/.test(transcript(b)), 'the participant was not told');
    assert(/client_error=presign_http_503/.test(b.net.urlsOf(CONFIRM)[0]),
      'the confirm did not carry the reason: ' + b.net.urlsOf(CONFIRM)[0]);
  }

  // --- 409 still means the object is confirmed there -----------------------
  // The counterpart of the case above, and the reason the two must not share a
  // status: this one the page may act on, because the server says it only after
  // S3 answered.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(409, { detail: 'video already uploaded' }) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: true, bytes: 4096 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, true, 'a confirmed object was not honoured: ' + JSON.stringify(out));
    assert.strictEqual(b.net.countOf(S3), 0, 'the blob was re-sent over a confirmed object');
  }

  // --- a hung socket is bounded by its deadline, not left forever ----------
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => 'hang' },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
    ]);
    const p = b.ctx.finishVideoRecording();
    let out = null; p.then(v => { out = v; });
    await b.clock.advance(14000);
    assert.strictEqual(out, null, 'the presign gave up before its deadline');
    await b.clock.advance(400000);
    assert(out && out.ok === false, 'a hung presign never settled');
    assert.strictEqual(out.reason, 'timeout', 'wrong reason for a hung socket: ' + out.reason);
  }

  // --- nothing recorded is not a failure to report ------------------------
  // The absence is genuine here, so a record that says "no video" is telling
  // the truth and the participant has nothing to be told.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b, 0);
    b.net.route([]);
    const out = await upload(b);
    assert.strictEqual(out.silent, true, 'an empty recording was reported as a loss');
    assert.strictEqual(b.net.calls.length, 0, 'an empty recording still called the server');
    assert(!/could not be saved/.test(transcript(b)), 'an empty recording alarmed the participant');
  }

  console.log('UPLOAD OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


CONSENT_HARNESS = r"""/* Drives static/v2.html's consent gate: what the participant is TOLD after
   they press Decline.

   The decline route answers 200 whether or not it recorded anything —
   deliberately, because re-prompting somebody who has just refused would be
   worse than a thin record — and says what it actually did in the body. The
   page read only `r.ok`, so the one case the server refuses on purpose (a
   record that already carries consent, which is what both tabs of a duplicated
   study link produce) painted "You have not taken part / nothing about you was
   recorded" over a participant whose encounters may already be on disk. That is
   a false statement about their own data on the one screen they read carefully,
   and it leaves them believing they are out of a study they are still in. */
'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
const $ = (b, id) => b.dom.document.getElementById(id);
const DECLINE = '/api/consent/decline';

/* Open the gate and press Decline, with the decline route answering `reply`
   (an Error rejects, as an unreachable server does). */
async function decline(reply) {
  const b = bootV2(PAGE, '?run=r_1');
  b.net.route([
    { match: DECLINE, fn: () => reply },
    { match: '/api/consent', fn: () => b.net.res(200, {
        title: 'Consent', body: 'text', confirm_checkbox: 'I agree' }) },
    { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
  ]);
  vm.runInContext("participantId = 'p_test'; consentPending = true;", b.ctx);
  b.ctx.ensureParticipant();
  await b.clock.advance(100);
  assert($(b, 'consentDecline').listeners.click, 'the decline button has no handler');
  $(b, 'consentDecline').click();
  await b.clock.advance(1000);
  return b;
}

(async () => {
  // --- a refusal that was recorded: the card may say so --------------------
  {
    const b = await decline({ ok: true, status: 200,
      json: async () => ({ recorded: true, withdrawn: true, reason: 'recorded' }),
      text: async () => '' });
    assert(/You have not taken part/.test($(b, 'nextTitle').textContent),
      'a recorded refusal did not get the closing card: ' + $(b, 'nextTitle').textContent);
    const body = $(b, 'nextBody').innerHTML;
    assert(/noted that you chose not to take part/.test(body), 'the refusal was not confirmed: ' + body);
    assert(/nothing about you was recorded/.test(body), 'the true reassurance was dropped: ' + body);
  }

  // --- 200, recorded:false, already consented ------------------------------
  // The case the whole item is about. Nothing here may claim the refusal is on
  // file, and nothing may claim nothing was recorded.
  {
    const b = await decline({ ok: true, status: 200,
      json: async () => ({ recorded: false, withdrawn: false, reason: 'already_consented' }),
      text: async () => '' });
    const title = $(b, 'nextTitle').textContent, body = $(b, 'nextBody').innerHTML;
    assert(!/You have not taken part/.test(title),
      'an already-consented participant was told they had not taken part: ' + title);
    assert(!/nothing about you was recorded/.test(body),
      'the page claimed nothing was recorded for a consented record: ' + body);
    assert(!/noted that you chose not to take part/.test(body),
      'the page claimed a refusal was filed when the server said it was not: ' + body);
    assert(/consent has already been given/.test(body),
      'the participant was not told what actually happened: ' + body);
    assert(/tell the researcher/.test(body), 'no way to actually get out was offered: ' + body);
  }

  // --- 200, recorded:false, no such record ---------------------------------
  // Nothing was captured here, so the reassurance is true; what must not
  // survive is the claim that the refusal was filed.
  {
    const b = await decline({ ok: true, status: 200,
      json: async () => ({ recorded: false, withdrawn: false, reason: 'no_record' }),
      text: async () => '' });
    const body = $(b, 'nextBody').innerHTML;
    assert(/nothing about you was recorded/.test(body), 'the true reassurance was dropped: ' + body);
    assert(/could not note your decision/.test(body),
      'an unfiled refusal was reported as filed: ' + body);
    assert(!/noted that you chose not to take part/.test(body), body);
  }

  // --- the server never answered -------------------------------------------
  {
    const b = await decline(new Error('offline'));
    const body = $(b, 'nextBody').innerHTML;
    assert(/could not note your decision/.test(body),
      'an unreachable server was reported as a filed refusal: ' + body);
  }

  // --- a 500 is not a recorded refusal either ------------------------------
  {
    const b = await decline({ ok: false, status: 500, json: async () => ({}),
                              text: async () => '' });
    const body = $(b, 'nextBody').innerHTML;
    assert(/could not note your decision/.test(body), 'a 500 read as success: ' + body);
  }

  console.log('CONSENT OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


NOTICE_HARNESS = r"""/* R4: a message from the app must not be written into the transcript wearing
   the participant's name.

   The runner sends {"type":"error"} for a segment boundary it could not cross
   and for a lost transcription channel. The page rendered both with
   appendTranscript('user', ...), which labels the line "You:" — so the
   transcript, the one artefact whose entire content is who said what, recorded
   the participant saying "Something went wrong. Please try again." And
   _advance_segment retries the boundary, so a persistent failure wrote one more
   of those per attempt. */
'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];

(async () => {
  const b = bootV2(PAGE, '?run=r_1');
  vm.runInContext("cast = [{ id: 'a1', name: 'Dana' }];", b.ctx);

  // What the server actually sends, rendered by the page's own helpers.
  b.ctx.appendTranscript('user', null, 'I think we should ship it.');
  b.ctx.appendNotice('Something went wrong. Please try again.');
  b.ctx.appendNotice('We could not turn on your microphone.');

  const lines = b.dom.document.getElementById('transcript').children;
  assert.strictEqual(lines.length, 3, 'the notices were not rendered at all');

  const spoken = lines[0];
  assert(/You:/.test(spoken.innerHTML), 'a real participant turn lost its name');

  for (const note of [lines[1], lines[2]]) {
    assert.strictEqual(note.className, 'system-note',
      'a system message is still rendered as a turn: ' + note.className);
    assert(!/You:/.test(note.innerHTML + note.textContent),
      'a system message is still attributed to the participant: ' + note.textContent);
    assert(!/speaker/.test(note.innerHTML),
      'a system message still carries a speaker label: ' + note.innerHTML);
  }
  assert(/went wrong/.test(lines[1].textContent), 'the message itself was lost');

  console.log('NOTICE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


OVERLAY_HARNESS = r"""/* Drives static/v2.html's completion overlay: what the participant can do
   while their recording is being saved, and what they are told afterwards. */
'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
const set = (b, code) => vm.runInContext(code, b.ctx);
const $ = (b, id) => b.dom.document.getElementById(id);
const vis = (el) => el.style.display && el.style.display !== 'none';

const ADVANCED = { done: false, position: 2, total: 4, completed: [1], run_id: 'r_1',
                   completion_code: 'CODE1', participant_id: 'p_test', current: { id: 'sc' } };

// The page in the state it is in the instant a conversation ends: a session
// that just closed, and an upload whose fate the test decides.
function primed(uploadJs) {
  const b = bootV2(PAGE, '?run=r_1');
  b.net.route([
    { match: '/advance', fn: () => b.net.res(200, ADVANCED) },
    { match: '/api/run/config', fn: () => b.net.res(200, {
        return_url: 'https://survey.invalid/x', return_label: 'Return to the survey' }) },
  ]);
  set(b, "sessionId = 's_x'; advancing = false; " + uploadJs);
  return b;
}

(async () => {
  // --- the wait is bounded, and has a door while it waits -----------------
  {
    const b = primed('videoUpload = new Promise(() => {});');   // an upload that never lands
    b.ctx.onEncounterComplete();
    await b.clock.advance(1000);
    assert(/Saving your recording/.test($(b, 'nextTitle').textContent), 'no saving screen');
    assert(vis($(b, 'nextBtn')), 'the modal has no button while it waits — the participant is parked');
    assert(/without waiting/i.test($(b, 'nextBtn').textContent), 'no way past the wait');
    assert(vis($(b, 'nextAlt')), 'no exit door during the wait');
    await b.clock.advance(43000);
    assert(/Saving your recording/.test($(b, 'nextTitle').textContent), 'the wait ended early');
    await b.clock.advance(3000);
    assert(/Encounter 1 of 4 complete/.test($(b, 'nextTitle').textContent),
      'the deadline did not release the participant: ' + $(b, 'nextTitle').textContent);
    assert(!/could not save/i.test($(b, 'nextBody').innerHTML),
      'a slow upload was reported to the participant as a lost one');
  }

  // --- the skip button releases them at once ------------------------------
  {
    const b = primed('videoUpload = new Promise(() => {});');
    b.ctx.onEncounterComplete();
    await b.clock.advance(500);
    $(b, 'nextBtn').onclick();
    await b.clock.advance(10);
    assert(/Encounter 1 of 4 complete/.test($(b, 'nextTitle').textContent),
      'the skip button did not release the wait: ' + $(b, 'nextTitle').textContent);
    assert(/Start encounter 2 of 4/.test($(b, 'nextBtn').textContent), 'no way on to the next encounter');
  }

  // --- a failed upload is said out loud on the next screen ----------------
  {
    const b = primed("videoUpload = Promise.resolve({ ok: false, reason: 'put_http_403' });");
    b.ctx.onEncounterComplete();
    await b.clock.advance(1000);
    assert(/Encounter 1 of 4 complete/.test($(b, 'nextTitle').textContent), 'did not advance');
    assert(/could not save the video/i.test($(b, 'nextBody').innerHTML),
      'a lost recording produced the identical success screen: ' + $(b, 'nextBody').innerHTML);
  }

  // --- and on the final screen, without displacing the completion code ----
  {
    const b = bootV2(PAGE, '?run=r_1');
    const done = Object.assign({}, ADVANCED, { done: true, position: 5, completed: [1, 2, 3, 4] });
    b.net.route([
      { match: '/advance', fn: () => b.net.res(200, done) },
      { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '', return_label: '' }) },
    ]);
    set(b, "sessionId = 's_x'; advancing = false;" +
           "videoUpload = Promise.resolve({ ok: false, reason: 'network' });");
    b.ctx.onEncounterComplete();
    await b.clock.advance(1000);
    assert(/All encounters complete/.test($(b, 'nextTitle').textContent), 'did not finish the run');
    assert(/CODE1/.test($(b, 'nextBody').innerHTML), 'the completion code was lost');
    assert(/could not save the video/i.test($(b, 'nextBody').innerHTML), 'the final screen hid the loss');
  }

  // --- a success is silent, as it should be -------------------------------
  {
    const b = primed('videoUpload = Promise.resolve({ ok: true });');
    b.ctx.onEncounterComplete();
    await b.clock.advance(1000);
    assert(!/could not save/i.test($(b, 'nextBody').innerHTML), 'a landed upload alarmed the participant');
    assert(!vis($(b, 'nextAlt')), 'the exit door was left up after the wait');
  }

  // --- taking the exit mid-wait is not painted over when the wait ends ----
  {
    const b = primed('videoUpload = new Promise(() => {});');
    b.ctx.onEncounterComplete();
    await b.clock.advance(500);
    $(b, 'nextAlt').onclick();
    await b.clock.advance(500);
    assert(/Finishing here/.test($(b, 'nextTitle').textContent),
      'the exit door did not open: ' + $(b, 'nextTitle').textContent);
    await b.clock.advance(60000);
    assert(/Finishing here/.test($(b, 'nextTitle').textContent),
      'the upload wait repainted over the goodbye screen: ' + $(b, 'nextTitle').textContent);
    assert(/CODE1/.test($(b, 'nextBody').innerHTML), 'the partial code was lost on the way out');
  }

  // --- leaving the study holds briefly for an in-flight upload ------------
  {
    const b = primed('videoUpload = new Promise(() => {});');
    let gone = null;
    b.sandbox.location = { search: '?run=r_1', get href() { return ''; },
                           set href(v) { gone = v; }, reload() {} };
    b.ctx.showClosing('Finishing here', '<p>bye</p>', 'CODE1');
    await b.clock.advance(100);
    assert(vis($(b, 'nextBtn')), 'no way back to the survey');
    $(b, 'nextBtn').onclick();
    await b.clock.advance(100);
    assert.strictEqual(gone, null, 'the exit did not wait at all for the recording');
    assert(/press again to leave now/.test($(b, 'nextBtn').textContent), 'no immediate escape offered');
    await b.clock.advance(13000);
    assert(gone && /survey.invalid/.test(gone), 'the hold never released: ' + gone);
  }

  // --- ...and a second press leaves at once -------------------------------
  {
    const b = primed('videoUpload = new Promise(() => {});');
    let gone = null;
    b.sandbox.location = { search: '?run=r_1', get href() { return ''; },
                           set href(v) { gone = v; }, reload() {} };
    b.ctx.showClosing('Finishing here', '<p>bye</p>', 'CODE1');
    await b.clock.advance(100);
    $(b, 'nextBtn').onclick();
    await b.clock.advance(10);
    $(b, 'nextBtn').onclick();
    assert(gone && /survey.invalid/.test(gone), 'the second press did not leave immediately');
  }

  console.log('OVERLAY OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


RELEASE_HARNESS = r"""/* What happens to the recording AFTER the completion overlay's bounded wait
   lets the participant go, and what happens to the overlay when they withdraw
   while /advance is still in flight.

   The bound itself is not in question — a participant may not be held on a
   modal indefinitely — but the first version of it dropped the promise and
   navigated on the next click, so a slow-but-healthy upload died with no
   notice, no confirm and no events line: "captured, upload lost" became
   indistinguishable from "this encounter never had a camera", which is the one
   thing the whole chain exists to keep apart. These cases drive the real upload
   chain, with a PUT that lands at a time the test chooses, and press the real
   buttons. */
'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
const set = (b, code) => vm.runInContext(code, b.ctx);
const $ = (b, id) => b.dom.document.getElementById(id);

const ADVANCED = { done: false, position: 2, total: 4, completed: [1], run_id: 'r_1',
                   completion_code: 'CODE1', participant_id: 'p_test', current: { id: 'sc' } };
const S3 = 'https://s3.invalid/';
const SID = 's_1772460300_44c9a2';
const CONFIRM = '/video-uploaded';

/* The page one instant after a conversation ended: a recorder that stops, the
   page's own upload chain running against it, and a PUT that lands at
   `putAtMs` — 72 s for the 45 MB a ten-minute encounter makes at the 600 kbps
   this page records, which is an ordinary hotspot or rural uplink and well
   inside the chain's own 180 s PUT deadline. */
function primed(putAtMs, advanceDelayMs) {
  const b = bootV2(PAGE, '?run=r_1');
  let gone = null;
  b.sandbox.location = { search: '?run=r_1', get href() { return ''; },
                         set href(v) { gone = v; }, reload() {} };
  b.sandbox.__rec = { state: 'recording', onstop: null,
                      stop() { const f = this.onstop; if (f) f(); } };
  b.sandbox.__chunk = { size: 45 * 1024 * 1024 };
  // `runId` is already 'r_1' — it is a const read from the query string above.
  // `run` is the run as it stands BEFORE this advance, code and all, which is
  // what a withdrawal part-way through has to be able to hand back.
  set(b, "videoRecorder = __rec; videoChunks = [__chunk]; videoMime = 'video/webm';" +
         "sessionId = " + JSON.stringify(SID) + ";" +
         "participantId = 'p_test'; advancing = false;" +
         "run = { run_id: 'r_1', participant_id: 'p_test', position: 1, total: 4," +
         "        completion_code: 'CODE0', current: { id: 'sc' } };");
  const later = (ms, v) => new Promise(r => b.clock.setTimeout(() => r(v), ms));
  b.net.route([
    { match: '/advance', fn: () => (advanceDelayMs
        ? later(advanceDelayMs, b.net.res(200, ADVANCED)) : b.net.res(200, ADVANCED)) },
    { match: '/withdraw', fn: () => b.net.res(200, { ok: true }) },
    { match: '/api/run/config', fn: () => b.net.res(200, {
        return_url: 'https://survey.invalid/x', return_label: 'Return to the survey' }) },
    { match: '/video-upload-url', fn: () => b.net.res(200, { url: S3 + SID }) },
    { match: S3, fn: () => (putAtMs === null ? 'hang' : later(putAtMs, b.net.res(200, {}))) },
    { match: CONFIRM, fn: () => b.net.res(200, { ok: true, bytes: 45 * 1024 * 1024 }) },
  ]);
  b.ctx.finishVideoRecording();
  return { b, gone: () => gone };
}

(async () => {
  // --- released by the deadline, but not navigated out from under ---------
  {
    const { b, gone } = primed(72000);
    b.ctx.onEncounterComplete();
    await b.clock.advance(46000);
    assert(/Encounter 1 of 4 complete/.test($(b, 'nextTitle').textContent),
      'the deadline did not release the participant: ' + $(b, 'nextTitle').textContent);
    assert(/still being saved/i.test($(b, 'nextBody').innerHTML),
      'a release with the upload still running showed the plain success screen: ' +
      $(b, 'nextBody').innerHTML);
    assert(/Start encounter 2 of 4/.test($(b, 'nextBtn').textContent), 'no way on');
    $(b, 'nextBtn').onclick();
    await b.clock.advance(10);
    assert.strictEqual(gone(), null, 'the page navigated out from under a running upload');
    assert(/press again/i.test($(b, 'nextBtn').textContent),
      'the hold offered no immediate escape: ' + $(b, 'nextBtn').textContent);
    await b.clock.advance(40000);   // the PUT lands at t=72s, inside the hold
    assert(gone() && /\/v2\?run=r_1/.test(gone()), 'the hold never released: ' + gone());
    assert.strictEqual(b.net.countOf(CONFIRM), 1,
      'the recording that landed during the hold was never confirmed');
    assert(!/client_error/.test(b.net.urlsOf(CONFIRM)[0]),
      'a landed upload was confirmed as an error: ' + b.net.urlsOf(CONFIRM)[0]);
    assert.strictEqual(b.beacons.length, 0, 'an upload that landed was reported abandoned');
  }

  // --- the hold ends, and the loss is RECORDED rather than vanishing ------
  {
    const { b, gone } = primed(null);   // a PUT that never lands
    b.ctx.onEncounterComplete();
    await b.clock.advance(46000);
    $(b, 'nextBtn').onclick();
    await b.clock.advance(61000);       // past NEXT_UPLOAD_HOLD_MS
    assert(gone() && /\/v2\?run=r_1/.test(gone()), 'the hold trapped the participant: ' + gone());
    assert.strictEqual(b.beacons.length, 1,
      'a recording abandoned at the hold left no trace at all: ' + JSON.stringify(b.beacons));
    const beacon = b.beacons[0];
    assert(/client_error=abandoned/.test(beacon), 'the beacon did not say what happened: ' + beacon);
    assert(beacon.includes(SID), 'the loss was recorded against the wrong session: ' + beacon);
    assert(/participant_id=p_test/.test(beacon), 'the beacon lost its participant_id: ' + beacon);
  }

  // --- pressing through the hold reports it too ---------------------------
  {
    const { b, gone } = primed(null);
    b.ctx.onEncounterComplete();
    await b.clock.advance(46000);
    $(b, 'nextBtn').onclick();
    await b.clock.advance(10);
    $(b, 'nextBtn').onclick();          // "press again to continue now"
    await b.clock.advance(10);
    assert(gone(), 'the second press did not continue');
    assert(b.beacons.length === 1 && /client_error=abandoned/.test(b.beacons[0]),
      'continuing past the hold lost the recording silently: ' + JSON.stringify(b.beacons));
  }

  // --- any other way out of the page records it as well -------------------
  {
    const { b } = primed(null);
    b.ctx.onEncounterComplete();
    await b.clock.advance(46000);
    b.fire('pagehide');                 // closed tab, Back button, anything
    assert(b.beacons.length === 1 && /client_error=abandoned/.test(b.beacons[0]),
      'closing the tab mid-upload left no record: ' + JSON.stringify(b.beacons));
    b.fire('pagehide');
    assert.strictEqual(b.beacons.length, 1, 'the abandonment was reported twice');
  }

  // --- an upload that finished is never reported abandoned ----------------
  {
    const { b } = primed(2000);
    b.ctx.onEncounterComplete();
    await b.clock.advance(10000);
    assert(/Encounter 1 of 4 complete/.test($(b, 'nextTitle').textContent), 'did not advance');
    assert(!/still being saved/i.test($(b, 'nextBody').innerHTML),
      'a landed upload was described as still running');
    b.fire('pagehide');
    assert.strictEqual(b.beacons.length, 0, 'a completed upload was reported as abandoned');
  }

  // --- withdrawing while /advance is in flight is not painted over --------
  // The regression this is here for: the wait screen was painted first and the
  // `closing` check came only after the race, so the goodbye card was replaced
  // by a modal whose primary button was the skip handler the race had already
  // consumed — no working way out at all.
  {
    const { b, gone } = primed(null, 3000);
    b.ctx.onEncounterComplete();
    await b.clock.advance(1000);
    $(b, 'leaveBtn').click();           // the page's own Stop-the-study handler
    await b.clock.advance(500);
    assert(/You have stopped the study/.test($(b, 'nextTitle').textContent),
      'the withdrawal card never appeared: ' + $(b, 'nextTitle').textContent);
    await b.clock.advance(120000);      // /advance lands at t=3s, the wait would end at t=46s
    assert(/You have stopped the study/.test($(b, 'nextTitle').textContent),
      'the upload wait repainted over the goodbye card: ' + $(b, 'nextTitle').textContent);
    assert(/CODE0/.test($(b, 'nextBody').innerHTML), 'the partial code was lost');
    // And the button on that card is the one that works, not a consumed skip.
    $(b, 'nextBtn').onclick();
    await b.clock.advance(10);
    $(b, 'nextBtn').onclick();          // press again to leave now
    await b.clock.advance(10);
    assert(gone() && /survey.invalid/.test(gone()),
      'the withdrawn participant was parked with no way out: ' + gone());
  }

  // --- the exit door during the wait does not deny the encounter ----------
  // It is offered AFTER `run = advanced`, so the encounter is counted; the door
  // used to tell the participant it might not have been.
  {
    const { b } = primed(null);
    b.ctx.onEncounterComplete();
    await b.clock.advance(1000);
    $(b, 'nextAlt').onclick();
    await b.clock.advance(500);
    assert(/Finishing here/.test($(b, 'nextTitle').textContent), 'the exit door did not open');
    const body = $(b, 'nextBody').innerHTML;
    assert(!/may not have been counted/.test(body),
      'the exit door denied an encounter the run had already recorded: ' + body);
    assert(/counted towards your run/.test(body), 'the door did not say what did happen: ' + body);
    assert(/CODE1/.test(body), 'the code was lost on the way out');
  }

  // --- a recorder that died mid-encounter is still said on the next screen --
  // startVideoRecording's onerror writes exactly this line into videoNotice
  // (asserted where it is written, in tests/test_browser_compat.py), and the
  // completion overlay used to ASSIGN that variable from the upload outcome
  // before takeVideoNotice could consume it — so on the one path where the
  // upload succeeded and the recording was truncated anyway, the line was
  // discarded and the participant saw the plain success screen.
  {
    const { b } = primed(10);
    set(b, "videoNotice = '<p>The camera recording stopped part way through this conversation.</p>';");
    b.ctx.onEncounterComplete();
    await b.clock.advance(20000);
    const body = $(b, 'nextBody').innerHTML;
    assert(/stopped part way through/.test(body),
      'the recorder-error notice was overwritten before any screen could show it: ' + body);
    assert(!/<p>The camera recording[^<]*$/.test(body), 'the notice was rendered unwrapped');
  }

  console.log('RELEASE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


RATER_HARNESS = r"""/* Drives static/rater.html's packet opener: what the submit button does on a
   packet the packet itself says must not be rated. */
'use strict';
const fs = require('fs'), assert = require('assert');
const { makeContext, vm } = require('./stub.js');

const PAGE = process.argv[2];
const SRC = fs.readFileSync(PAGE, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];

const ITEMS = [1, 2, 3, 4, 5, 6].map(i => ({ id: 'i' + i, text: 'item ' + i }));

function boot() {
  // A token the page rejects, so boot() gates without touching the network and
  // the packet under test is the only thing that has been opened.
  const b = makeContext({
    location: { search: '?token=not-a-token', href: 'http://t/rater', reload() {} },
    performance: { now: () => 0 },
    scrollTo() {},
    CSS: { escape: (s) => s },
  });
  b.net.route([{ match: '/api/rater/', fn: () => b.net.res(200, {}) }]);
  vm.runInContext(SRC, b.ctx, { filename: 'rater.html' });
  return b;
}

async function open(packet) {
  const b = boot();
  b.net.route([{ match: '/api/rater/packet/', fn: () => b.net.res(200, packet) }]);
  await b.ctx.openAssignment('a_1');
  return b;
}

const $ = (b, id) => b.dom.document.getElementById(id);

(async () => {
  // --- a recording that could not be stored: blocked, and it LOOKS blocked -
  {
    const b = await open({ assignment_id: 'a_1', status: 'assigned', items: ITEMS,
      transcript: [{ speaker: 'participant', text: 'hello' }],
      media: { video_url: null, video_available: true, video_status: 'failed',
               note: 'This encounter WAS recorded, but the recording could not be stored.' } });
    assert.strictEqual($(b, 'submitBtn').disabled, true,
      'the submit button was re-enabled on a packet that must not be rated');
    assert(/Do not rate it/i.test($(b, 'submitMsg').textContent),
      'the rater was left to discover the block by pressing a dead button: ' +
      $(b, 'submitMsg').textContent);
    assert(/could not be stored/i.test($(b, 'videoSlot').innerHTML),
      'a lost recording read as an unsignable link: ' + $(b, 'videoSlot').innerHTML);
    // And the rating is still refused if it is attempted anyway.
    await b.ctx.onSubmit();
    assert.strictEqual(b.net.countOf('/rating'), 0, 'a blocked rating was filed');
  }

  // --- a link that could not be signed: blocked, and named apart ----------
  {
    const b = await open({ assignment_id: 'a_1', status: 'assigned', items: ITEMS,
      media: { video_url: null, video_available: true, video_status: 'unsigned',
               note: 'This encounter has a webcam recording, but a playback link could not be issued.' } });
    assert.strictEqual($(b, 'submitBtn').disabled, true, 'an unsignable packet was ratable');
    assert(/could not be loaded/i.test($(b, 'videoSlot').innerHTML),
      'the unsignable state lost its own wording: ' + $(b, 'videoSlot').innerHTML);
  }

  // --- an encounter that never had a camera IS ratable --------------------
  // Two of the twenty-seven in the reference wave. Blocking these would cost
  // the study real ratings.
  {
    const b = await open({ assignment_id: 'a_1', status: 'assigned', items: ITEMS,
      media: { video_url: null, video_available: false, video_status: 'absent',
               note: 'No webcam recording was captured for this encounter.' } });
    assert.strictEqual($(b, 'submitBtn').disabled, false,
      'an encounter with no camera was blocked from being rated');
    assert(/No video for this encounter/.test($(b, 'videoSlot').innerHTML),
      'the no-camera state lost its wording');
  }

  // --- an ordinary packet is ratable --------------------------------------
  {
    const b = await open({ assignment_id: 'a_1', status: 'assigned', items: ITEMS,
      media: { video_url: 'https://s3.invalid/v.webm', video_available: true,
               video_status: 'ok', expires_in: 3600 } });
    assert.strictEqual($(b, 'submitBtn').disabled, false, 'a good packet was blocked');
    assert(!$(b, 'submitMsg').textContent, 'a good packet was given a warning');
  }

  // --- a packet with no items stays blocked too ---------------------------
  {
    const b = await open({ assignment_id: 'a_1', status: 'assigned', items: [],
      media: { video_url: 'https://s3.invalid/v.webm', video_available: true, video_status: 'ok' } });
    assert.strictEqual($(b, 'submitBtn').disabled, true, 'a packet with no items was ratable');
    assert(/without its rating items/i.test($(b, 'submitMsg').textContent),
      'the missing item bank was not explained: ' + $(b, 'submitMsg').textContent);
  }

  console.log('RATER OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


LAUNCH_HARNESS = r"""/* Drives static/researcher.html's launch card: presses the button and reads
   the URL the participant's tab is actually pointed at. */
'use strict';
const fs = require('fs'), assert = require('assert');
const { makeContext, vm } = require('./stub.js');

const PAGE = process.argv[2];
const SRC = fs.readFileSync(PAGE, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
const KEY = 'S3CRET-SESSION-KEY';

function boot(opts) {
  const opened = [];
  const b = makeContext({
    location: { search: '?key=' + KEY, href: 'http://t/researcher?key=' + KEY, reload() {} },
  });
  b.sandbox.window.open = () => {
    if (opts.popupBlocked) return null;
    const t = { location: null };
    opened.push(t);
    return t;
  };
  b.net.route(opts.routes(b));
  vm.runInContext(SRC, b.ctx, { filename: 'researcher.html' });
  b.opened = opened;
  return b;
}

const launchOk = (b) => ({ match: '/api/launch', fn: () => b.net.res(200, {
  launch_id: 'ab12cd34', participant_url: '/v2?scenario=peer_feedback&launch=ab12cd34' }) });
const quiet = (b) => ({ match: '/api/', fn: () => b.net.res(200, []) });

async function pressLaunch(b) {
  vm.runInContext("launchDetail = { id: 'peer_feedback', agents: [] };", b.ctx);
  const btn = b.dom.document.getElementById('launchBtn');
  const handlers = btn.listeners.click || [];
  assert(handlers.length, 'the launch button has no click handler');
  handlers[0]({});
  await b.clock.advance(20000);   // past the probe's own deadline
}

(async () => {
  // --- open collection: the researcher key must not travel ----------------
  {
    const b = boot({ routes: (b) => [
      launchOk(b),
      // check_participant is a no-op here, so an unkeyed participant route answers 200
      { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
      quiet(b),
    ] });
    await pressLaunch(b);
    assert.strictEqual(b.opened.length, 1, 'no participant tab was opened');
    const url = b.opened[0].location;
    assert(url, 'the participant tab was never pointed anywhere');
    assert(!/key=/.test(url), 'the researcher key was handed to the participant: ' + url);
    assert(/launch=ab12cd34/.test(url), 'the launch was lost: ' + url);
  }

  // --- nor into the copyable fallback link when the popup is blocked ------
  {
    const b = boot({ popupBlocked: true, routes: (b) => [
      launchOk(b),
      { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
      quiet(b),
    ] });
    await pressLaunch(b);
    const html = b.dom.document.getElementById('launchStatus').innerHTML;
    assert(/Popup blocked/.test(html), 'no fallback link was offered: ' + html);
    assert(!new RegExp(KEY).test(html), 'the fallback link leaks the key: ' + html);
  }

  // --- a deployment that really does demand the key still gets it ---------
  {
    const b = boot({ routes: (b) => [
      launchOk(b),
      // PARTICIPANT_KEY_REQUIRED is set: the participant-open probe is refused
      { match: '/api/run/config', fn: () => b.net.res(401, { detail: 'Bad or missing key' }) },
      quiet(b),
    ] });
    await pressLaunch(b);
    const url = b.opened[0].location;
    assert(new RegExp('key=' + KEY).test(url),
      'the participant page needs the key and did not get it: ' + url);
  }

  // --- a server that answers the question itself has the last word -------
  {
    const b = boot({ routes: (b) => [
      { match: '/api/launch', fn: () => b.net.res(200, {
          launch_id: 'ab12cd34', participant_url: '/v2?scenario=x&launch=ab12cd34',
          participant_key_required: false }) },
      { match: '/api/run/config', fn: () => b.net.res(401, {}) },   // must not be consulted
      quiet(b),
    ] });
    await pressLaunch(b);
    assert(!/key=/.test(b.opened[0].location),
      'the launch response was overruled by the probe: ' + b.opened[0].location);
    assert.strictEqual(b.net.countOf('/api/run/config'), 0, 'the probe ran anyway');
  }

  // --- a probe that cannot be answered fails closed ----------------------
  // A participant page that answers 401 is a visible mistake a researcher can
  // fix in seconds; a leaked dataset credential is neither visible nor fixable.
  {
    const b = boot({ routes: (b) => [
      launchOk(b),
      { match: '/api/run/config', fn: () => new Error('offline') },
      quiet(b),
    ] });
    await pressLaunch(b);
    assert(!/key=/.test(b.opened[0].location),
      'a failed probe leaked the key anyway: ' + b.opened[0].location);
  }

  // --- a probe that never answers is bounded ------------------------------
  // The probe sits on the critical path of handing a participant their link:
  // the tab is already open and blank. An unbounded GET that hangs never
  // returns, never reaches the catch, and leaves the researcher looking at
  // "Creating session…" with a blank tab and a launch record quietly expiring.
  {
    const b = boot({ routes: (b) => [
      launchOk(b),
      { match: '/api/run/config', fn: () => 'hang' },
      quiet(b),
    ] });
    await pressLaunch(b);
    assert.strictEqual(b.opened.length, 1, 'no participant tab was opened');
    const url = b.opened[0].location;
    assert(url, 'a hung probe left the participant tab pointed at nothing');
    assert(/launch=ab12cd34/.test(url), 'the launch was lost: ' + url);
    assert(!/key=/.test(url), 'a hung probe leaked the key: ' + url);
    assert(/tab opened/i.test(b.dom.document.getElementById('launchStatus').textContent),
      'the launch never got past "Creating session…": ' +
      b.dom.document.getElementById('launchStatus').textContent);
  }

  console.log('LAUNCH OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


WAVE_HARNESS = r"""/* The upload chain, once per real encounter in the fixture wave, under the
   credential failures that actually happen when AWS keys arrive: no
   credentials (the server 500s), a throttled bucket, a wrong region, an expired
   presign, a socket that hangs, and the ordinary success. The point is not the
   individual verdicts — the cases above cover those — but that across 27 real
   encounters the chain always settles, always addresses the right session, and
   never fails without saying so. */
'use strict';
const fs = require('fs'), assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
const SESSIONS = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const S3 = 'https://s3.invalid/';

// name -> how the three legs behave. Named for the botocore condition each one
// stands in for at the server end.
const MODES = {
  ok:                { presign: () => 200, put: () => 200, confirm: () => 'ok' },
  no_credentials:    { presign: () => 500, put: () => 200, confirm: () => 500 },
  throttled_bucket:  { presign: () => 200, put: () => 503, confirm: () => 'empty' },
  wrong_region:      { presign: () => 500, put: () => 200, confirm: () => 500 },
  expired_presign:   { presign: () => 200, put: () => 403, confirm: () => 'empty' },
  hung_put:          { presign: () => 200, put: () => 'hang', confirm: () => 'empty' },
  lost_confirm:      { presign: () => 200, put: () => 200, confirm: () => 500 },
};
const ORDER = Object.keys(MODES);

(async () => {
  let reported = 0, landed = 0;
  for (let i = 0; i < SESSIONS.length; i++) {
    const sid = SESSIONS[i];
    const mode = ORDER[i % ORDER.length];
    const m = MODES[mode];
    const b = bootV2(PAGE, '?run=r_wave');
    b.sandbox.__rec = { state: 'recording', onstop: null,
                        stop() { const f = this.onstop; if (f) f(); } };
    b.sandbox.__chunk = { size: 45 * 1024 * 1024 };   // a ten-minute encounter at 600 kbps
    vm.runInContext("videoRecorder = __rec; videoChunks = [__chunk];" +
                    "videoMime = 'video/webm'; sessionId = " + JSON.stringify(sid) + ";" +
                    "participantId = 'p_' + " + JSON.stringify(sid.slice(-6)) + ";", b.ctx);
    b.net.route([
      { match: '/video-upload-url', fn: () => {
          const s = m.presign(); return s === 200 ? b.net.res(200, { url: S3 + sid }) : b.net.res(s, {}); } },
      { match: S3, fn: () => { const s = m.put(); return s === 'hang' ? 'hang' : b.net.res(s, {}); } },
      { match: '/video-uploaded', fn: () => {
          const s = m.confirm();
          if (s === 'ok') return b.net.res(200, { ok: true, bytes: 45 * 1024 * 1024 });
          if (s === 'empty') return b.net.res(200, { ok: false, bytes: 0 });
          return b.net.res(s, {}); } },
    ]);

    const p = b.ctx.finishVideoRecording();
    let out = null;
    p.then(v => { out = v; });
    await b.clock.advance(600000);
    assert(out !== null, sid + ' (' + mode + '): the upload never settled');

    // Whatever the chain did, it addressed this encounter and no other.
    for (const call of b.net.calls) {
      if (call.url.startsWith(S3)) continue;
      assert(call.url.includes(sid), sid + ' (' + mode + '): a request went to ' + call.url);
    }
    const told = b.dom.document.getElementById('transcript').children
      .map(c => c.textContent).join(' | ');
    if (out.ok) {
      landed++;
      assert(!/could not be saved/.test(told), sid + ' (' + mode + '): a landed upload was reported lost');
    } else {
      reported++;
      assert(out.reason, sid + ' (' + mode + '): a failure with no reason to give the researcher');
      assert(/could not be saved/.test(told),
        sid + ' (' + mode + '): the upload failed silently');
      // A failure the server can still hear about is a failure the wave can be
      // audited for; contract 2's "captured, upload failed" state depends on it.
      assert(b.net.countOf('/video-uploaded') >= 1,
        sid + ' (' + mode + '): no confirm was ever attempted, so nothing records the gap');
    }
  }
  assert(landed > 0 && reported > 0, 'the wave exercised only one outcome');
  console.log('WAVE OK ' + SESSIONS.length + ' encounters, ' + landed + ' landed, ' +
              reported + ' reported as lost');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harnesses need it")
    return node


def _run(tmp_path, harness_src, page, extra_arg=None):
    """Write the stub and one harness beside each other and run it."""
    (tmp_path / "stub.js").write_text(DOM_STUB, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(harness_src, encoding="utf-8")
    argv = [_node(), str(harness), str(page)]
    if extra_arg is not None:
        argv.append(str(extra_arg))
    # encoding pinned: node emits UTF-8 whatever the machine's locale is, and
    # these pages carry characters cp1252 cannot represent. Without it a
    # Windows contributor reads a failing harness's output as mojibake, which
    # is the worst possible moment for the output to be unreliable.
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_the_upload_chain_reports_what_happened(tmp_path):
    """B4 + B47: retries, deadlines, and a result nobody can throw away."""
    assert "UPLOAD OK" in _run(tmp_path, UPLOAD_HARNESS, V2)


def test_the_decline_card_says_what_the_server_actually_did(tmp_path):
    """B16's client half: the route answers 200 whether or not it recorded
    anything, so `r.ok` is not the answer — the body is."""
    assert "CONSENT OK" in _run(tmp_path, CONSENT_HARNESS, V2)


def test_a_system_message_is_not_written_as_a_participant_turn(tmp_path):
    """R4: {"type":"error"} frames were rendered as "You: Something went
    wrong", which puts words in the participant's mouth in the one artefact
    whose whole content is who said what."""
    assert "NOTICE OK" in _run(tmp_path, NOTICE_HARNESS, V2)


def test_the_completion_overlay_never_traps_the_participant(tmp_path):
    """B46: a bound, a skip, an exit — and the loss said out loud afterwards."""
    assert "OVERLAY OK" in _run(tmp_path, OVERLAY_HARNESS, V2)


def test_the_bound_does_not_lose_the_recording(tmp_path):
    """R19 + R20 (B4, B47): what happens after the bounded wait releases them,
    and what happens to the overlay when they withdraw mid-advance."""
    assert "RELEASE OK" in _run(tmp_path, RELEASE_HARNESS, V2)


def test_the_rating_console_blocks_what_it_says_it_blocks(tmp_path):
    """R32: a submit button that looks usable on a packet that must not be
    rated, by opening the packets rather than reading the file."""
    assert "RATER OK" in _run(tmp_path, RATER_HARNESS, RATER)


def test_the_launch_card_keeps_the_session_key(tmp_path):
    """B48 / contract 6, by pressing the button rather than reading the file."""
    assert "LAUNCH OK" in _run(tmp_path, LAUNCH_HARNESS, RESEARCHER)


def test_the_chain_survives_the_fixture_wave(tmp_path, wave_encounters):
    """Every encounter in the recorded wave, one credential failure each.

    `wave_encounters` comes from tests/conftest.py: it skips with a message
    naming the three environment variables when no wave is available, and it
    globs */record.json rather than trusting a directory to be non-empty.
    """
    listing = tmp_path / "sessions.json"
    listing.write_text(json.dumps(list(wave_encounters)), encoding="utf-8")
    out = _run(tmp_path, WAVE_HARNESS, V2, listing)
    assert "WAVE OK" in out, out


def test_the_microphone_test_releases_the_microphone_when_it_concludes():
    """The mic check used to stay live after it had its answer.

    Reported from hands-on testing as "the test is not turning off", and that
    is exactly what it was. Both concluding branches — `loud >= 6` setting "We
    can hear you", and the 8-second timer setting "Nothing heard" — updated the
    status line and returned, leaving `meterInt` polling every 50ms and the
    getUserMedia stream open. The only teardown was reached by pressing
    Continue or Skip, so between concluding and continuing the participant sat
    on a page with their recording indicator lit and a meter still bouncing.

    That is bad on a page nobody is recording yet, and worse on this one: the
    consent flow is what earns the right to turn a microphone on, and this ran
    ahead of the encounter with no way to tell it had stopped listening.

    A structural read rather than a driven one, and the reason is worth stating:
    the shared clock in DOM_STUB implements setInterval as a one-shot no-op
    ("nothing under test polls"), so the meter loop this bug lives inside cannot
    execute under the existing harness, and giving it a real setInterval would
    change the clock every other harness in this file shares. What is asserted
    here is the invariant that broke — every path that ends the test also ends
    the listening.
    """
    src = V2.read_text(encoding="utf-8")
    start = src.index("function runAudioCheck(")
    body = src[start:src.index("\n  }", start)]

    # The teardown exists, is named, and actually releases both resources.
    assert "const stopListening = () =>" in body, \
        "runAudioCheck no longer has a single named teardown"
    stop = body[body.index("const stopListening"):body.index("const concludeMicTest")]
    assert "clearInterval(meterInt)" in stop and "getTracks().forEach(t => t.stop())" in stop, \
        "stopListening no longer stops both the meter and the microphone stream"

    # Both concluding branches call it. Checked by their own status text, so
    # this test names the two states a participant actually sees.
    # Sliced at each branch's own closing token rather than a character
    # count, so adding or removing a comment inside a branch cannot move
    # the answer.
    heard = body.index("'We can hear you'")
    success = body[heard:body.index('meterInt = setInterval', heard)]
    assert "concludeMicTest()" in success, "the success branch does not release the microphone"
    silent = body.index("'Nothing heard'")
    timeout = body[silent:body.index("}, 8000);", silent)]
    assert "concludeMicTest()" in timeout, "the 'Nothing heard' branch does not release the microphone"

    # And the handlers do not accumulate across runs. needsAudioCheck returns
    # True whenever sessionStorage is unavailable, which is private browsing, so
    # a second run used to add a second click handler to the same button and one
    # press then opened two streams while only the last was tracked.
    assert "removeEventListener('click', onMicTest)" in body, \
        "the mic test handler is no longer removed, so a second run doubles it"


def test_a_drop_the_server_explained_is_not_offered_as_a_reconnect():
    """A permanent failure was presented as a transient one, forever.

    Reproduced against the running server with no gateway credential: the voice
    socket sends {"type":"session"}, then
    {"type":"error","message":"No gateway API key (set LITELLM_API_KEY)"}, then
    closes with no close frame. The page discarded the reason, and its close
    handler ran the same path a wifi blip takes: "Connection lost ... click
    Reconnect to pick up this encounter again". Reconnect met the identical condition,
    so the participant looped -- re-granting the camera on every press, watching
    their own face appear, and being reassured each time.

    The server's own message is deliberately NOT shown. It is written for an
    operator and names an environment variable; a participant can do nothing
    with it. What the page keeps is only that a reason was stated, which is what
    separates a drop worth retrying from one that will repeat.
    """
    src = V2.read_text(encoding="utf-8")

    # The signal is recorded where the error frame is handled, and reset per
    # attempt so a retry earns its own verdict.
    assert "serverStatedFailure = true" in src, (
        "the page no longer notices that the server stated a reason before the drop")
    assert src.count("serverStatedFailure = false") >= 1, (
        "serverStatedFailure is never reset, so one failure poisons every later attempt")

    start = src.index("function onConnectionDropped(")
    body = src[start:src.index("\n  $('dropReconnect')", start)]
    assert "const fatal = serverStatedFailure" in body, (
        "onConnectionDropped no longer distinguishes a stated failure from a drop")

    # Both branches write the card, or a later transient drop inherits the
    # failure's wording.
    assert body.count("$('dropText')") >= 2, (
        "only one branch writes the drop card, so its text can go stale")
    # This pinned the transient branch by the phrase "Nothing is lost", which
    # was the sentence it opened with. That sentence has since been removed as
    # untrue — Reconnect opens a new session and the actor starts again at its
    # first line, which is measured and is now what the card says — so the
    # discriminator is the promise each branch actually makes: one offers a
    # fresh run at this part, the other says the part is over. What this test is
    # for is unchanged: the two drops must not read alike.
    assert "start again from the beginning" in body and "could not continue" in body, (
        "the two drops no longer say two different things")

    # And the operator's message must not reach the participant. Checked at the
    # only place it is in scope — the error-frame handler — rather than by
    # searching the whole file for a vendor name, which a comment quoting the
    # message would trip without anything being rendered.
    handler = src.index("m.type === 'error'")
    assert "m.message" not in src[handler:handler + 900], (
        "the server's operator-facing error text is being rendered to a participant")


def test_a_second_stated_failure_stops_offering_a_retry():
    """"You can try once more" has to mean once.

    The card offered one retry in its text and then re-offered the button on
    every subsequent drop, so the same condition produced the same card
    indefinitely: the wording promised a limit the code did not keep, which is
    the defect the card was written to remove, one layer up.

    The exhausted state deliberately does NOT navigate to "/". That route serves
    landing.html, a scenario picker, and showing a study participant the whole
    scenario bank is a worse outcome than the loop. Their real exit is the
    survey they arrived from, which /api/run/config knows about, and which the
    completion and withdrawal cards already use.
    """
    src = V2.read_text(encoding="utf-8")
    start = src.index("function onConnectionDropped(")
    body = src[start:src.index("function showFailureExit(", start)]

    assert "fatalDrops++" in body and "fatalDrops = 0" in body, (
        "consecutive stated failures are no longer counted, so the retry cannot be spent")
    assert "const exhausted" in body, "there is no exhausted state"

    # The retry button is withdrawn once, and restored on the paths that still
    # offer one, or an exhausted encounter would hide it from a later drop that
    # a reconnect would genuinely fix.
    assert "$('dropReconnect').style.display = 'none'" in body, (
        "the retry button survives a second failure")
    assert body.count("$('dropReconnect').style.display = ''") >= 2, (
        "the retry button is never restored, so one exhausted encounter disables it for good")

    # The counter resets when an encounter actually completes.
    ec = src.index("async function onEncounterComplete(")
    assert "fatalDrops = 0" in src[ec:ec + 400], (
        "a finished encounter does not clear the count, so an early failure follows the "
        "participant into a later encounter that works")

    # And the way out is the survey, not the scenario picker.
    exit_fn = src[src.index("function showFailureExit("):]
    exit_fn = exit_fn[:exit_fn.index("\n  $('dropReconnect').addEventListener")]
    assert "/api/run/config" in exit_fn, "the exhausted card offers no way out"
    assert "contactWho()" in exit_fn, "the exhausted card names nobody to tell"
    assert "location.href = '/'" not in src, (
        "a failure path sends a study participant to the scenario picker")


def test_the_end_of_the_road_still_has_a_door():
    """A card with no button is worse than the loop it replaced.

    Reported from testing: once the retry was spent the button vanished and the
    participant was sealed onto the scenario screen. The only exit offered was
    the survey link, which is built from SURVEY_RETURN_URL, and that is unset on
    every developer machine and on any deployment nobody configured. So the
    exhausted card could render with a message and nothing to press.

    The finish door is therefore unconditional and does not wait on a fetch. It
    goes through showClosing, the same surface the "I can't continue" exit uses,
    so someone who stops here gets what someone who stops anywhere else gets:
    their completion code, which is what payment depends on.
    """
    src = V2.read_text(encoding="utf-8")
    exit_fn = src[src.index("function showFailureExit("):]
    exit_fn = exit_fn[:exit_fn.index("\n  $('dropReconnect').addEventListener")]

    assert "function showFailureExit(exhausted)" in src, (
        "showFailureExit can no longer tell a spent card from a retryable one")
    assert "showFailureExit(true)" in src and "showFailureExit(false)" in src, (
        "both call sites no longer say which state they are in")

    # The door is inside the `if (exhausted)` block, ahead of both network
    # calls, so no failed request can remove it.
    door = exit_fn[exit_fn.index("if (exhausted)"):exit_fn.index("contactWho()")]
    assert "showClosing(" in door, (
        "the exhausted card has no finish button, so a participant is sealed in")
    assert "completion_code" in door, (
        "finishing here does not hand over the completion code, which is what payment needs")
    assert "$('dropNote').classList.remove('show')" in door, (
        "the drop card is not dismissed, so it sits over the closing screen")

    # And the door must not be behind the return_url lookup.
    assert door.index("showClosing(") < exit_fn.index("/api/run/config"), (
        "the finish button is built after the survey lookup, so an unset "
        "SURVEY_RETURN_URL or a failed fetch can still strand someone")


LANDING = ROOT / "static" / "landing.html"


def test_every_page_carries_the_tab_icon():
    """One icon, and not the 334 KB one.

    logo.png is 839x850 and a third of a megabyte. Pointing rel="icon" at it
    works, and costs every visitor that download for a 16px square. favicon.png
    is the same artwork at 64px and 4.4 KB.
    """
    for name in ("landing.html", "v2.html", "rater.html", "researcher.html",
                 "director.html", "evidence.html", "participant.html"):
        p = ROOT / "static" / name
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        assert 'rel="icon"' in src, f"{name} has no tab icon"
        assert "/static/favicon.png" in src, f"{name} points its icon somewhere unexpected"
        assert 'rel="icon" type="image/png" href="/static/logo.png"' not in src, (
            f"{name} uses the full-size logo as its favicon")
    for asset in ("favicon.png", "apple-touch-icon.png"):
        f = ROOT / "static" / asset
        assert f.exists(), f"{asset} is missing, so every page links a 404"
        assert f.stat().st_size < 60_000, f"{asset} is too heavy for an icon"


def test_the_footer_never_prints_a_contact_the_consent_form_does_not_carry():
    """The landing footer shows the logo and the study contact.

    The contact comes from /api/consent's `contact:` block and nowhere else,
    and each field is printed only when it is really filled in. The shipped
    config carries "[FILL IN: principal investigator's name]", and a footer
    that prints that to a visitor is worse than one that says nothing. This is
    the rule the participant pages get from contactPhrase(), applied to the one
    page that has no such helper.
    """
    src = LANDING.read_text(encoding="utf-8")

    assert "footContact" in src and "loadFooterContact" in src, (
        "the footer no longer builds its contact from the consent config")
    assert "fetch('/api/consent'" in src, (
        "the footer contact does not come from the consent config")
    assert "function isFilled(" in src and "FILL IN" in src, (
        "nothing stops a placeholder reaching a visitor")

    fn = src[src.index("async function loadFooterContact("):]
    fn = fn[:fn.index("\n  loadScenarios();")]
    # An unfilled field shows a labelled empty slot, not the config's raw
    # "[FILL IN: ...]" text and not silence. Silence was the first version and it
    # made the gap invisible: the footer just looked like it had no contact by
    # design. What must never happen is the placeholder itself reaching a
    # visitor, or a slot that could be read as a real contact.
    assert "function slot(" in fn, (
        "an unfilled contact field no longer shows a slot, so the gap is invisible again")
    assert "not set" in fn, "the empty slot does not say it is unset"
    assert fn.count("isFilled(") == 3, (
        "a contact field is rendered without checking it is really filled in")
    # The participant-facing rule is the opposite one and must stay that way.
    v2 = V2.read_text(encoding="utf-8")
    assert "not set" not in v2, (
        "a participant screen now shows an unset-contact slot; there a missing "
        "contact must show nothing at all")

    # Operator-supplied text, on a page anyone can load: built as text nodes,
    # never pasted into innerHTML.
    assert "innerHTML" not in fn, (
        "the footer pastes config text into innerHTML")
    assert fn.count("textContent") >= 3, (
        "the contact fields are no longer written as text")

    # And the footer still carries the logo it was asked for.
    footer = src[src.index("<footer>"):src.index("</footer>")]
    assert "/static/logo.png" in footer, "the footer lost the logo"
    assert "research prototype" in footer and "Citations live in" not in footer, (
        "the old footer text is back")


EVIDENCE = ROOT / "static" / "evidence.html"


def test_the_evidence_trace_is_light_and_fills_the_screen():
    """Three things about the console an analyst reads encounters in.

    It was the only page in the platform carrying a dark palette, so a
    researcher on a dark OS opened the evidence trace onto a different ground
    from the console they clicked out of. Every other page here is light only.

    It also opened with a page title and a standfirst above a three-column tool
    that already names the encounter in its own appbar, and sat as a 1320px card
    on a ground, which on a 1920 display spent 600px of margin on a layout whose
    middle column is a transcript.
    """
    src = EVIDENCE.read_text(encoding="utf-8")

    assert "prefers-color-scheme" not in src and 'data-theme' not in src, (
        "the evidence trace flips with the operating system again, and no other page does")
    for other in ("landing.html", "researcher.html", "rater.html", "v2.html"):
        assert "prefers-color-scheme" not in (ROOT / "static" / other).read_text(encoding="utf-8"), (
            f"{other} gained a dark palette; the platform is light only by choice")

    assert "<h1>Evidence trace</h1>" not in src, "the page banner is back"
    assert 'class="subtitle"' not in src, "the standfirst is back"

    assert "max-width:none" in src and "height:100vh" in src, (
        "the console is no longer sized to the window")
    assert "max-width:1320px" not in src, "the fixed-width card is back"
    # In the window-sized layout the scroll panes size from the layout rather
    # than a guessed vh, or they leave dead space at one window height and clip
    # at another. The stacked fallback below still uses vh caps, correctly: once
    # the page scrolls, a cap is what stops one pane owning the whole screen.
    default_rules = src[:src.index("@media (max-width:1080px), (max-height:620px){")]
    assert "vh;" not in default_rules.replace("100vh;", ""), (
        "a scroll pane in the window-sized layout is back to a hard-coded "
        "viewport fraction")
    assert "min-height:0" in src, (
        "the grid has no min-height:0, so its panes grow the page instead of scrolling")

    # A column that cannot grow has to scroll. Without this the side panel's
    # last rows were simply unreachable and the replay's video card painted
    # over them.
    assert ".col.side{border-left:1px solid var(--line);overflow-y:auto;}" in src, (
        "the side column cannot scroll, so its content is clipped with no way to reach it")
    assert ".panel{padding:14px 16px;flex-shrink:0;}" in src, (
        "the side panels are squeezed to fit instead of overflowing into the scroll")

    # And the window-sized console only applies where there is room for it.
    # Below 1080px the columns stack and below ~620px tall there is no room for
    # a transcript, a side panel and the replay at once; holding 100vh there
    # does not make it fit, it moves the overflow somewhere with no scrollbar.
    assert "@media (max-width:1080px), (max-height:620px){" in src, (
        "the full-screen layout is unconditional again, so a short or narrow "
        "window clips content it cannot scroll to")
    fallback = src[src.index("@media (max-width:1080px), (max-height:620px){"):]
    fallback = fallback[:fallback.index("@media (max-width:1080px){")]
    assert "height:auto" in fallback and "overflow-y:visible" in fallback, (
        "the fallback does not release the fixed height, so it still cannot scroll")


def test_the_landing_page_does_not_call_the_researcher_console_a_reviewer_view():
    """This platform has two reviewing roles, and only one of them is a rater.

    A participant has the conversation, a researcher runs and monitors the
    study, and a RATER scores the recorded encounters afterwards from their own
    console at /rate with their own scoped token. "rater" is the word the rest of
    the codebase uses, by a wide margin.

    So the two entry points on the landing page are named for the role each one
    belongs to, and the researcher console is not called a reviewer view: that
    would point the reviewing word at the wrong one of two real reviewing jobs,
    on the page a new team member reads first.
    """
    src = LANDING.read_text(encoding="utf-8")

    assert "Conversation view" in src, "the participant entry point lost its name"
    assert "Researcher view" in src, "the researcher entry point lost its name"
    assert "Start a conversation" not in src, (
        "the two entry points are no longer named as a matched pair")

    # The word that would collide, in anything a visitor reads.
    visible = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    visible = re.sub(r"<style>.*?</style>", "", visible, flags=re.S)
    visible = re.sub(r"<script>.*?</script>", "", visible, flags=re.S)
    assert "eviewer" not in visible, (
        "a visible label calls something a reviewer; this platform's reviewing "
        "role is the rater, and it has its own console")

    # There is a rater door, and it must not be a published link.
    #
    # A rater's URL carries their own scoped token, so a public one cannot exist:
    # an <a href> to /rate would hand every visitor a 401 and tell them nothing.
    # The card takes a token from the person who has one and navigates from
    # there, which is the only shape that works. What must never appear is a
    # hardcoded rt_ token in the page, which would publish one rater's identity
    # to everyone who views source.
    assert 'href="/rate' not in src, (
        "the landing page publishes a rater link; a rater URL is personal and "
        "a bare /rate answers 401")
    assert "location.href = '/rate?token='" in src, (
        "the rater card no longer navigates with the token the rater supplied")
    assert not re.search(r"rt_[A-Za-z0-9]{8,}", src), (
        "a real rater token is hardcoded in the landing page")


def test_an_empty_transcript_says_why_it_is_empty():
    """A blank half-screen under a TRANSCRIPT heading reads as a broken console.

    Every other panel in that column already explains itself: the file list and
    the score panel both print "No session selected." The transcript printed
    nothing, so a researcher who opened the page and picked nothing, or who
    started the server without DATA_DIR and therefore had nothing to pick, got a
    heading over a void and no way to tell those two apart.

    They need different sentences, because they have different fixes: one is
    "choose from the list", the other is "the server is pointed at an empty
    directory".
    """
    src = RESEARCHER.read_text(encoding="utf-8")

    assert "function showTranscriptPlaceholder(" in src, (
        "the transcript no longer explains an empty state")
    fn = src[src.index("function showTranscriptPlaceholder("):]
    fn = fn[:fn.index("\n  function setRightPanel(")]

    assert "No session selected" in fn and "No sessions found" in fn, (
        "the two empty states are collapsed into one message, so 'pick one' and "
        "'this directory is empty' look the same")
    assert "DATA_DIR" in fn, (
        "the empty-directory case does not name the thing that fixes it")
    assert "sessionsCache.length" in fn, (
        "the placeholder no longer distinguishes the two cases")

    # It has to be painted on load and after the first list arrives, or the page
    # shows the wrong one of the two sentences until something else repaints.
    assert src.count("showTranscriptPlaceholder()") >= 3, (
        "the placeholder is not painted on every path that clears the transcript")
