"""The researcher console: its live failure strip, and its door into /evidence.

Two findings, one page. The first is the strip, below. The second is at the
bottom of this file: the way from the console into the evidence trace was a
12px inline link tucked under the steering log, in the same accent red that log
already uses for trigger ids, reading "Full evidence trace" — a name that means
nothing to anyone who has not already opened the page it names. It is now a
card with a full-width primary button and a description naming, panel by panel,
what is behind it, and that description is checked against static/evidence.html
so it cannot drift away from the page it describes.

The console is the only window in which anyone can intervene in a 7-12 minute
encounter that cannot be repeated, and until now it forwarded exactly three
frame types — state, transcript and steering — and dropped everything else on
the floor. Every mid-encounter failure went to events.jsonl and to nobody: an
agent turn that produced no transcript (so on screen the character simply never
speaks), a planted beat briefed and never spoken, a steering pass that errored
every turn, the director falling back to cast[0] for a whole group scenario,
the realtime gateway's own error frames. A collapsing encounter looked exactly
like a healthy one for the full run.

The socket now carries a fourth frame:

    {"type": "encounter_event", "kind": <str>, "t": <float|null>,
     "agent_id": <str|null>, "detail": <str|null, redacted>,
     "severity": "warn"|"error"}

What each block below is a regression test for:

1. **An encounter with no failures shows no chrome.** A strip permanently
   reading "0 failures" is furniture, and the eye stops seeing furniture. Its
   appearance is the signal, so it does not exist until there is something to
   say.
2. **A failure appears, with a running count and the most recent one.** Both,
   because one line of "planted beat never spoken" reads like a hiccup and
   "14 failures" reads like an encounter to abandon.
3. **An error is distinguishable from a warning without colour.** The badge
   changes glyph and shape and the count line says the word "error"; the red
   border is the third signal, not the only one.
4. **The full list is one click away and never opens itself.** Expanding is
   deliberate; a strip that grew on its own would move the transcript under a
   reader's eyes at the moment they are trying to read it.
5. **The strip never takes focus and never scrolls the column.** The researcher
   may be typing a note or holding a knob when the director dies.
6. **connect() clears it.** The related HIGH, closed server-side and still
   latent here: the strip is per-session state, and one session's collapse
   must not be on screen over the next session's transcript.

The page is HTML with an inline script, so it runs in a Node vm against a thin
DOM and a stub socket driving real frames of the contract shape, and every
assertion is made against the DOM the page actually builds. Skipped where node
is not installed; node is not a runtime dependency of the study.

Run from the repo root:

    python -m pytest tests/test_final_console.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESEARCHER = ROOT / "static" / "researcher.html"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structural checks. No node needed, so these hold on any machine that can run
# the suite at all.
# ---------------------------------------------------------------------------

def test_the_failure_strip_is_above_the_transcript_and_is_not_an_overlay():
    """It has to be in the transcript column's own flow.

    A toast, a fixed banner or an absolutely-positioned card would sit on top of
    the messages — hiding transcript at exactly the moment a researcher is
    reading it to decide whether to intervene. Sticky is the one position that
    keeps the strip on screen while the transcript auto-scrolls past it without
    covering anything at rest.
    """
    src = _src(RESEARCHER)
    strip = src.index('id="encEvents"')
    assert strip < src.index('id="msgs"'), "the failure strip is not above the transcript"

    rule = re.search(r"\.enc-events\s*\{[^}]*\}", src)
    assert rule, "static/researcher.html has no .enc-events rule"
    assert "position: sticky" in rule.group(0), (
        "the strip is not sticky: the transcript scrolls each new turn into view, so a strip "
        "that only lives at the top of the column is off screen for the whole encounter")
    assert "position: fixed" not in rule.group(0) and "position: absolute" not in rule.group(0), (
        "the strip is positioned out of flow, so it covers the transcript underneath it")

    lst = re.search(r"\.enc-events-list\s*\{[^}]*\}", src)
    assert lst and "max-height" in lst.group(0), (
        "the expanded list is unbounded: a bad encounter produces dozens of these and the "
        "transcript would be pushed off the screen")


def test_the_strip_reads_the_frame_the_server_actually_sends():
    """Contract (c) is a four-way agreement and this is the client half of it.

    Every field is load-bearing: `kind` is what happened, `severity` is whether
    it is survivable, `t` is where in the encounter to look, `agent_id` is who
    it happened to and `detail` is the redacted reason. A field this page never
    reads is a field the other three sides are maintaining for nothing.
    """
    src = _src(RESEARCHER)
    assert "m.type === 'encounter_event'" in src, \
        "the socket handler still drops every frame that is not state/transcript/steering"
    body = src[src.index("function appendEncEvent("):src.index("function resetEncEvents(")]
    for field in ("kind", "t", "agent_id", "detail", "severity"):
        assert re.search(rf"\bm\.{field}\b", body), \
            f"the strip never reads `{field}` off the frame the server sends"


# ---------------------------------------------------------------------------
# The harness.
# ---------------------------------------------------------------------------

CONSOLE_STUB_JS = r"""/* A thin browser for a page whose whole surface is a DOM and a socket.

   Deliberately not the media stub next door: this page opens no AudioContext
   and records nothing, and what it needs instead is a DOM faithful in the two
   places the assertions land — innerHTML='' really clears children, so a
   rebuilt list can be counted, and the ids the markup declares are known, so a
   test can tell an element the page builds from one this stub invented.
*/
'use strict';
const fs = require('fs');
const vm = require('vm');

function makeClock() {
  let now = 0, nextId = 1;
  const timers = [];
  const api = {
    now: () => now,
    setTimeout(fn, ms) { const t = { id: nextId++, at: now + (ms || 0), fn }; timers.push(t); return t.id; },
    clearTimeout(id) { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
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
    async flush() { for (let i = 0; i < 40; i++) await new Promise(r => setImmediate(r)); },
  };
  return api;
}

function makeDom() {
  const byId = new Map();
  const invented = new Set();
  function el(id) {
    const node = {
      id: id || '', tagName: 'DIV', style: {}, dataset: {}, children: [], attrs: {},
      textContent: '', value: '', disabled: false, className: '', listeners: {},
      _html: '',
      get innerHTML() { return this._html; },
      // A real innerHTML assignment replaces the children. The whole question
      // block 4 asks is how many rows the rebuilt list holds, and a stub that
      // kept the old ones would answer it wrong in the safe direction.
      set innerHTML(v) { this._html = String(v); this.children.length = 0; },
      classList: {
        _s: new Set(),
        add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
        contains(c) { return this._s.has(c); },
        toggle(c, on) { if (on === undefined) on = !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); },
      },
      addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
      removeEventListener() {},
      fire(ev, detail) { (this.listeners[ev] || []).slice().forEach(f => f(detail || { type: ev })); },
      appendChild(c) { this.children.push(c); return c; },
      prepend(c) { this.children.unshift(c); return c; },
      removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); },
      remove() {}, focus() {}, blur() {}, scrollIntoView() {},
      click() { this.fire('click'); },
      querySelector(sel) { return el(sel); },
      querySelectorAll: () => [],
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      get lastChild() { return this.children[this.children.length - 1] || null; },
      get firstChild() { return this.children[0] || null; },
    };
    return node;
  }
  const document = {
    getElementById(id) {
      if (!byId.has(id)) { byId.set(id, el(id)); invented.add(id); }
      return byId.get(id);
    },
    createElement(tag) { const n = el(''); n.tagName = String(tag).toUpperCase(); return n; },
    querySelector(sel) { return document.getElementById(sel); },
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    body: el('body'), head: el('head'),
    hidden: false, visibilityState: 'visible', activeElement: null,
  };
  return { document, byId, invented, el, $: (id) => document.getElementById(id) };
}

function makeFetch() {
  const calls = [];
  let routes = [];
  function res(status, body) {
    return { ok: status >= 200 && status < 300, status,
             json: async () => body, text: async () => JSON.stringify(body) };
  }
  function fetchStub(url, opts) {
    opts = opts || {};
    const call = { url: String(url), method: (opts.method || 'GET') };
    calls.push(call);
    const route = routes.find(r => call.url.includes(r.match));
    if (!route) return Promise.reject(new Error('unrouted fetch: ' + call.url));
    const out = route.fn(call);
    return out instanceof Error ? Promise.reject(out) : Promise.resolve(out);
  }
  return { fetch: fetchStub, calls, res, route(list) { routes = list; } };
}

function bootPage(page, routes, extra) {
  const src = fs.readFileSync(page, 'utf8');
  const markup = src.slice(0, src.indexOf('<script>'));
  const markupIds = new Set(Array.from(markup.matchAll(/\bid="([^"]+)"/g)).map(m => m[1]));
  const script = src.match(/<script>([\s\S]*)<\/script>/)[1];

  const clock = makeClock();
  const dom = makeDom();
  const net = makeFetch();
  net.route(routes || []);
  const sockets = [];

  function Socket(url) {
    this.url = url; this.readyState = 0; this.sent = []; this.listeners = {};
    this.addEventListener = (ev, fn) => { (this.listeners[ev] = this.listeners[ev] || []).push(fn); };
    this.send = (m) => { this.sent.push(m); };
    this.close = () => { this.readyState = 3; };
    this.open = () => { this.readyState = 1; (this.listeners.open || []).forEach(f => f({})); };
    this.drop = () => { this.readyState = 3; (this.listeners.close || []).forEach(f => f({})); };
    // Real frames of the contract shape, delivered the way the server does:
    // one JSON string per message event.
    this.deliver = (obj) => {
      (this.listeners.message || []).forEach(f => f({ data: JSON.stringify(obj) }));
    };
    sockets.push(this);
  }

  const sandbox = {
    console, JSON, Math, Date, Promise, Object, Array, String, Number, Boolean,
    Set, Map, RegExp, Error, TypeError, isNaN, isFinite, parseInt, parseFloat,
    encodeURIComponent, decodeURIComponent, URLSearchParams, AbortController,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    fetch: net.fetch, document: dom.document, WebSocket: Socket,
    performance: { now: () => clock.now() },
    alert() {}, confirm: () => true, open: () => null,
  };
  sandbox.navigator = {};
  sandbox.addEventListener = () => {};
  sandbox.removeEventListener = () => {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  Object.assign(sandbox, extra || {});
  const ctx = vm.createContext(sandbox);
  vm.runInContext(script, ctx, { filename: require('path').basename(page) });
  return {
    ctx, sandbox, clock, dom, net, sockets, markupIds,
    $: dom.$,
    inMarkup: (id) => markupIds.has(id),
    run(code) { return vm.runInContext(code, sandbox); },
  };
}

module.exports = { bootPage };
"""


CONSOLE_HARNESS = r"""/* Drives static/researcher.html's own socket handler with real
   encounter_event frames and reads what the page painted. Nothing is asserted
   by looking at the file. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];

const ROUTES = [
  { match: '/files', fn: (c) => ({ ok: true, status: 200, json: async () => [], text: async () => '[]' }) },
  { match: '/api/scenarios', fn: () => ({ ok: true, status: 200, json: async () => [], text: async () => '[]' }) },
  { match: '/api/', fn: () => ({ ok: true, status: 200, json: async () => [], text: async () => '[]' }) },
];

function boot() {
  return bootPage(PAGE, ROUTES, {
    location: { search: '?key=k', href: 'http://t/researcher', protocol: 'http:', host: 't',
                reload() {} },
  });
}

(async () => {
  const b = boot();
  const $ = b.$;

  // The strip has to be an element the page declares, not one this stub
  // invented on first lookup: getElementById here answers every id, so an
  // assertion about a missing element would otherwise pass against a page that
  // has no strip at all.
  ['encEvents', 'encEventsBadge', 'encEventsCount', 'encEventsLatest',
   'encEventsToggle', 'encEventsList'].forEach(id => {
    assert(b.inMarkup(id), `static/researcher.html declares no #${id}: the console has no ` +
      'place to show a failure, so a collapsing encounter still looks healthy');
  });

  b.run("sessionsCache = [{ id: 's_1', status: 'active', title: 't', turn_count: 0 }," +
        "                 { id: 's_2', status: 'active', title: 't2', turn_count: 0 }];");
  b.ctx.connect('s_1');
  await b.clock.flush();
  assert.strictEqual(b.sockets.length, 1, 'no socket was opened');
  const sock = b.sockets[0];
  sock.open();

  // The strip must never move the researcher's caret or the column. This throws
  // from inside the page's own render if it ever tries.
  const strip = $('encEvents');
  strip.focus = () => { throw new Error('the failure strip took focus mid-encounter'); };
  strip.scrollIntoView = () => { throw new Error('the failure strip scrolled the transcript column'); };

  // --- 1. a healthy encounter shows nothing at all ------------------------
  assert.strictEqual(strip.style.display, 'none',
    'the strip is on screen before anything has gone wrong: empty chrome is furniture, and ' +
    'the eye stops seeing furniture');
  assert.strictEqual($('encEventsCount').textContent, '',
    'the strip is already counting something on a session with no failures');

  // --- 2. a warning: a running count and the most recent event ------------
  // A planted beat briefed and never spoken. On the old console this was
  // store-only: the scenario's whole manipulation silently did not happen.
  sock.deliver({ type: 'encounter_event', kind: 'trigger_undelivered', t: 73.4,
                 agent_id: 'a_dana', detail: null, severity: 'warn' });
  assert.notStrictEqual(strip.style.display, 'none',
    'a mid-encounter failure arrived and the console showed nothing: this is the defect');
  const warnCount = $('encEventsCount').textContent;
  assert(/\b1\b/.test(warnCount), 'no running count of failures: ' + JSON.stringify(warnCount));
  assert(!/error/i.test(warnCount), 'a warning is being reported as an error: ' + warnCount);
  const warnBadge = $('encEventsBadge').textContent;
  assert(warnBadge, 'the severity badge is empty, so severity is carried by colour alone');
  assert(!strip.classList.contains('sev-error'), 'a warning escalated the whole strip to error');
  const latest = $('encEventsLatest').textContent;
  assert(/1:13/.test(latest),
    'the strip does not say when in the encounter this happened: ' + latest);
  assert(/planted beat/i.test(latest),
    'the strip shows a raw event kind rather than what went wrong: ' + latest);

  // --- 3. an error, told apart from a warning without colour --------------
  // `voice_error` and not `realtime_error`: this is the kind the runner really
  // sends, and it is the events.jsonl type as well, so the live line and the
  // archived row join. Driving the harness with a kind no emitter sends let a
  // dead label key pass here for as long as it existed.
  sock.deliver({ type: 'encounter_event', kind: 'voice_error', t: 130,
                 agent_id: null, detail: 'gateway closed the stream (1011)', severity: 'error' });
  assert(/realtime gateway error/i.test($('encEventsLatest').textContent),
    'the gateway erroring reaches the researcher as a raw event name rather than as ' +
    'something to act on: ' + $('encEventsLatest').textContent);
  const errCount = $('encEventsCount').textContent;
  assert(/\b2\b/.test(errCount), 'the count did not advance: ' + errCount);
  assert(/error/i.test(errCount),
    'nothing in the text says one of these is an error, so severity is colour alone: ' + errCount);
  assert(strip.classList.contains('sev-error'),
    'an error left the strip looking exactly like a warning');
  assert.notStrictEqual($('encEventsBadge').textContent, warnBadge,
    'the badge glyph is the same for a warning and an error: a researcher with a red-green ' +
    'deficiency, or anyone reading this projected, has no signal at all');
  assert(/gateway closed the stream/.test($('encEventsLatest').textContent),
    'the reason the gateway failed is not shown: ' + $('encEventsLatest').textContent);

  // --- 4. the full list is one click away, and never opens itself ---------
  const list = $('encEventsList');
  assert.strictEqual(list.style.display, 'none',
    'the list expanded on its own, shoving the transcript down the column mid-encounter');
  $('encEventsToggle').click();
  assert.notStrictEqual(list.style.display, 'none', 'the toggle does not open the list');
  assert.strictEqual(list.children.length, 2,
    'the expanded list does not hold every failure: ' + list.children.length);
  assert(!/\berror\b/.test(list.children[0].className),
    'the warning row is marked as an error');
  assert(/\berror\b/.test(list.children[1].className),
    'the error row is indistinguishable from the warning row in the expanded list');
  assert.strictEqual($('encEventsToggle').getAttribute('aria-expanded'), 'true',
    'the toggle does not report its own state to a screen reader');

  // A third event while the list is open must not collapse it, and must not
  // renumber the ones already there.
  sock.deliver({ type: 'encounter_event', kind: 'auto_steer_error', t: null,
                 agent_id: null, detail: 'director returned no JSON', severity: 'error' });
  assert.notStrictEqual(list.style.display, 'none',
    'a new failure collapsed the list the researcher had open');
  assert.strictEqual(list.children.length, 3, 'the new failure is not in the list');
  assert(/\b3\b/.test($('encEventsCount').textContent),
    'the count is stale: ' + $('encEventsCount').textContent);

  // An unlabelled kind is still shown. The server side owns the list of
  // failures worth broadcasting; a console that drops the ones it has no label
  // for reintroduces this whole defect one event type at a time.
  sock.deliver({ type: 'encounter_event', kind: 'some_future_failure', t: 5,
                 agent_id: null, detail: null, severity: 'warn' });
  assert.strictEqual(list.children.length, 4, 'an unrecognised failure kind was dropped');

  // --- 5. and it never took focus or scrolled the column ------------------
  // (the spies above throw if it did; reaching here is the assertion)

  // --- 6. connect() clears it, so one session's collapse is not reported
  //        against the next -------------------------------------------------
  b.ctx.connect('s_2');
  await b.clock.flush();
  assert.strictEqual($('encEvents').style.display, 'none',
    "the previous session's failures are still on screen over this session's transcript");
  assert.strictEqual($('encEventsCount').textContent, '',
    'the failure count survived the session switch: ' + $('encEventsCount').textContent);
  assert.strictEqual($('encEventsList').children.length, 0,
    'the previous session\'s failure rows are still in the list');

  const sock2 = b.sockets[b.sockets.length - 1];
  sock2.open();
  sock2.deliver({ type: 'encounter_event', kind: 'transcript_missing', t: 12,
                  agent_id: 'a_dana', detail: null, severity: 'warn' });
  const fresh = $('encEventsCount').textContent;
  assert(/\b1\b/.test(fresh) && !/\b[45]\b/.test(fresh),
    'the new session is counting the previous session\'s failures too: ' + fresh);

  // --- 7. the beat that was never even briefed ----------------------------
  // trigger_brief_failed is the one kind that says the scenario's planted
  // manipulation did not reach the character at all — a different, worse thing
  // than trigger_undelivered, where the brief landed and the beat was not
  // spoken. Rendered as "trigger brief failed" the distinction is gone.
  sock2.deliver({ type: 'encounter_event', kind: 'trigger_brief_failed', t: 40,
                  agent_id: 'a_dana', detail: 'beat b2 was never briefed: 503',
                  severity: 'error' });
  const briefed = $('encEventsLatest').textContent;
  assert(/never briefed/i.test(briefed),
    'a planted beat that never reached the character shows as a raw event name: ' + briefed);

  console.log('CONSOLE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def _run(tmp_path, page):
    """Run the harness against `page`; returns (returncode, output)."""
    (tmp_path / "stub.js").write_text(CONSOLE_STUB_JS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(CONSOLE_HARNESS, encoding="utf-8")
    # encoding pinned rather than left to the machine's locale: node emits UTF-8
    # everywhere and this page contains characters cp1252 cannot represent, so a
    # harness failure decoded through the wrong codec reaches a Windows
    # contributor as mojibake at exactly the moment they need to read it.
    proc = subprocess.run([_node(), str(harness), str(page)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def test_a_collapsing_encounter_does_not_look_like_a_healthy_one(tmp_path):
    """The whole finding: the console renders the failures the socket carries,
    counts them, tells an error from a warning without colour, and shows nothing
    when there is nothing to show."""
    code, out = _run(tmp_path, RESEARCHER)
    assert code == 0, out
    assert "CONSOLE OK" in out, out


def test_the_harness_is_red_against_the_console_that_had_no_strip(tmp_path):
    """The demonstration, kept in the suite rather than in a commit message.

    A test that passes against the page before the fix proves nothing, so the
    committed page is asked the same questions and must fail them. Self-
    retiring: once the strip is in HEAD there is no longer a before to compare
    against and this skips.
    """
    if not shutil.which("git"):
        pytest.skip("git is not on PATH")
    proc = subprocess.run(["git", "show", "HEAD:static/researcher.html"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        pytest.skip("HEAD has no static/researcher.html to compare against")
    if "encounter_event" in (proc.stdout or ""):
        pytest.skip("HEAD already carries the failure strip; there is no before left to fail")
    before = tmp_path / "researcher.before.html"
    before.write_text(proc.stdout, encoding="utf-8")
    code, out = _run(tmp_path, before)
    assert code != 0, (
        "the harness passes against a console that drops every frame but "
        "state/transcript/steering, so it is not testing the fix:\n" + out)
    assert "declares no #encEvents" in out, (
        "the harness failed against the old page for some reason other than the missing "
        "strip, so it is not the failure this test claims to demonstrate:\n" + out)


# ---------------------------------------------------------------------------
# The label map against the emitters.
#
# The strip renders an unlabelled kind rather than dropping it, which keeps the
# console honest but not useful: `voice_error` on screen as "voice error" tells
# a researcher a string, where "realtime gateway error" tells them the gateway
# is the thing to look at. The two sides drifted once already — the page carried
# a label for `realtime_error`, a kind no emitter has ever sent, while the kind
# that is sent (`voice_error`, matching the events.jsonl type so the live line
# and the archived row join) fell through to the fallback. Both halves of that
# drift are asserted below, because a one-way subset check would have passed on
# the dead key and gone red only on the missing one.
# ---------------------------------------------------------------------------

RVS = ROOT / "server" / "realtime_voice_session.py"
APP = ROOT / "server" / "app.py"


def _kinds_the_server_can_emit() -> set[str]:
    """Every `kind` string that can reach the researcher socket, read off source.

    Derived rather than listed so this goes red the next time someone adds an
    emitter without a label, which is the class of bug rather than the instance
    of it. Three producers, because there are three:

      * `_encounter_event` / `_encounter_event_soon` in the runner, called with
        the kind as a string literal;
      * the runner's store tee, which forwards a session-written event by its
        events.jsonl type name (the keys of its `forwarded` map);
      * `_report_encounter_failure` in app.py, for the failures that escape the
        runner entirely and surface in the socket handler.
    """
    rvs = _src(RVS)
    app_src = _src(APP)
    kinds = set(re.findall(r"_encounter_event(?:_soon)?\(\s*\n?\s*\"(\w+)\"", rvs))
    tee = re.search(r"forwarded\s*=\s*\{(.*?)\}", rvs, re.S)
    if tee:
        kinds |= set(re.findall(r"\"(\w+)\"\s*:", tee.group(1)))
    kinds |= set(re.findall(
        r"_report_encounter_failure\(\s*\w+,\s*\"(\w+)\"", app_src))
    return kinds


def _labelled_kinds() -> set[str]:
    src = _src(RESEARCHER)
    body = re.search(r"const ENC_EVENT_LABEL\s*=\s*\{(.*?)\n  \};", src, re.S)
    assert body, "static/researcher.html no longer has an ENC_EVENT_LABEL map"
    return set(re.findall(r"^\s*(\w+)\s*:", body.group(1), re.M))


def test_every_kind_the_server_emits_has_a_label_on_the_console():
    """The joinability contract, checked from the consuming side.

    `voice_error` and `trigger_brief_failed` are the two a researcher most needs
    to recognise mid-encounter — the gateway erroring and a planted beat that
    was never even briefed — and both arrived unlabelled while a key for a kind
    nothing emits sat in the map beside them.
    """
    emitted = _kinds_the_server_can_emit()
    # Guard the parse itself: a refactor that broke the regex would otherwise
    # make this test pass by finding nothing to check.
    assert len(emitted) >= 8, (
        "the emitter parse found almost nothing, so this test is asserting against an "
        f"empty set rather than the server's real inventory: {sorted(emitted)}")
    assert "voice_error" in emitted, (
        "voice_error is no longer emitted; if the runner renamed it, rename the "
        "label key with it — the console and events.jsonl have to name one thing")

    missing = sorted(emitted - _labelled_kinds())
    assert not missing, (
        "the server can emit these kinds and static/researcher.html has no label for "
        f"them, so they reach a watching researcher as raw event names: {missing}")


def test_the_label_map_carries_no_key_no_emitter_sends():
    """A dead key is invisible: it renders correctly for a kind never sent.

    This is the half of the drift that has no symptom on screen, so it can only
    be caught here. A label for a kind the server cannot send is also actively
    misleading to the next person editing the page, who reads the map as the
    inventory of what can arrive.
    """
    dead = sorted(_labelled_kinds() - _kinds_the_server_can_emit())
    assert not dead, (
        "static/researcher.html labels kinds no emitter in server/ sends, so the map "
        f"misstates what can arrive on the socket: {dead}")


# ---------------------------------------------------------------------------
# The door into the evidence trace.
#
# The console is where a researcher spends their time, and the evidence trace
# is the second view they need — the per-turn alignment of participant line,
# stage direction and actor line, plus fidelity, ESCI coverage and session
# health. There is exactly one route to it from anywhere in the study's UI: the
# link at the bottom of a closed session's right column. It was a 12px
# inline-block anchor sitting directly under a steering log that routinely runs
# to twenty items, coloured in the same accent red that log uses for trigger
# ids, and labelled with the study's own name for the page. Three ways of not
# being found, stacked.
#
# What is asserted here, and why each one is a defect and not a preference:
#
#   * it renders as the panel's primary action rather than as one more line of
#     the log it sits under;
#   * it says what is behind it, in the researcher's language, naming the
#     panels /evidence actually paints — and that list is READ OFF
#     static/evidence.html rather than typed here, so a panel renamed or
#     dropped on that page turns this red instead of leaving the console
#     advertising something that is gone;
#   * it does not promise the recording plays there, because it does not: the
#     replay pane moves a playhead and a timecode, and its own hint says video
#     sync is still pending;
#   * it still lands on the encounter being looked at, and still carries `key`
#     when the server runs with SESSION_KEY. That part already worked and is
#     pinned rather than fixed — the card was rebuilt around the anchor, and a
#     rebuild is exactly when a query string gets dropped.
#
# Not asserted, deliberately: nothing about rater.html. The rater is blind this
# round and the evidence trace shows planted trigger ids and their ESCI tags,
# so this door belongs on this page and on no other.
# ---------------------------------------------------------------------------

EVIDENCE = ROOT / "static" / "evidence.html"


def _rule(src: str, selector: str) -> str:
    """The first CSS rule for `selector`, so an assertion can read its body."""
    m = re.search(re.escape(selector) + r"\s*\{[^}]*\}", src)
    assert m, f"static/researcher.html has no {selector} rule"
    return m.group(0)


def _evidence_card(src: str) -> str:
    """The card's markup, sliced out of the closed-session panel."""
    assert '<div class="evidence-card"' in src, (
        "static/researcher.html has no evidence card: the console's only route into "
        "the evidence trace is gone")
    start = src.index('<div class="evidence-card"')
    end = src.index('id="rightControls"')
    assert end > start, (
        "the evidence card is no longer inside the closed-session panel, so it is "
        "either on screen during a live encounter or nowhere")
    return src[start:end]


def _card_prose(src: str) -> str:
    """What a researcher actually reads on the card, tags stripped."""
    return re.sub(r"<[^>]+>", " ", _evidence_card(src))


def _evidence_panels() -> list[str]:
    """The panel headings /evidence paints, read off that page.

    Derived rather than listed so this stays a check on two files agreeing,
    which is the class of bug. A hand-typed list here would agree with itself
    forever while the page underneath changed.
    """
    src = _src(EVIDENCE)
    out = [re.sub(r"<[^>]*>", "", h).strip()
           for h in re.findall(r"<h3>(.*?)</h3>", src, re.S)]
    return [h for h in out if h]


def _evidence_turn_columns() -> list[str]:
    """The named columns of the per-turn table on /evidence."""
    src = _src(EVIDENCE)
    head = re.search(r'<div class="thead">(.*?)</div>', src, re.S)
    assert head, "static/evidence.html no longer has a per-turn table header"
    cols = [re.sub(r"<[^>]*>", "", c).strip()
            for c in re.findall(r"<span>(.*?)</span>", head.group(1), re.S)]
    # "t (s)" is the timestamp gutter, not a party to the conversation.
    return [c for c in cols if re.fullmatch(r"[A-Za-z ]+", c)]


def test_the_evidence_trace_is_a_button_and_not_one_more_line_of_the_log():
    """It has to read as the way out of this page.

    An inline anchor at the foot of a twenty-item list, in that list's own
    colour and a size smaller than that list's own text, is a line of the list.
    Full width, filled, and inside the card that explains it, it is the one
    thing on a closed session's panel that looks clickable.

    It stays an <a>: it has to open a new tab and carry a query string, and the
    console must not lose the session being watched by navigating away from it.
    What changes is that nothing about it reads as a link.
    """
    src = _src(RESEARCHER)

    assert (src.index('id="rightClosed"') < src.index('id="steerBody"')
            < src.index('id="evidenceLink"') < src.index('id="rightControls"')), (
        "the evidence button has left the bottom of the closed-session column: it belongs "
        "under the steering log, on the state a researcher reviewing recorded encounters "
        "is actually in")

    tag = re.search(r'<a id="evidenceLink"[^>]*>', src)
    assert tag, (
        "#evidenceLink is no longer an anchor, so it can no longer open /evidence in a "
        "new tab and the console loses the session being watched")
    assert 'target="_blank"' in tag.group(0), (
        "the evidence trace now replaces the console in the same tab, dropping the live "
        "socket and the researcher's place in the transcript")
    assert "noopener" in tag.group(0), (
        "the new tab is opened holding a handle back on the console's window")

    rule = _rule(src, ".evidence-btn")
    assert "display: block" in rule and "width: 100%" in rule, (
        "the evidence button is inline again, so it is the width of its own text at the "
        f"bottom of the steering log rather than an object in its own right: {rule}")
    assert "background: var(--accent)" in rule, (
        "the button is not filled, so on a panel of bordered white cards there is nothing "
        f"to say it is the action: {rule}")
    assert "text-decoration: none" in rule, (
        f"the button is underlined, which is the text link this replaced: {rule}")
    size = re.search(r"font-size:\s*(\d+(?:\.\d+)?)px", rule)
    assert size and float(size.group(1)) >= 13, (
        "the evidence button is back to the 12px of the old inline link, smaller than the "
        f"steering-log rows it sits under: {rule}")


def _div_depth(fragment: str) -> int:
    """Open `<div`s minus `</div>`s in a slice of markup."""
    return len(re.findall(r"<div\b", fragment)) - len(re.findall(r"</div>", fragment))


def test_the_button_is_outside_the_steering_log_s_own_scroll_box():
    """The defect underneath the cosmetic one, and the reason for the nesting.

    `.steer-log` is max-height:200px, overflow-y:auto — it has to be, because
    the live auto-steer log shares that class and grows without limit. The
    evidence link was the last child of it. On s_1773142745_384dad, a four-
    direction encounter in the demo data, measured in a browser at 1500x900:
    the log's content is 682px inside a 200px window and the link sat at offset
    666 of it, 482px below the fold of its own scroll box. Not "hard to
    notice" — not rendered on screen at all unless the researcher had already
    scrolled a list of stage directions to its end. Every longer encounter is
    worse.

    So the card is a sibling of the log, not a child, and this is the assertion
    that keeps it one. Skips if the log ever stops being a scroll box, at which
    point being inside it would cost nothing.
    """
    src = _src(RESEARCHER)
    log_rule = _rule(src, ".steer-log")
    if "max-height" not in log_rule or "overflow" not in log_rule:
        pytest.skip(".steer-log is no longer a bounded scroll box")

    _evidence_card(src)   # asserts the card exists, with the message for that
    between = src[src.index('class="steer-log"'):src.index('<div class="evidence-card"')]
    # The slice starts inside the log's own opening tag, so its `<div` is not
    # counted: a card that is still a descendant leaves the depth at 0 or above,
    # and one that is a sibling leaves it negative on the log's own `</div>`.
    assert _div_depth(between) < 0, (
        "the evidence card is back inside .steer-log, which is a 200px scroll box: on any "
        "encounter with more than a couple of stage directions the console's only route "
        "into the evidence trace is below the fold of a list nobody scrolls to the end of")


def test_the_card_names_the_panels_the_evidence_trace_actually_paints():
    """"Evidence trace" names nothing to someone who has not seen one.

    The card has to say what is in there, and it has to keep saying something
    true. Both halves are checked from static/evidence.html: every panel that
    page paints and every party in its per-turn table must be named on the card,
    so renaming or removing a panel there goes red here rather than leaving the
    console advertising a view that no longer exists.
    """
    panels = _evidence_panels()
    # Guard the parse: a refactor that broke the regex would otherwise make this
    # pass by finding nothing to look for.
    assert len(panels) >= 4, (
        "the parse of static/evidence.html found almost no panels, so this test is "
        f"checking the card against an empty list: {panels}")
    cols = _evidence_turn_columns()
    assert len(cols) >= 3, (
        "the parse found fewer than three named columns in the per-turn table, so the "
        f"three-way alignment this card promises cannot be checked: {cols}")

    prose = _card_prose(_src(RESEARCHER)).lower()
    missing = [name for name in panels + cols if name.lower() not in prose]
    assert not missing, (
        "static/evidence.html shows these and the console's card does not mention them, "
        "so a researcher deciding whether to open it is deciding on a description of a "
        f"different page: {missing}")

    # The label on the button itself has to be an action. "Full evidence trace"
    # is a noun phrase naming a page nobody has seen.
    label = re.search(r'<a id="evidenceLink"[^>]*>(.*?)</a>', _src(RESEARCHER), re.S)
    assert label and re.search(r"\bopen\b", label.group(1), re.I), (
        "the button's own text does not say that it opens anything: "
        + (label.group(1).strip() if label else "(no label)"))


def test_the_card_does_not_promise_the_recording_plays_there():
    """Describe what is there, not what sounds good.

    The replay pane on /evidence moves a playhead and prints a timecode; the
    video panel beside it is a placeholder, and its own hint says video sync is
    pending clock_offset_ms in the manifest. A card promising a researcher they
    can watch the encounter there sends them to a grey rectangle, which costs
    more trust than a dull link ever cost.

    Self-retiring: when that page really does play back, this skips and the card
    can say so.
    """
    if "video sync pending" not in _src(EVIDENCE):
        pytest.skip("/evidence no longer marks video sync pending; the card may say more")
    prose = _card_prose(_src(RESEARCHER)).lower()
    for promise in ("watch", "playback", "play back", "plays"):
        assert promise not in prose, (
            "the card tells a researcher they can watch the recording on /evidence, and "
            f"they cannot — the video panel there is still a placeholder: {promise!r}")


# --- and it still opens on the right encounter, with the key ---------------

EVIDENCE_HARNESS = r"""/* Drives static/researcher.html's own closed-session panel and reads the href
   the page put on the evidence button. Nothing is asserted by looking at the
   file. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];
const SEARCH = process.argv[3] || '';

const RECORD = {
  steering_log: [{ at: 12, agent_id: 'a_dana', trigger_id: 't3_defensiveness',
                   esci: ['EI'], direction: 'Push back on the new date.' }],
};

const ROUTES = [
  // Ahead of the catch-all: the steering log reads record.json, and an empty
  // array here would drive the "could not load" branch instead of the one this
  // harness is about.
  { match: '/record', fn: () => ({ ok: true, status: 200,
      json: async () => RECORD, text: async () => JSON.stringify(RECORD) }) },
  { match: '/api/', fn: () => ({ ok: true, status: 200,
      json: async () => [], text: async () => '[]' }) },
];

(async () => {
  const b = bootPage(PAGE, ROUTES, {
    location: { search: SEARCH, href: 'http://t/researcher', protocol: 'http:',
                host: 't', reload() {} },
  });
  const $ = b.$;

  // Both have to be ids the page declares, not ones this stub invented on
  // first lookup: getElementById here answers every id, so an assertion about
  // a missing element would otherwise pass against a page that has neither.
  ['evidenceCard', 'evidenceLink'].forEach(id => {
    assert(b.inMarkup(id), `static/researcher.html declares no #${id}: the console has ` +
      'no signposted way into the evidence trace');
  });

  b.run("sessionsCache = [{ id: 's_9', status: 'complete', title: 't', turn_count: 4 }," +
        "                 { id: 's_4', status: 'complete', title: 't2', turn_count: 4 }];");
  b.run("currentSessionId = 's_9';");
  b.ctx.setRightPanel('closed');
  await b.clock.flush();

  assert.strictEqual($('rightClosed').style.display, 'block',
    'the closed-session panel did not open, so nothing below is being tested');

  const first = String($('evidenceLink').href || '');
  assert(first.indexOf('/evidence') === 0,
    'the button does not point at the evidence trace: ' + JSON.stringify(first));
  assert(/[?&]session=s_9(?:&|$)/.test(first),
    'the button opens the evidence trace on whatever encounter it likes rather than ' +
    'the one on screen: ' + first);
  if (/key=/.test(SEARCH)) {
    assert(/[?&]key=k7(?:&|$)/.test(first),
      'the console is running against a server with SESSION_KEY and the button drops ' +
      'the key, so it opens on a rejection: ' + first);
  } else {
    assert(!/[?&]key=/.test(first),
      'a key appeared in the URL on a server that has none: ' + first);
  }

  // Switching to another finished encounter has to move the button with it. A
  // stale href is the worst failure available here: it opens, it looks right,
  // and it is the wrong encounter's evidence.
  b.run("currentSessionId = 's_4';");
  b.ctx.setRightPanel('closed');
  await b.clock.flush();
  const second = String($('evidenceLink').href || '');
  assert(/[?&]session=s_4(?:&|$)/.test(second),
    'the button still points at the previously selected encounter: ' + second);

  console.log('EVIDENCE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _run_harness(tmp_path, page, harness_src, name, *args):
    """Run an arbitrary harness against `page`; returns (returncode, output)."""
    (tmp_path / "stub.js").write_text(CONSOLE_STUB_JS, encoding="utf-8")
    harness = tmp_path / name
    harness.write_text(harness_src, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(page), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def test_the_button_opens_the_encounter_the_researcher_is_looking_at(tmp_path):
    """With SESSION_KEY set, which is how the study actually runs it."""
    code, out = _run_harness(tmp_path, RESEARCHER, EVIDENCE_HARNESS,
                             "evidence.js", "?key=k7")
    assert code == 0, out
    assert "EVIDENCE OK" in out, out


def test_the_button_carries_no_key_when_the_server_is_running_without_one(tmp_path):
    """The other half: an unkeyed console must not invent a `key=` parameter."""
    code, out = _run_harness(tmp_path, RESEARCHER, EVIDENCE_HARNESS,
                             "evidence.js", "")
    assert code == 0, out
    assert "EVIDENCE OK" in out, out


def test_the_evidence_harness_is_red_against_the_console_that_had_no_card(tmp_path):
    """The demonstration, kept in the suite rather than in a commit message.

    Self-retiring: once the card is in HEAD there is no longer a before to
    compare against and this skips.
    """
    if not shutil.which("git"):
        pytest.skip("git is not on PATH")
    proc = subprocess.run(["git", "show", "HEAD:static/researcher.html"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        pytest.skip("HEAD has no static/researcher.html to compare against")
    if "evidenceCard" in (proc.stdout or ""):
        pytest.skip("HEAD already carries the evidence card; there is no before left to fail")
    before = tmp_path / "researcher.before.html"
    before.write_text(proc.stdout, encoding="utf-8")
    code, out = _run_harness(tmp_path, before, EVIDENCE_HARNESS,
                             "evidence.js", "?key=k7")
    assert code != 0, (
        "the harness passes against a console with no signposted way into the evidence "
        "trace, so it is not testing the fix:\n" + out)
    assert "declares no #evidenceCard" in out, (
        "the harness failed against the old page for some reason other than the missing "
        "card, so it is not the failure this test claims to demonstrate:\n" + out)
