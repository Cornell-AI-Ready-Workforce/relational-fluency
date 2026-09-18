"""The defects a researcher meets while testing the platform by hand.

This round's brief was "package everything up ... make sure that it is the best
version with no errors", and the sweep that preceded it drove the three
surfaces a researcher actually touches — the participant encounter, the rating
console and the evidence trace — with a real browser, a real microphone and the
deployment's own .env. What it found is below, one section per surface, and
every test here failed on the tree it was written against.

Two rules held throughout:

*   Nothing here asserts on a string a human will read unless the string is the
    defect. Four of these findings ARE wording — a page that tells a
    participant "nothing is lost on your side" while it discards their
    conversation is not a cosmetic problem — so those tests name the false
    sentence and require it gone, rather than pinning the replacement.

*   The client-side findings are driven, not read. static/v2.html and
    static/rater.html are run in a Node vm against the DOM stub
    tests/test_client_blockers.py already builds, and the assertion is made by
    calling the page's own functions and reading what it did. A test that
    greps for the fix cannot tell a fix from a comment about one.

Run from the repo root:

    python -m pytest tests/test_round8_package_fixes.py
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from test_browser_compat import STUB_JS  # noqa: E402
from test_client_blockers import _run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "static" / "v2.html"
EVIDENCE = ROOT / "static" / "evidence.html"
APP = ROOT / "server" / "app.py"


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the client harnesses need it")
    return node


# =========================================================================== #
# 1. The participant page: a permission prompt nobody answers
#
# Chrome leaves the getUserMedia promise pending — forever — when the
# participant neither allows nor blocks the microphone prompt. Both call sites
# awaited that promise with nothing racing it:
#
#   * the audio check sat on "Listening…" with the 8-second "Nothing heard"
#     timer never armed, because it is scheduled AFTER the await;
#   * "Start conversation" disabled itself, awaited startCapture(), and stayed
#     disabled, with an empty transcript and no notice of any kind. The only
#     control left on the screen was "Stop and leave the study".
#
# An explicit Block was always handled well. This is the case where the
# participant looks away, or dismisses the prompt with Escape.
# =========================================================================== #

UNANSWERED_MIC_HARNESS = r"""/* The microphone prompt is shown and never answered.

   getUserMedia returns a promise that never settles, which is what Chrome does
   for a prompt left open — and what the page had no defence against. Every
   other part of the browser here is a working one: this machine has a
   microphone, has a camera, and is waiting on one click that is not coming.

   The assertion is that startCapture SETTLES, because everything the
   participant sees hangs off that. startSession already re-enables #startBtn
   and writes CAPTURE_MESSAGES into the transcript in its catch — that half was
   never broken. What it could not do was reach the catch. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];

(async () => {
  const b = bootPage(PAGE, {}, {
    location: { search: '?run=r_1', href: 'http://t/v2?run=r_1', reload() {}, replace() {} },
  });
  b.net.route([{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);

  let asked = 0;
  b.sandbox.navigator.mediaDevices = {
    getUserMedia: () => { asked++; return new Promise(() => {}); },
  };

  let settled = null, err = null;
  b.ctx.startCapture().then(() => { settled = 'resolved'; },
                            (e) => { settled = 'rejected'; err = e; });
  // Two minutes of virtual time. Any bound the page picks is well inside this,
  // and a participant who has not answered in two minutes is not going to.
  await b.clock.advance(120000);

  assert(asked > 0, 'startCapture never asked for the microphone at all');
  assert.strictEqual(settled, 'rejected',
    'startCapture never settled against a permission prompt nobody answered, so '
    + '"Start conversation" stays disabled with an empty transcript and the only '
    + 'control left on the screen is "Stop and leave the study"');

  // A rejection the page cannot name is a rejection the participant is told
  // the wrong thing about, so the kind has to map to a real sentence.
  const kind = b.ctx.captureKindFor(err);
  const msg = b.run('CAPTURE_MESSAGES')[kind];
  assert(msg && msg.length > 20,
    'the timeout produced capture kind ' + JSON.stringify(kind) + ', which has no message');
  assert(/microphone|permission/i.test(msg),
    'the message for an unanswered prompt does not mention the microphone or the '
    + 'permission: ' + JSON.stringify(msg));

  console.log('UNANSWERED MIC OK');
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


def _run_media(harness_src: str, tmp_path: Path, page: Path = V2) -> str:
    (tmp_path / "stub.js").write_text(STUB_JS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(harness_src, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(page)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_an_unanswered_microphone_prompt_does_not_freeze_the_encounter(tmp_path):
    """The participant is told, and Start can be pressed again."""
    assert "UNANSWERED MIC OK" in _run_media(UNANSWERED_MIC_HARNESS, tmp_path)


def test_the_audio_check_gives_up_on_a_prompt_that_is_never_answered():
    """The mic check's own timer must not be scheduled behind the await.

    Measured: with the prompt left open, micStatus reads "Listening…" for as
    long as anyone waits, because `micTimer` is armed on the line AFTER
    `await getUserMedia`. Whatever the repair, the await may not be the only
    thing that can conclude the check.
    """
    src = V2.read_text(encoding="utf-8")
    body = src[src.index("const onMicTest = async () => {"):]
    body = body[:body.index("\n      const onSpkTest")]
    assert "navigator.mediaDevices.getUserMedia(" not in body, (
        "the audio check still awaits a bare getUserMedia, so a prompt nobody "
        "answers leaves it on 'Listening…' with no timer armed")


# =========================================================================== #
# 2. The participant page: what a dropped connection actually costs
#
# Measured: a refresh or a drop mid-encounter opens a NEW session, the actor
# restarts at its first line, and the transcript the participant had built is
# gone from the screen. The card said the opposite.
#
# The DATA half of the same finding does not reproduce and is not tested here:
# server/app.py's _rateable_split already withholds any session no run recorded
# as completed, which is exactly what a reconnected fragment is. See the round
# report.
# =========================================================================== #

def test_the_drop_card_does_not_promise_that_nothing_is_lost():
    """The sentence is false and it is the one a participant reads."""
    src = V2.read_text(encoding="utf-8")
    assert "Nothing is lost on your side" not in src, (
        "the drop card still tells the participant nothing is lost, while "
        "Reconnect starts the encounter again from its first line")


def test_the_drop_card_says_the_conversation_starts_again():
    """Saying less is not enough — the participant has to know what Reconnect
    will do before they press it, or they will read the actor's opening line as
    a bug."""
    src = V2.read_text(encoding="utf-8")
    card = src[src.index("$('dropReconnect').textContent = 'Reconnect';") - 900:
               src.index("$('dropReconnect').textContent = 'Reconnect';")]
    assert re.search(r"start (again|over)|begin again|from the beginning", card, re.I), (
        "the reconnect card does not say that this part starts again")


# =========================================================================== #
# 3. The participant page: a withdrawal that argues with the server
#
# Measured on a real withdrawal: five red console errors after the withdrawal
# card was already on screen — GET video-upload-url 403, retried, then POST
# video-uploaded 403 three times. The server is right to refuse (it refuses
# every write for a participant who has withdrawn); the page should not be
# asking.
# =========================================================================== #

WITHDRAWN_UPLOAD_HARNESS = r"""/* The upload chain against a server that has stopped accepting this
   participant's data, which is what a withdrawal makes it.

   Every upload route answers 403. The assertion is on the number of requests
   the page makes: a refusal that will be identical one request later is not
   worth a retry, and a confirm that is refused by the same rule is not worth
   sending at all. */
'use strict';
const assert = require('assert');
const { bootV2 } = require('./stub.js');

const PAGE = process.argv[2];
const PRESIGN = '/video-upload-url', CONFIRM = '/video-uploaded';

(async () => {
  const b = bootV2(PAGE, '?run=r_1');
  b.net.route([
    { match: PRESIGN, fn: () => b.net.res(403, { detail: 'participant withdrew from the study' }) },
    { match: CONFIRM, fn: () => b.net.res(403, { detail: 'participant withdrew from the study' }) },
    { match: '/api/run/', fn: () => b.net.res(503, {}) },
  ]);

  const blob = new b.sandbox.Blob([{ size: 4096 }], { type: 'video/webm' });
  const p = b.ctx.uploadRecording('s_withdrawn', blob, 'video/webm');
  await b.clock.advance(600000);
  await p;

  const n = b.net.countOf(PRESIGN) + b.net.countOf(CONFIRM);
  assert(n <= 1,
    'the page made ' + n + ' requests against a server that had already refused '
    + 'this participant once (' + b.net.countOf(PRESIGN) + ' presign, '
    + b.net.countOf(CONFIRM) + ' confirm); a withdrawn participant\'s recording '
    + 'is never going to be accepted');

  // Quieter, not silent. This upload was not driven through the withdrawal
  // control, so as far as the page knows a refusal here is a fault nobody asked
  // for — and it keeps the notice it has always had. Only the four extra
  // requests are gone.
  const said = b.dom.$('transcript').children.map(c => c.textContent || '').join(' ');
  assert(/could not be saved/i.test(said),
    'a refusal the participant did not ask for is now silent as well as quiet: '
    + JSON.stringify(said));
  console.log('WITHDRAWN UPLOAD OK');
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


def test_a_refused_upload_is_not_argued_with(tmp_path):
    """403 on the presign is terminal: the confirm is refused by the same rule."""
    assert "WITHDRAWN UPLOAD OK" in _run(tmp_path, WITHDRAWN_UPLOAD_HARNESS, V2)


def test_a_withdrawal_marks_itself_before_it_tears_down_capture():
    """Order, not presence.

    Stopping the recorder is what starts the upload, so a flag raised after
    endSession would be raised too late for the upload to read it — and a
    withdrawing participant would be told their video could not be saved on top
    of the withdrawal card they just asked for.
    """
    src = V2.read_text(encoding="utf-8")
    handler = src[src.index("$('leaveBtn').addEventListener("):]
    handler = handler[:handler.index("showClosing(")]
    assert "withdrawing = true" in handler, (
        "the withdrawal control never marks itself, so the upload path cannot "
        "tell a refusal the participant asked for from one they did not")
    assert handler.index("withdrawing = true") < handler.index("endSession("), (
        "the withdrawal flag is set after capture is torn down, which is after "
        "the upload it exists to be read by has already started")


# =========================================================================== #
# 4. The participant page: a refusal that can never clear
#
# An entry link carrying ?pid= and no ?qid= is refused with
# no_survey_response_id, permanently — _why_consent_was_refused cannot reach any
# other answer for that link. The card told the participant to "press Try again
# in a few minutes", which is advice that cannot work, on the one screen a
# stopped participant reads carefully.
# =========================================================================== #

PERMANENT_REFUSAL_HARNESS = r"""/* The card a participant sees on a link that carried no survey reference.

   Driven through the page's own consent gate, opened the way the boot
   sequence, Start and the situation card all open it. POST /api/consent
   answers what server/app.py actually answers for that link: 409, with
   `reason: "no_survey_response_id"` beside the prose.

   Two cards are read, and the second is the control. The refusal an operator
   CAN clear — an unset UPSTREAM_CONSENT_VERSION, which is fixed by setting a
   variable and restarting — must keep its retry advice. The refusal that is a
   property of the link itself must not have it. */
'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
const CFG = { version: 'v1.0', title: 'Consent',
              body: 'We record your microphone audio and webcam video.',
              contact: { pi_name: 'Dr Rivera', email: 'pi@example.invalid' } };

async function card(status, reason) {
  const b = bootV2(PAGE, '?run=r_1&participant_id=p_test&consent=1');
  b.net.route([
    { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
    { match: '/api/run/', fn: () => b.net.res(503, {}) },
    { match: '/api/consent', fn: (call) => (call.method === 'POST'
        ? b.net.res(status, { detail: 'refused', reason }) : b.net.res(200, CFG)) },
  ]);
  vm.runInContext("run = { run_id: 'r_1', participant_id: 'RF_1' };", b.ctx);
  b.ctx.ensureParticipant();
  await b.clock.advance(5000);
  assert.strictEqual(b.$('consentOverlay').style.display, 'flex',
    'no card was shown for a refused consent (' + reason + ')');
  return String(b.$('consentBody').innerHTML);
}

(async () => {
  // The link is missing the survey reference. Nothing about waiting can help:
  // _why_consent_was_refused reaches this answer for that link every time.
  const permanent = await card(409, 'no_survey_response_id');
  assert(!/Try again in a few\s*minutes/i.test(permanent),
    'a permanently refused link is still told to press Try again in a few '
    + 'minutes: ' + permanent);
  assert(/link/i.test(permanent),
    'the card never mentions the link, which is the thing that is wrong: ' + permanent);
  assert(/contact|team|@/i.test(permanent),
    'the card offers no way to reach anybody: ' + permanent);

  // The control. An unset consent version really is fixed in a few minutes by
  // somebody who is not the participant, and that card should still say so.
  const transient = await card(503, 'consent_version_unset');
  assert(/Try again/i.test(transient),
    'the transient refusal lost its retry advice too: ' + transient);

  console.log('PERMANENT REFUSAL OK');
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


def _run_consent(harness_src: str, tmp_path: Path) -> str:
    (tmp_path / "stub.js").write_text(NODE_STUB, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(harness_src, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(V2)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


# =========================================================================== #
# 5. The rating console: a submit gate that re-arms forever
#
# static/rater.html showed a soft warning once and cleared it on a second
# click — except the "have you been quick" branch interpolates the live clock
# into the warning, and the once-only test compared the RENDERED STRING. The
# clock moves between clicks, so the string differs, so the gate re-arms. A
# rater who takes under two minutes, or who never presses play, cannot submit
# at all: measured at 1:26, 1:42, 1:42 on the demo wave, three clicks, three
# warnings. It submitted only when two clicks landed inside the same wall-clock
# second.
#
# `!videoPlayed` sits in the same branch as the elapsed-time text, so a rater
# who never plays the recording is locked out however long they spend.
# =========================================================================== #

GATE_HARNESS = r"""/* Drives static/rater.html's submit gate with a clock that moves between
   clicks, which is the only condition the defect needs.

   The answers are primed through the page's own draft restore rather than
   through 22 synthesised radio events: loadDraft() is the one path that fills
   `answers` without a real DOM, and `answers` is a lexical binding the vm
   cannot reach from outside. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');

const html = fs.readFileSync(process.argv[2], 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
assert(m, 'no script block in the console');

const ITEMS = [];
for (let i = 1; i <= 22; i++) {
  ITEMS.push({ id: 'esci_' + String(i).padStart(2, '0'), number: i,
               text: 'Statement ' + i, construct: 'listening', reverse: false });
}
// Not straight-lined: the other soft gate is a constant string and clears
// normally, and this test is about the one that does not.
const ANSWERS = {};
ITEMS.forEach((it, i) => { ANSWERS[it.id] = (i % 5) + 1; });

const PACKET = {
  assignment_id: 'as_gate', status: 'pending', rating_code: 'RC-GATE',
  construct: 'listening',
  situation: { text: 'Ten minutes.', people: [{ name: 'Devi', role: 'colleague' }] },
  transcript: [{ role: 'agent', speaker: 'Devi', t: 1, text: 'Morning.' }],
  duration_s: 600, duration_display: '10:00',
  counts: { participant_turns: 6, agent_turns: 7 },
  // `absent` is the one media state that neither blocks the rating nor needs a
  // <video>: it also sets videoPlayed, so this test is about the pace half of
  // the branch on its own.
  media: { video_url: null, video_available: false, note: 'No webcam recording.' },
  items: ITEMS,
  scale: { min: 1, max: 5, labels: {}, na_label: 'N/A' },
  scale_note: 'N/A where you cannot judge.',
  instrument_notice: 'Licensed instrument.',
};

let clock = 0;
const written = {};
const store = {};
function el(id) {
  const e = {
    id, style: {}, dataset: {}, value: '', disabled: false, className: '', checked: false,
    set innerHTML(v) { written[id] = v; }, get innerHTML() { return written[id] || ''; },
    set textContent(v) { written[id + ':text'] = v; }, get textContent() { return written[id + ':text'] || ''; },
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, removeEventListener() {},
    focus() {}, scrollIntoView() {}, appendChild() {},
    closest() { return null; }, setAttribute() {}, getAttribute: () => null,
    querySelectorAll() { return []; }, querySelector() { return el(id + ':q'); },
  };
  return e;
}
const els = {};
const posted = [];
const ctx = {
  console, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp, Set, Map,
  parseInt, parseFloat, isNaN, isFinite, URLSearchParams, encodeURIComponent, Promise, Error,
  document: {
    getElementById: (id) => (els[id] = els[id] || el(id)),
    addEventListener() {}, querySelector: (s) => el(s), querySelectorAll() { return []; },
    hidden: false, activeElement: null,
  },
  window: { addEventListener() {}, scrollTo() {} },
  location: { search: '?token=rt_' + 'a'.repeat(32), pathname: '/rate' },
  performance: { now: () => clock },
  setInterval() {}, clearInterval() {}, setTimeout: (f) => { f(); return 1; }, clearTimeout() {},
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem(k, v) { store[k] = String(v); }, removeItem(k) { delete store[k]; },
  },
  alert() {}, confirm: () => true,
  CSS: { escape: (s) => s },
  Event: class { constructor(t) { this.type = t; } },
  fetch: async (url, opts) => {
    const u = String(url).split('?')[0];
    if (u.endsWith('/api/rater/me')) return { ok: true, status: 200, json: async () => ({ rater_id: 'rr_1', name: 'A', kind: 'trained', assignments_pending: 1 }) };
    if (u.endsWith('/api/rater/assignments')) return { ok: true, status: 200, json: async () => [{ assignment_id: 'as_gate', rating_code: 'RC-GATE', status: 'pending', assigned_at: '2026-04-01T14:05:00Z' }] };
    if (u.includes('/api/rater/packet/')) return { ok: true, status: 200, json: async () => PACKET };
    if (u.includes('/api/rater/ratings/')) { posted.push(JSON.parse((opts || {}).body || '{}')); return { ok: true, status: 200, json: async () => ({ ok: true }) }; }
    return { ok: false, status: 404, json: async () => null };
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(m[1], ctx, { filename: 'rater.html' });

(async () => {
  await new Promise(r => setImmediate(r));
  // The draft the console will restore. Written before the packet is opened,
  // under the key the console builds from the assignment id.
  store['rf.rating.draft.as_gate'] = JSON.stringify({ answers: ANSWERS, better: '', notable: '', at: 1 });
  await ctx.openAssignment('as_gate');

  // Nine minutes on the encounter would clear the pace gate outright, so the
  // rater here is deliberately quick — which is the case the gate is FOR, and
  // the case that could not get past it.
  clock = 1000; ctx.tick();      // ~1s banked
  await ctx.onSubmit();
  assert.strictEqual(posted.length, 0, 'the first click submitted without warning');
  const first = written['submitMsg:text'] || '';
  assert(first.length > 0, 'the first click said nothing');

  // The clock moves, exactly as it does between two human clicks.
  clock = 21000; ctx.tick();
  await ctx.onSubmit();
  assert.strictEqual(posted.length, 1,
    'the second click did not submit: the gate re-armed because the clock moved '
    + '(warned again with ' + JSON.stringify(written['submitMsg:text'] || '') + ')');

  console.log('GATE OK');
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


# =========================================================================== #
# 6. The rating console: a recording shorter than the encounter
#
# timelineTotal() prefers packet.duration_s over v.duration, and it is right to
# — raw MediaRecorder WebM has no duration header, so v.duration reads Infinity
# in Chrome and 0 in Safari. What was missing is the case where v.duration IS
# finite and much smaller: measured 0:05 rendered against a 9:58 encounter,
# with transcript clicks past 0:06 silently clamped to the end. A truncated
# upload renders identically to a complete one, and the rater submits believing
# they watched the encounter.
# =========================================================================== #

# =========================================================================== #
# 6b. The transcript the rater reads
#
# The captions occasionally glue a re-sent fragment straight onto the sentence
# before it with no space. Observed in the cleanest measured run (quiet
# microphone, no barge-in):
#
#   "The cleanest thing is I walk the client through it.I got the deck done
#    last night"
#
# and the same shape is in the stored record, so it is the transcript the
# server wrote and not a rendering artefact. _clean_agent_text already collapses
# a fragment that REPEATS an earlier one; this one is a paraphrase, so it is
# left alone — correctly — and only the missing space is wrong.
# =========================================================================== #

SEAMS = [
    # The measured case.
    ("The cleanest thing is I walk the client through it.I got the deck done last night",
     "The cleanest thing is I walk the client through it. I got the deck done last night"),
    ("That is what I heard.Can we come back to it?",
     "That is what I heard. Can we come back to it?"),
    ("Is that fair?Because I would rather say it now.",
     "Is that fair? Because I would rather say it now."),
]

# What must NOT be touched. Every one of these is a full stop with no space
# after it that is not a sentence boundary, and a naive rule breaks all of them.
UNTOUCHED = [
    "We are 3.5 points behind and that is before the renewal lands.",
    "The U.S.A team asked for it and I said we would look at the numbers.",
    "Go to example.com/Reports and the whole thing is laid out there.",
    "It was 1,200.00 dollars and nobody had approved the spend at all.",
    "I said no.then he asked again and I still said no, which is the whole point.",
]


@pytest.mark.parametrize("raw,want", SEAMS)
def test_a_sentence_seam_with_no_space_is_repaired(raw, want):
    from server.realtime_voice_session import _clean_agent_text

    assert _clean_agent_text(raw) == want


@pytest.mark.parametrize("raw", UNTOUCHED)
def test_a_full_stop_that_is_not_a_sentence_boundary_is_left_alone(raw):
    from server.realtime_voice_session import _clean_agent_text

    assert _clean_agent_text(raw) == raw


def test_the_repeat_collapse_still_works_over_the_seam_repair():
    """The two passes must not fight: a doubled turn is still one turn."""
    from server.realtime_voice_session import _clean_agent_text

    assert _clean_agent_text("It's a slippery slope.It's a slippery slope.") \
        == "It's a slippery slope."


# =========================================================================== #
# 7. The evidence trace
# =========================================================================== #

def test_the_session_list_carries_a_date():
    """An eight-day wave rendered as 27 bare clock times.

    Measured on the demo wave: '09:25 PM / 09:37 PM / 09:35 AM ...' for
    encounters spanning 2026-03-02 to 2026-03-10, with no date on the row and
    no title attribute carrying one. static/rater.html formats the same
    timestamp with a month and a day already.
    """
    src = EVIDENCE.read_text(encoding="utf-8")
    render = src[src.index("function renderList()"):src.index("async function select(")]
    assert "toLocaleDateString" in render, (
        "the evidence trace's session list still shows a time with no date")


def _contrast(fg: str, bg: str) -> float:
    def lum(h):
        h = h.lstrip("#")
        ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _token(name: str) -> str:
    src = EVIDENCE.read_text(encoding="utf-8")
    hit = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", src)
    assert hit, f"--{name} is not defined in static/evidence.html"
    return hit.group(1)


def test_the_partial_badge_meets_aa_on_its_own_ground():
    """The last token on the page that did not.

    Found by re-running the sweep's own computed-style contrast pass over the
    rendered page after the grey work: 43 failures before, 1 after — the
    "Partial" badge, --warn #946a12 on --warn-bg, 4.38:1 at 10px. Now 0.
    """
    assert _contrast(_token("warn"), _token("warn-bg")) >= 4.5
    assert _contrast(_token("warn"), _token("surface-2")) >= 4.5


@pytest.mark.parametrize("token", ["faint", "muted"])
def test_the_evidence_trace_greys_meet_aa(token):
    """--faint was #9a9a96: 2.82:1 on white and 2.61:1 on --surface-2, at 10px
    to 12px, against the 4.5:1 WCAG AA requires at those sizes. 43 elements
    carried it — every session-list clock, every latency pill, the "recording
    present" note, the instrument labels and the "pending instrumentation"
    cells.

    Both backgrounds it is actually drawn on are checked, and --muted alongside
    it: raising --faint to --muted's exact value would pass this test and lose
    the page's typographic hierarchy, so the two are held apart by being
    required to pass separately.
    """
    fg = _token(token)
    for bg_name in ("surface", "surface-2", "ground"):
        bg = _token(bg_name)
        ratio = _contrast(fg, bg)
        assert ratio >= 4.5, (
            f"--{token} {fg} on --{bg_name} {bg} is {ratio:.2f}:1, "
            f"below the 4.5:1 AA requires at these sizes")


def test_faint_and_muted_are_still_two_different_greys():
    """The hierarchy is the reason there are two tokens."""
    assert _token("faint") != _token("muted")


# =========================================================================== #
# 8. The server
# =========================================================================== #

@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """A throwaway DATA_DIR for the session-scanning routes."""
    from server import runs, storage

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(storage, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return tmp_path


def test_encounters_come_back_newest_first_by_start_time(data_dir):
    """The route's own docstring promises "newest first"; it sorted on the
    directory's mtime.

    MEASURED on the demo wave: directory mtimes span 0.4 s while the manifests'
    `started_at` spans 7.9 days, so the sort key carried essentially no time
    signal at all — the order was filesystem tie-breaking noise. Any touch of a
    session directory after the encounter decouples the two: the post-close
    `video_uploaded` append does it, and so does a retranscribe, an rsync, a
    restore, or simply copying DATA_DIR. This is what the evidence trace's
    session list is built from.
    """
    from server import app as appmod
    from server.storage import SESSIONS_DIR

    # Written newest-first, so the directory mtimes run the opposite way to the
    # start times — which is every wave whose sessions were touched after the
    # encounter, and which is what the demo wave measured as.
    for name, started in (("s_newest", 3000.0), ("s_middle", 2000.0), ("s_oldest", 1000.0)):
        d = SESSIONS_DIR / name
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({
            "scenario": "S1A", "started_at": started, "participant_id": "p_1",
            "status": "closed", "cohort": "study", "run_id": "r_1",
            "encounter_index": 1}), encoding="utf-8")
        time.sleep(0.05)

    out = asyncio.run(appmod.api_encounters())
    ids = [e["id"] for e in out]
    assert ids == ["s_newest", "s_middle", "s_oldest"], ids


def test_there_is_a_demo_route():
    """/static/demo.html is the page to put in front of a colleague — it reads
    /health and says what will and will not work before you present — and it
    was the one console with no route of its own. /director, /evidence,
    /researcher and / all have one.
    """
    from server import app as appmod

    paths = {r.path for r in appmod.app.routes if hasattr(r, "path")}
    assert "/demo" in paths, "there is still no /demo route"


@pytest.fixture()
def web(monkeypatch):
    """A TestClient that the host allowlist will answer.

    ALLOWED_HOSTS is read once at import and defaults to the two loopback
    names, so TestClient's own "testserver" Host header is refused with 400
    before any route is reached.
    """
    from starlette.testclient import TestClient

    from server import app as appmod

    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


def test_the_demo_route_serves_the_demo_page(web):
    r = web.get("/demo")
    assert r.status_code == 200
    assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()


@pytest.mark.parametrize("path", ["/favicon.ico", "/apple-touch-icon.png"])
def test_the_browsers_default_icon_requests_are_answered(web, path):
    """The entry-check page is self-contained HTML served by the app, and it
    carried no icon link — so every arrival at /start/... logged a 404 in the
    console before the participant had done anything. Cosmetic, and visible to
    exactly the person testing the install.
    """
    r = web.get(path)
    assert r.status_code == 200, f"GET {path} is still a 404"


def test_the_entry_check_page_declares_its_icon():
    src = APP.read_text(encoding="utf-8")
    page = src[src.index("_ENTRY_CHECK_PAGE = "):]
    page = page[:page.index('"""\n', page.index("<body>"))]
    assert 'rel="icon"' in page, (
        "the entry-check page still asks the browser to guess at /favicon.ico")

