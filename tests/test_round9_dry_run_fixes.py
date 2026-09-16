"""What a stranger hit when they followed the package with no prior knowledge.

Round 8 packaged the platform so a researcher could test it unaided. Somebody
who knew nothing then walked the package end to end on this machine and wrote
down every place it misled them. This file is the defects from that walk, and
every test here failed on the tree it was written against.

Two of them were blockers, and they are different in kind:

*   The rating console's incomplete-recording notice fired on NOTHING. The
    walkthrough says, twice, that you will see it on every encounter in the
    fixture and that seeing it is the new safeguard working. It was reachable
    only from the probe that runs when the engine reports NO duration — so it
    was skipped on exactly the well-formed files that can answer the question,
    which is every file the study actually produces. This is the worst shape a
    defect can take here: the package said something untrue about itself, in
    the one place it asked the reader to trust a safeguard.

*   A machine with no microphone was told its microphone was BLOCKED, and sent
    to look for a permission prompt that had never appeared. The quiet "Skip
    the check" button then carried it into the room, where the same missing
    device stopped it again.

The client-side findings are DRIVEN, not read: static/rater.html and
static/v2.html are run in a Node vm and the assertion is made by firing the
events a browser fires and reading what the page did. The rating-console
harness in particular fires `loadedmetadata` rather than calling the probe by
hand — calling the probe is what let the round-8 test pass while the page
itself never reached the check.

Run from the repo root:

    python -m pytest tests/test_round9_dry_run_fixes.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_browser_compat import STUB_JS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "static" / "v2.html"
RATER = ROOT / "static" / "rater.html"


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the client harnesses need it")
    return node


def _run_js(harness_src: str, tmp_path: Path, page: Path, with_stub: bool = False) -> str:
    if with_stub:
        (tmp_path / "stub.js").write_text(STUB_JS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(harness_src, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(page)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


# =========================================================================== #
# 1. BLOCKER. The incomplete-recording notice fired on no encounter at all.
#
# MEASURED by the stranger on two encounters (RC-18DC6C1A8A, RC-ADADD2A244),
# after a clean server restart, with ten seconds of settle time: video duration
# 5.95s against packet.duration_s of 597.6s and 541.7s, `shortRecording ===
# false`, #playNote present in the DOM at display:none.
#
# Cause: the `loadedmetadata` handler settled a finite duration and returned.
# noteShortRecording was called only from resolveDuration's finish(), and
# resolveDuration runs only on the Infinity-duration branch. A well-formed file
# — every demo stub, and every truncated upload that closed its container —
# reports a finite duration, took the other branch, and said nothing.
#
# The round-8 test called resolveDuration() directly, which is why it was green
# against a page that never reached the check. This one fires the event.
# =========================================================================== #

SHORT_ON_METADATA = r"""/* The rating console meets the file the study actually produces. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');

const html = fs.readFileSync(process.argv[2], 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
assert(m, 'no script block in the console');

const ITEMS = [];
for (let i = 1; i <= 22; i++) {
  ITEMS.push({ id: 'esci_' + String(i).padStart(2, '0'), number: i,
               text: 'Statement ' + i, construct: 'listening', reverse: false });
}
const PACKET = {
  assignment_id: 'as_short', status: 'pending', rating_code: 'RC-SHORT',
  construct: 'listening',
  situation: { text: 'Ten minutes.', people: [{ name: 'Devi', role: 'colleague' }] },
  transcript: [{ role: 'agent', speaker: 'Devi', t: 1, text: 'Morning.' }],
  duration_s: 597.6, duration_display: '9:58',
  counts: { participant_turns: 6, agent_turns: 7 },
  media: { video_url: '/api/rater/video/as_short', video_available: true, video_status: 'ok' },
  items: ITEMS,
  scale: { min: 1, max: 5, labels: {}, na_label: 'N/A' },
  scale_note: 'N/A where you cannot judge.',
  instrument_notice: 'Licensed instrument.',
};

let clock = 0;
const written = {};
const store = {};
function el(id) {
  const handlers = {};
  const e = {
    id, style: {}, dataset: {}, value: '', disabled: false, className: '', checked: false,
    // The properties a browser reports for a perfectly well-formed 6-second file.
    duration: NaN, currentTime: 0, readyState: 1, networkState: 2, error: null,
    set innerHTML(v) { written[id] = v; }, get innerHTML() { return written[id] || ''; },
    set textContent(v) { written[id + ':text'] = v; }, get textContent() { return written[id + ':text'] || ''; },
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
    removeEventListener(t, fn) { if (handlers[t]) handlers[t] = handlers[t].filter(f => f !== fn); },
    fire(t) { (handlers[t] || []).slice().forEach(fn => fn({ type: t })); },
    listeners(t) { return (handlers[t] || []).length; },
    focus() {}, scrollIntoView() {}, appendChild() {},
    closest() { return null; }, setAttribute() {}, getAttribute: () => null,
    querySelectorAll() { return []; }, querySelector() { return el(id + ':q'); },
    play() { return Promise.resolve(); }, pause() {}, load() {},
  };
  return e;
}
const els = {};
const ctx = {
  console, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp, Set, Map,
  parseInt, parseFloat, isNaN, isFinite, URLSearchParams, encodeURIComponent, Promise, Error,
  document: {
    getElementById: (id) => (els[id] = els[id] || el(id)),
    createElement: (tag) => el(tag),
    addEventListener() {}, querySelector: (s) => el(s), querySelectorAll() { return []; },
    hidden: false, activeElement: null,
  },
  window: { addEventListener() {}, scrollTo() {} },
  location: { search: '?token=rt_' + 'a'.repeat(32), pathname: '/rate' },
  performance: { now: () => clock },
  setInterval() {}, clearInterval() {}, setTimeout: () => 1, clearTimeout() {},
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem(k, v) { store[k] = String(v); }, removeItem(k) { delete store[k]; },
  },
  alert() {}, confirm: () => true,
  CSS: { escape: (s) => s },
  Event: class { constructor(t) { this.type = t; } },
  fetch: async (url) => {
    const u = String(url).split('?')[0];
    if (u.endsWith('/api/rater/me')) return { ok: true, status: 200, json: async () => ({ rater_id: 'rr_1', name: 'A', kind: 'trained', assignments_pending: 1 }) };
    if (u.endsWith('/api/rater/assignments')) return { ok: true, status: 200, json: async () => [{ assignment_id: 'as_short', rating_code: 'RC-SHORT', status: 'pending', assigned_at: '2026-04-01T14:05:00Z' }] };
    if (u.includes('/api/rater/packet/')) return { ok: true, status: 200, json: async () => PACKET };
    return { ok: false, status: 404, json: async () => null };
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(m[1], ctx, { filename: 'rater.html' });

(async () => {
  await new Promise(r => setImmediate(r));
  await ctx.openAssignment('as_short');
  const v = els.vid;
  assert(v, 'no <video> was built for a playable packet');
  assert(v.listeners('loadedmetadata') > 0,
    'the page attached no loadedmetadata listener, so this harness is not driving '
    + 'the path a browser drives, and proves nothing');

  // The demo wave, and a truncated upload: a well-formed file, 5.95s long, of
  // a 9:58 encounter. Finite duration, so the probe never runs.
  v.duration = 5.95;
  v.fire('loadedmetadata');

  const note = (written['playNote'] || '') + ' ' + (written['playNote:text'] || '');
  const shown = els.playNote && els.playNote.style && els.playNote.style.display;
  assert(/incomplete|truncat|shorter|stops before/i.test(note),
    'a 5.95s recording of a 9:58 encounter drew no notice when its metadata '
    + 'loaded: the check is reached only when the duration is NOT finite, which '
    + 'is never true of the files the study produces. note=' + JSON.stringify(note));
  assert(shown !== 'none',
    'the notice was written but left hidden (display=' + JSON.stringify(shown) + ')');

  // The control. A notice that fires on a complete recording means nothing.
  written['playNote'] = ''; written['playNote:text'] = '';
  PACKET.duration_s = 6.0;
  ctx.noteShortRecording(els.vid);
  const quiet = (written['playNote'] || '') + ' ' + (written['playNote:text'] || '');
  assert(!/incomplete|truncat|stops before/i.test(quiet),
    'a complete recording was called incomplete: ' + JSON.stringify(quiet));

  console.log('FINITE SHORT OK');
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


def test_a_short_recording_is_named_when_its_metadata_loads(tmp_path):
    """The path a browser takes, not the path the round-8 test called by hand."""
    assert "FINITE SHORT OK" in _run_js(SHORT_ON_METADATA, tmp_path, RATER)


def test_the_finite_duration_branch_asks_the_question_too():
    """Structural half: whatever the handler becomes, both branches must ask.

    A notice reachable from only one branch of `isFinite(duration)` is a notice
    that cannot fire on a well-formed file, which is every file that matters.
    """
    src = RATER.read_text(encoding="utf-8")
    start = src.index("v.addEventListener('loadedmetadata'")
    handler = src[start:src.index("});", start)]
    finite = handler[handler.index("if (isFinite(v.duration)"):handler.index("else if")]
    assert "noteShortRecording" in finite, (
        "the finite-duration branch still settles the seek and returns without "
        "comparing the file's length against the encounter's")


# =========================================================================== #
# 2. BLOCKER (the half a page can fix). A machine with no microphone.
#
# MEASURED: getUserMedia rejects with NotFoundError — nothing blocked, nothing
# pending, no device. The audio check reported "Mic blocked" and help text
# about the mic icon in the address bar. There is no icon: nothing ever asked.
# The undocumented "Skip the check" button then let the tester past the card
# and into the room, where startCapture met the same missing device and stopped
# them again, with nothing having warned that it would.
#
# captureKindFor already told these apart for the room. The check did not ask
# it. There is no repair for the requirement itself — the encounters are spoken
# out loud, and a completion code is minted only against a recorded session in
# which the participant actually spoke — so the fix is to say the true thing
# early, including that Skip will not get past it.
# =========================================================================== #

NO_MICROPHONE = r"""/* A machine with no capture device: the dry run's machine, any VM, any laptop
   with the microphone disabled in Windows privacy settings. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];
const noSuchDevice = () => Object.assign(new Error('Requested device not found'),
                                         { name: 'NotFoundError' });

const boot = () => bootPage(PAGE, {}, {
  location: { search: '?run=r_1', href: 'http://t/v2?run=r_1', reload() {}, replace() {} },
});

(async () => {
  const b = boot();
  b.net.route([{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);
  b.sandbox.navigator.mediaDevices = { getUserMedia: () => Promise.reject(noSuchDevice()) };

  // The help the markup ships is about allowing a permission. Whatever is said
  // to a machine with no device must not be that.
  const permissionHelp = 'Check that your browser allowed the microphone';
  b.dom.$('micHelp').textContent = "We can't hear anything yet. " + permissionHelp + ' ...';

  b.ctx.runAudioCheck();
  await b.clock.advance(10);
  b.dom.$('micTestBtn').click();
  await b.clock.advance(20000);

  const status = String(b.dom.$('micStatus').textContent || '');
  const help = String(b.dom.$('micHelp').textContent || '');

  assert(!/blocked/i.test(status),
    'a machine with no microphone was told the microphone was BLOCKED, which is '
    + 'a permission nobody refused and nobody can grant: ' + JSON.stringify(status));
  assert(/no microphone|not find|no working microphone|does not appear to have/i.test(status + ' ' + help),
    'nothing on screen says the machine has no microphone: micStatus='
    + JSON.stringify(status) + ' micHelp=' + JSON.stringify(help));
  assert(!help.includes(permissionHelp),
    'the help still sends a tester with no device after a permission prompt that '
    + 'never appeared: ' + JSON.stringify(help));
  assert(/skip/i.test(help),
    'nothing says that skipping the check will not get past the requirement, so '
    + 'the quiet Skip button is still an undocumented dead end: ' + JSON.stringify(help));

  // The controls. A refusal is a different thing that a participant can act on,
  // and it must keep saying its own different thing.
  const c = boot();
  c.net.route([{ match: '/api/run/', fn: () => c.net.res(503, {}) }]);
  c.sandbox.navigator.mediaDevices = {
    getUserMedia: () => Promise.reject(Object.assign(new Error('x'), { name: 'NotAllowedError' })),
  };
  c.ctx.runAudioCheck();
  await c.clock.advance(10);
  c.dom.$('micTestBtn').click();
  await c.clock.advance(20000);
  const denied = String(c.dom.$('micStatus').textContent || '');
  assert(/blocked/i.test(denied),
    'a genuine denial no longer says the microphone is blocked: ' + JSON.stringify(denied));
  assert(denied !== status,
    'a denial and a missing device still say the same thing: ' + JSON.stringify(status));

  console.log('NO MIC OK');
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


def test_a_machine_with_no_microphone_is_told_so(tmp_path):
    assert "NO MIC OK" in _run_js(NO_MICROPHONE, tmp_path, V2, with_stub=True)


def test_the_audio_check_uses_the_same_capture_kinds_as_the_room():
    """One microphone, one story.

    The check and the room used to disagree: the room distinguished denied /
    missing / unsupported / insecure / unanswered and said the right sentence
    for each, while the check collapsed all five into "Mic blocked" unless the
    kind happened to be 'unanswered'.
    """
    src = V2.read_text(encoding="utf-8")
    body = src[src.index("const onMicTest = async () => {"):]
    body = body[:body.index("\n      const onSpkTest")]
    assert "captureKindFor(" in body, (
        "the audio check still decides for itself what a rejection means, rather "
        "than asking the function the room asks")


# =========================================================================== #
# 3. The submit gate's warning and the console's dwell timer disagreed.
#
# MEASURED: the warning read "You have spent 0:00 on this encounter" while
# #timeText, on the same screen, read 0:16. Both are drawn from activeMs — but
# the warning is a snapshot taken at the click and the timer keeps running, so
# they drift apart from the moment it is drawn. Two clocks contradicting each
# other about one quantity, and the one the rater is asked to act on is the one
# that looks wrong.
# =========================================================================== #

PACE_CLOCK = r"""/* One quantity, two places on the screen. They must agree. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');

const html = fs.readFileSync(process.argv[2], 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
assert(m, 'no script block in the console');

const ITEMS = [];
for (let i = 1; i <= 22; i++) {
  ITEMS.push({ id: 'esci_' + String(i).padStart(2, '0'), number: i,
               text: 'Statement ' + i, construct: 'listening', reverse: false });
}
const ANSWERS = {};
ITEMS.forEach((it, i) => { ANSWERS[it.id] = (i % 5) + 1; });   // not straight-lined

const PACKET = {
  assignment_id: 'as_pace', status: 'pending', rating_code: 'RC-PACE',
  construct: 'listening',
  situation: { text: 'Ten minutes.', people: [{ name: 'Devi', role: 'colleague' }] },
  transcript: [{ role: 'agent', speaker: 'Devi', t: 1, text: 'Morning.' }],
  duration_s: 600, duration_display: '10:00',
  counts: { participant_turns: 6, agent_turns: 7 },
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
  return {
    id, style: {}, dataset: {}, value: '', disabled: false, className: '', checked: false,
    set innerHTML(v) { written[id] = v; }, get innerHTML() { return written[id] || ''; },
    set textContent(v) { written[id + ':text'] = v; }, get textContent() { return written[id + ':text'] || ''; },
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, removeEventListener() {},
    focus() {}, scrollIntoView() {}, appendChild() {},
    closest() { return null; }, setAttribute() {}, getAttribute: () => null,
    querySelectorAll() { return []; }, querySelector() { return el(id + ':q'); },
  };
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
    if (u.endsWith('/api/rater/assignments')) return { ok: true, status: 200, json: async () => [{ assignment_id: 'as_pace', rating_code: 'RC-PACE', status: 'pending', assigned_at: '2026-04-01T14:05:00Z' }] };
    if (u.includes('/api/rater/packet/')) return { ok: true, status: 200, json: async () => PACKET };
    if (u.includes('/api/rater/ratings/')) { posted.push(JSON.parse((opts || {}).body || '{}')); return { ok: true, status: 200, json: async () => ({ ok: true }) }; }
    return { ok: false, status: 404, json: async () => null };
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(m[1], ctx, { filename: 'rater.html' });

const mmssOf = (s) => { const m2 = /(\d+):(\d\d)/.exec(s || ''); return m2 ? (+m2[1] * 60 + +m2[2]) : null; };

(async () => {
  await new Promise(r => setImmediate(r));
  store['rf.rating.draft.as_pace'] = JSON.stringify({ answers: ANSWERS, better: '', notable: '', at: 1 });
  await ctx.openAssignment('as_pace');

  clock = 400; ctx.tick();
  await ctx.onSubmit();
  assert.strictEqual(posted.length, 0, 'the first click submitted without warning');
  assert(/You have spent/.test(written['submitMsg:text'] || ''),
    'the pace gate did not fire: ' + JSON.stringify(written['submitMsg:text']));

  // They read it. Sixteen seconds pass, exactly as in the dry run.
  for (let t = 1400; t <= 16400; t += 1000) { clock = t; ctx.tick(); }

  const timer = mmssOf(written['timeText:text']);
  const warn = mmssOf(written['submitMsg:text']);
  assert.strictEqual(timer, 16,
    'the harness did not bank 16s: ' + JSON.stringify(written['timeText:text']));
  assert.strictEqual(warn, timer,
    'the warning and the dwell timer disagree about the same quantity on the same '
    + 'screen: warning ' + JSON.stringify(written['submitMsg:text'])
    + ' vs timer ' + JSON.stringify(written['timeText:text']));

  // And the gate still clears on the second click: keeping the sentence current
  // must not re-arm what round 8 fixed.
  await ctx.onSubmit();
  assert.strictEqual(posted.length, 1,
    'the second click did not submit: keeping the warning current re-armed the gate');

  console.log('PACE CLOCK OK');
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


def test_the_pace_warning_and_the_dwell_timer_agree(tmp_path):
    assert "PACE CLOCK OK" in _run_js(PACE_CLOCK, tmp_path, RATER)


def test_the_warning_is_still_a_sentence_and_the_memory_is_still_a_key():
    """Round 8's fix must survive round 9's.

    The gate remembers `warnKey` and displays `warn`. Redrawing the sentence is
    only safe while those stay two different things.
    """
    src = RATER.read_text(encoding="utf-8")
    gate = src[src.index("  let warn = null;"):src.index("  inFlight = true;")]
    assert re.search(r"confirmPending\s*!==\s*warnKey", gate), (
        "the gate no longer compares a stable key")
    assert "paceWarningText()" in gate, (
        "the pace branch no longer draws its sentence from the shared function, so "
        "the redraw and the click can render different text")


# =========================================================================== #
# 4. The group arm told the participant there was one other person in the room.
#
# MEASURED in a three-character group scenario (Alex, Jordan, Casey): the WHAT
# TO DO block read "Talk out loud, as you would at work. The other person hears
# you and replies" — singular, in the arm whose whole point is that there is
# more than one other person, in front of three named character tiles.
#
# All six group scenarios said it. Keyed on the interaction MODE, not the count
# of characters: S1A and S1B have two characters and speak to them one after
# the other, which is the singular case and must stay singular.
# =========================================================================== #

def _briefing_for(scenario_id: str) -> dict:
    from server.scenarios_v3 import compile_scenario
    return compile_scenario(scenario_id, "k_round9").briefing


@pytest.mark.parametrize("scenario_id", ["S3A", "S3B", "S3C", "S4A", "S4B", "S4C"])
def test_the_group_arm_does_not_say_the_other_person(scenario_id):
    b = _briefing_for(scenario_id)
    assert any(p["mode"] == "group" for p in b["parts"]), (
        f"{scenario_id} is not a group scenario, so this test is aimed wrongly")
    howto = " ".join(b["howto"])
    assert "The other person hears you" not in howto, (
        f"{scenario_id} puts three people in the room and tells the participant "
        f"there is one: {howto!r}")
    assert "other people hear you" in howto, (
        f"{scenario_id} no longer says who hears the participant: {howto!r}")


@pytest.mark.parametrize("scenario_id", ["S1A", "S1B", "S1C", "S2A", "S2B", "S2C"])
def test_the_one_to_one_arm_still_says_the_other_person(scenario_id):
    """The control, and it is not redundant.

    S1A and S1B each have TWO characters, spoken to one after the other. A fix
    keyed on how many people the scenario contains rather than how many are in
    the room at once would make these plural and be wrong.
    """
    b = _briefing_for(scenario_id)
    assert not any(p["mode"] == "group" for p in b["parts"]), (
        f"{scenario_id} is a group scenario, so this test is aimed wrongly")
    howto = " ".join(b["howto"])
    assert "The other person hears you and replies" in howto, (
        f"{scenario_id} speaks to one person at a time and no longer says so: {howto!r}")
