"""The participant page from the link to the first Start press, driven.

A walkthrough of the intro on production (2026-09-25, /v2?scenario=S4A, the
issues #39 and #41-#45) and the researchers' decisions of 2026-09-28 (#37, the
build tag) are the cases here. Each one is asserted by running the page's own
script in a Node vm against a thin DOM and a routed fetch, and pressing its own
buttons: what the participant would see and what the server would be sent,
never what the source happens to say.

Set ``RF_V2_PAGE`` to run the same harnesses against another copy of the page;
that is how "red before, green after" was proven against the unmodified file.
Skipped where node is not installed, like the other page harnesses.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_browser_compat import STUB_JS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
V2 = Path(os.environ.get("RF_V2_PAGE") or (ROOT / "static" / "v2.html"))


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harnesses need it")
    return node


# Shared by every harness below: boot the page with its routes in place BEFORE
# the script runs (the boot sequence fetches on its first line), a location
# that history.replaceState really rewrites, and a sessionStorage that can be
# carried into a second boot — which is what a refresh is.
PRELUDE = r"""'use strict';
const assert = require('assert');
const fs = require('fs');
const { makeContext, vm } = require('./stub.js');
const PAGE = process.argv[2];
const SRC = fs.readFileSync(PAGE, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];

function memoryStorage() {
  const d = {};
  return { _d: d, getItem: (k) => (k in d ? d[k] : null),
           setItem: (k, v) => { d[k] = String(v); }, removeItem: (k) => { delete d[k]; } };
}

function boot(search, routes, opts) {
  opts = opts || {};
  const loc = { search, href: 'http://t/v2' + search, host: 't', protocol: 'http:',
                pathname: '/v2', reload() {}, replace() {} };
  const history = {
    state: null, calls: [],
    replaceState(state, title, url) {
      const u = String(url);
      this.calls.push(u);
      const q = u.indexOf('?');
      loc.search = q >= 0 ? u.slice(q) : '';
      loc.href = 'http://t/v2' + loc.search;
    },
  };
  const b = makeContext(opts.cfg || {}, Object.assign({
    location: loc, history, sessionStorage: opts.sessionStorage || memoryStorage(),
  }, opts.extra || {}));
  b.loc = loc;
  b.history = history;
  b.set = (code) => vm.runInContext(code, b.ctx);
  // The routed fetch records url and method; the bodies are what a POST says.
  const inner = b.sandbox.fetch;
  b.sent = [];
  b.sandbox.fetch = (url, o) => {
    b.sent.push({ url: String(url), method: (o && o.method) || 'GET', body: o && o.body });
    return inner(url, o);
  };
  b.net.route(routes(b));
  vm.runInContext(SRC, b.ctx, { filename: 'v2.html' });
  return b;
}

const shown = (b, id) => b.dom.$(id).style.display === 'flex';
const posts = (b, sub) => b.net.calls.filter(c => c.method === 'POST' && c.url.includes(sub));
const gets = (b, sub) => b.net.calls.filter(c => c.method === 'GET' && c.url.includes(sub));
const notes = (b) => b.dom.$('transcript').children.filter(c => /system-note/.test(c.className || ''));

const BRIEF = {
  id: 'S4A', title: 'Planning an internal rollout', intro: 'You are in a room.', mode: 'group',
  intro_image: '',
  briefing: {
    situation: 'A working session about the rollout.', assets: [],
    people: [{ name: 'Dan', role: 'Lead' }, { name: 'Priya', role: 'Ops' }, { name: 'Chris', role: 'Eng' }],
    parts: [{ label: 'Working session', mode: 'group', with: ['Dan', 'Priya', 'Chris'] },
            { label: 'Follow-up', mode: 'group', with: ['Dan', 'Priya'] }],
    duration: [7, 12], howto: ['Talk out loud.'],
  },
  cast: [{ id: 'dan', name: 'Dan', role: 'Lead' }, { id: 'priya', name: 'Priya', role: 'Ops' },
         { id: 'chris', name: 'Chris', role: 'Eng' }],
};
"""


def _run(tmp_path: Path, body: str, marker: str) -> str:
    (tmp_path / "stub.js").write_text(STUB_JS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(
        PRELUDE + "\n(async () => {\n" + body + f"\n  console.log('{marker}');\n"
        "})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });\n",
        encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(V2)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker in proc.stdout, proc.stdout + proc.stderr
    return proc.stdout


# =========================================================================== #
# #45. Two wording errors every participant reads.
#
# The audio check told a blocked microphone "Your browser is refusing this page
# the microphone", and the situation card's "How it runs" joined the S4A cast
# with ' and ': "Working session, with Dan and Priya and Chris". The cast line
# under the brief already said it properly; one helper now says it for both.
# =========================================================================== #

WORDING = r"""
  const b = boot('?run=r_1', (b) => [{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);
  await b.clock.flush();

  // The helper, on the three shapes the page has.
  assert.strictEqual(b.ctx.listPhrase(['Dan']), 'Dan');
  assert.strictEqual(b.ctx.listPhrase(['Dan', 'Priya']), 'Dan and Priya');
  assert.strictEqual(b.ctx.listPhrase(['Dan', 'Priya', 'Chris']), 'Dan, Priya, and Chris');
  assert.strictEqual(b.ctx.listPhrase([]), '');

  // The situation card, as showSituation builds it.
  const card = b.ctx.briefingHtml(BRIEF, false);
  assert(/Working session, with Dan, Priya, and Chris/.test(card),
    'the situation card does not list the room like a sentence: ' + card);
  assert(/Follow-up, with Dan and Priya/.test(card), card);
  assert(!/Dan and Priya and Chris/.test(card), 'the doubled "and" is still on the card');
  // Escaped per name, not per list: the helper is handed markup-safe items.
  const odd = b.ctx.briefingHtml({ briefing: { parts: [{ label: 'x', with: ['<b>', 'Q'] }] } }, true);
  assert(/with &lt;b&gt; and Q/.test(odd), odd);

  // The cast line, through the page's own loadBrief.
  b.net.route([{ match: '/api/scenarios/', fn: () => b.net.res(200, BRIEF) }]);
  await b.ctx.loadBrief();
  assert.strictEqual(b.dom.$('castLine').innerHTML,
    'You will be talking with <strong>Dan</strong>, <strong>Priya</strong>, and <strong>Chris</strong>.');

  // The blocked-microphone help, as the audio check shows it.
  const denied = vm.runInContext('MIC_CHECK_HELP.denied', b.ctx);
  assert(/Your browser has blocked the microphone for this page\./.test(denied), denied);
  assert(!/refusing this page/.test(denied), denied);
  b.sandbox.navigator.mediaDevices = {
    getUserMedia: () => Promise.reject(Object.assign(new Error('no'), { name: 'NotAllowedError' })),
  };
  b.ctx.runAudioCheck();
  await b.clock.advance(10);
  b.dom.$('micTestBtn').click();
  await b.clock.advance(100);
  assert.strictEqual(b.dom.$('micHelp').textContent, denied);
"""


def test_wording_the_room_list_and_the_blocked_microphone(tmp_path):
    _run(tmp_path, WORDING, "WORDING OK")


# =========================================================================== #
# #42. The microphone error piled up, one copy per press of Start.
#
# MEASURED on production: four presses with the microphone blocked left four
# identical "We couldn't turn on your microphone" paragraphs, in an aria-live
# transcript that a screen reader read back each time. One notice, replaced on
# every failed press, and gone once capture starts.
# =========================================================================== #

ONE_NOTICE = r"""
  const cfg = { micError: 'NotAllowedError' };
  const b = boot('?run=r_1&participant_id=p_test',
                 (b) => [{ match: '/api/run/', fn: () => b.net.res(503, {}) }], { cfg });
  await b.clock.flush();
  const press = async () => { b.dom.$('startBtn').click(); await b.clock.advance(50); };

  await press(); await press(); await press();
  let n = notes(b);
  assert.strictEqual(n.length, 1,
    'three presses left ' + n.length + ' notices: ' + JSON.stringify(n.map(x => x.textContent)));
  assert(/allow microphone access/i.test(n[0].textContent), n[0].textContent);
  assert.strictEqual(b.dom.$('startBtn').disabled, false, 'Start was not offered again');

  // A different failure on the next press replaces the text, not the count.
  cfg.micError = 'NotFoundError';
  await press();
  n = notes(b);
  assert.strictEqual(n.length, 1, 'a second kind of failure added a second notice');
  assert(/find a working microphone/i.test(n[0].textContent), n[0].textContent);
  assert(!/allow microphone access/i.test(n[0].textContent), 'the old reason stayed on screen');

  // Other notices are not the capture notice's to remove.
  b.ctx.appendNotice('The other person’s line broke up for a moment.');
  cfg.micError = null;
  await press();
  n = notes(b);
  assert.strictEqual(n.length, 1, 'capture works now, and the failure is still on screen: '
    + JSON.stringify(n.map(x => x.textContent)));
  assert(/line broke up/.test(n[0].textContent), 'the wrong notice was removed');
  assert.strictEqual(b.set('started'), true, 'the encounter did not start once capture worked');
"""


def test_pressing_start_with_the_microphone_blocked_leaves_one_notice(tmp_path):
    _run(tmp_path, ONE_NOTICE, "ONE NOTICE OK")


# =========================================================================== #
# #41. A microphone that will not work had no way out that was not a withdrawal.
#
# MEASURED on production with the microphone blocked: the page showed the
# error and left Start, which fails the same way again, and on a study link
# "Stop and leave the study" — a withdrawal, recorded as one. The notice now
# carries its own door: a card with the audio check again, the study contact,
# and a way to finish that the run records as mic_failed / camera_failed and
# the survey link carries as the same status, with no /withdraw sent at all.
# =========================================================================== #

MIC_EXIT = r"""
  const RUN = { run_id: 'r_1', participant_id: 'RF_TEST_1', completion_code: 'RF-PARTIAL-1',
                position: 2, total: 4, current: { id: 'S2B' }, done: false, completed: ['S1A'],
                withdrawn: null, cohort: 'study' };
  const CFG = { return_url: 'https://survey.example/back', return_label: 'Return to the survey',
                contact_name: 'Dr Rivera', contact_email: 'rf@example.edu' };
  const button = (el) => (el.children || []).find(c => c.tagName === 'BUTTON');

  async function stuck(cfg, routes) {
    const b = boot('?run=r_1&participant_id=p_rec',
                   (b) => [{ match: '/api/run/', fn: () => b.net.res(503, {}) }], { cfg });
    await b.clock.flush();
    b.sandbox.__run = RUN;
    b.set('run = __run;');
    b.net.route(routes(b));
    b.dom.$('startBtn').click();
    await b.clock.advance(50);
    return b;
  }

  // --- the microphone -------------------------------------------------------
  {
    const cfg = { micError: 'NotAllowedError' };
    const b = await stuck(cfg, (b) => [
      { match: '/api/run/r_1/exit', fn: () => b.net.res(200, { recorded: true, status: 'mic_failed' }) },
      { match: '/api/run/r_1/withdraw', fn: () => b.net.res(200, RUN) },
      { match: '/api/run/config', fn: () => b.net.res(200, CFG) },
    ]);
    const [note] = notes(b);
    const door = button(note);
    assert(door, 'the capture notice offers no way out');
    assert.strictEqual(door.textContent, 'I can’t get my microphone working');

    door.click();
    await b.clock.advance(50);
    assert(shown(b, 'micHelpOverlay'), 'the door opened nothing');
    assert(/microphone/.test(b.dom.$('micHelpTitle').textContent));
    assert(/not the same as stopping/.test(b.dom.$('micHelpBody').innerHTML), b.dom.$('micHelpBody').innerHTML);
    assert(/rf@example\.edu/.test(b.dom.$('micHelpContact').innerHTML),
      'the card names no contact: ' + b.dom.$('micHelpContact').innerHTML);

    // The audio check again, and it starts clean rather than on the old verdict.
    b.dom.$('micStatus').textContent = 'We can hear you';
    b.dom.$('audioCheckContinue').disabled = false;
    b.dom.$('micHelpCheck').onclick();
    await b.clock.advance(10);
    assert(!shown(b, 'micHelpOverlay'));
    assert(shown(b, 'audioCheckOverlay'), 'the audio check did not open');
    assert.strictEqual(b.dom.$('micStatus').textContent, 'Not tested');
    assert.strictEqual(b.dom.$('audioCheckContinue').disabled, true,
      'the second check opened with Continue already armed');
    b.dom.$('audioCheckSkip').click();
    await b.clock.advance(10);
    assert(!shown(b, 'audioCheckOverlay'));
    assert.strictEqual(b.dom.$('startBtn').disabled, false, 'Start is not offered after the check');

    // Still blocked; this time they finish.
    b.dom.$('startBtn').click();
    await b.clock.advance(50);
    assert.strictEqual(notes(b).length, 1);
    button(notes(b)[0]).click();
    await b.clock.advance(50);
    b.dom.$('micHelpLeave').onclick();
    await b.clock.advance(100);

    const sent = b.sent.filter(c => c.method === 'POST' && c.url.includes('/api/run/r_1/exit'));
    assert.strictEqual(sent.length, 1, 'the exit was not recorded');
    const body = JSON.parse(sent[0].body);
    assert.strictEqual(body.status, 'mic_failed');
    assert.strictEqual(body.capture_kind, 'denied');
    assert.strictEqual(body.participant_id, 'p_rec', 'the run cannot tell whose exit this is');
    assert.strictEqual(posts(b, '/withdraw').length, 0, 'a broken microphone was sent as a withdrawal');

    assert(shown(b, 'nextOverlay'), 'no closing card');
    assert.strictEqual(b.dom.$('nextTitle').textContent, 'Finishing here');
    const closing = b.dom.$('nextBody').innerHTML;
    assert(/noted that your microphone did not work/.test(closing), closing);
    assert(/not recorded as you stopping the study/.test(closing), closing);
    assert(!/RF-PARTIAL-1/.test(closing), 'a completion code was handed out on the way out');
    assert(/rf@example\.edu/.test(closing), 'the closing card lost the contact');
    b.dom.$('nextBtn').onclick();
    assert(/^https:\/\/survey\.example\/back\?/.test(b.loc.href), b.loc.href);
    assert(/[?&]status=mic_failed(&|$)/.test(b.loc.href), 'the survey cannot tell this exit apart: ' + b.loc.href);
    assert(!/[?&]code=[^&]/.test(b.loc.href), 'a code went back to the survey: ' + b.loc.href);
  }

  // --- the camera, a server that cannot be reached, and no survey link -----
  {
    const cfg = { cameraError: 'NotReadableError' };
    const b = await stuck(cfg, (b) => [
      { match: '/api/run/r_1/exit', fn: () => b.net.res(503, {}) },
      { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
    ]);
    const door = button(notes(b)[0]);
    assert.strictEqual(door.textContent, 'I can’t get my camera working');
    door.click();
    await b.clock.advance(50);
    assert(/camera/.test(b.dom.$('micHelpTitle').textContent));
    b.dom.$('micHelpLeave').onclick();
    await b.clock.advance(100);
    const body = JSON.parse(b.sent.find(c => c.url.includes('/exit')).body);
    assert.strictEqual(body.status, 'camera_failed');
    const closing = b.dom.$('nextBody').innerHTML;
    assert(/could not reach the server/.test(closing), 'an unrecorded exit was reported as recorded: ' + closing);
    assert(/close this window/.test(closing), closing);
  }
"""


def test_a_microphone_that_will_not_work_has_a_way_out_that_is_not_a_withdrawal(tmp_path):
    _run(tmp_path, MIC_EXIT, "MIC EXIT OK")


# =========================================================================== #
# #44. The header read "Loading…" through the whole intro.
#
# The scenario was not fetched until the fiction notice and the audio check were
# both done, so for the first minute or two the header looked stuck. The brief
# is asked for as soon as the run has said which scenario it is, and the answer
# is shared with loadBrief rather than fetched twice.
# =========================================================================== #

RUN_VIEW = r"""
const RUN_VIEW = { run_id: 'r_1', participant_id: 'RF_TEST_1', completion_code: '', position: 1,
                   total: 4, current: { id: 'S4A' }, next: { id: 'S1B' }, done: false,
                   completed: [], withdrawn: null, cohort: 'study',
                   timing: { min_seconds: 420, wrap_seconds: 720, max_seconds: 780 } };
function studyRoutes(b) {
  return [
    { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
    { match: '/api/run/r_1', fn: () => b.net.res(200, RUN_VIEW) },
    { match: '/api/scenarios/S4A', fn: () => b.net.res(200, BRIEF) },
    { match: '/api/participant', fn: () => b.net.res(200, { participant_id: 'p_minted_1' }) },
  ];
}
"""

TITLE_EARLY = RUN_VIEW + r"""
  // --- a study arrival: /start handed the page its record --------------------
  {
    const b = boot('?run=r_1&participant_id=p_rec', studyRoutes);
    await b.clock.advance(50);
    assert(shown(b, 'fictionOverlay'), 'the intro did not open on the fiction notice');
    assert.strictEqual(b.dom.$('title').textContent, 'Planning an internal rollout',
      'the header is not showing the scenario during the fiction notice: '
      + JSON.stringify(b.dom.$('title').textContent));
    b.dom.$('fictionAck').click();
    await b.clock.advance(50);
    assert(shown(b, 'audioCheckOverlay'));
    assert.strictEqual(b.dom.$('title').textContent, 'Planning an internal rollout');
    b.dom.$('audioCheckSkip').click();
    await b.clock.advance(50);
    assert(shown(b, 'situationOverlay'), 'the brief never loaded');
    assert.strictEqual(gets(b, '/api/scenarios/').length, 1,
      'the brief was fetched twice: ' + JSON.stringify(gets(b, '/api/scenarios/').map(c => c.url)));
    assert(/participant_id=p_rec/.test(gets(b, '/api/scenarios/')[0].url),
      'the brief was fetched without the participant, so the assigned name can differ');
  }

  // --- a direct researcher link: the title does not wait for a record -------
  {
    const b = boot('?scenario=S4A', studyRoutes);
    await b.clock.advance(50);
    assert(shown(b, 'fictionOverlay'));
    assert.strictEqual(b.dom.$('title').textContent, 'Planning an internal rollout');
  }

  // --- a brief that fails first time is asked for again on Retry ------------
  {
    let n = 0;
    const b = boot('?run=r_1&participant_id=p_rec', (b) => [
      { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
      { match: '/api/run/r_1', fn: () => b.net.res(200, RUN_VIEW) },
      { match: '/api/scenarios/S4A', fn: () => (++n === 1 ? b.net.res(503, {}) : b.net.res(200, BRIEF)) },
    ]);
    await b.clock.advance(50);
    b.dom.$('fictionAck').click();
    await b.clock.advance(50);
    b.dom.$('audioCheckSkip').click();
    await b.clock.advance(50);
    assert.strictEqual(n, 2, 'loadBrief reused a failed prefetch instead of asking again');
    assert(shown(b, 'situationOverlay'));
    assert.strictEqual(b.dom.$('title').textContent, 'Planning an internal rollout');
  }
"""


def test_the_header_shows_the_scenario_during_the_intro(tmp_path):
    _run(tmp_path, TITLE_EARLY, "TITLE EARLY OK")


def test_the_header_placeholder_is_neutral():
    """What shows for the moment before the title lands, and whenever the
    brief cannot be fetched: never a word that reads as stuck."""
    src = V2.read_text(encoding="utf-8")
    placeholder = re.search(r'<span id="title">([^<]*)</span>', src).group(1)
    assert placeholder and "Loading" not in placeholder and "…" not in placeholder, placeholder


# =========================================================================== #
# #39. Opening a direct link made a participant record; refreshing made another.
#
# MEASURED on production: p_1790365076_2bb22c and p_1790365355_5567bf, one
# person, no run, no session, no acknowledgement of anything. The boot sequence
# minted before the fiction notice and never kept the id. Now nothing is minted
# until the notice is acknowledged, and the minted id goes into the address bar,
# so the refresh that used to make a second person reuses the first.
# =========================================================================== #

ONE_RECORD = RUN_VIEW + r"""
  const store = memoryStorage();   // one tab: a refresh keeps its sessionStorage

  // Open the link, and refresh it while the notice is still up.
  const first = boot('?scenario=S4A&key=k7', studyRoutes, { sessionStorage: store });
  await first.clock.advance(50);
  assert(shown(first, 'fictionOverlay'));
  assert.strictEqual(posts(first, '/api/participant').length, 0,
    'a participant record was made before the notice was acknowledged');
  const second = boot(first.loc.search, studyRoutes, { sessionStorage: store });
  await second.clock.advance(50);
  assert.strictEqual(posts(second, '/api/participant').length, 0, 'the refresh made a record');

  // Acknowledge: now, and only now, one record — kept in the address bar.
  second.dom.$('fictionAck').click();
  await second.clock.advance(50);
  assert.strictEqual(posts(second, '/api/participant').length, 1);
  const url = new URLSearchParams(second.loc.search);
  assert.strictEqual(url.get('participant_id'), 'p_minted_1',
    'the minted id is not in the address bar: ' + second.loc.search);
  assert.strictEqual(url.get('scenario'), 'S4A', 'the scenario fell out of the address bar');
  assert.strictEqual(url.get('key'), 'k7', 'the key fell out of the address bar');
  assert(shown(second, 'audioCheckOverlay'));

  // Refresh on the audio check. The same person comes back as the same person.
  const third = boot(second.loc.search, studyRoutes, { sessionStorage: store });
  await third.clock.advance(50);
  assert.strictEqual(posts(third, '/api/participant').length, 0,
    'a refresh POSTed /api/participant again, so one person is now two records');
  assert(!shown(third, 'fictionOverlay'), 'the acknowledged notice came back');
  assert(shown(third, 'audioCheckOverlay'));
  third.dom.$('audioCheckSkip').click();
  await third.clock.advance(50);
  const brief = gets(third, '/api/scenarios/S4A').pop();
  assert(/participant_id=p_minted_1/.test(brief.url),
    'the brief after a refresh is for a different participant: ' + brief.url);
  assert(shown(third, 'situationOverlay'));

  // And the room itself opens on that record.
  assert.strictEqual(third.set('participantId'), 'p_minted_1');
"""


def test_a_refresh_reuses_the_participant_record_it_already_made(tmp_path):
    _run(tmp_path, ONE_RECORD, "ONE RECORD OK")


# =========================================================================== #
# #37. Other people's voices are picked up, transcribed as the participant, and
# answered. Browser noise suppression removes steady noise, not speech, so the
# researchers' decision (2026-09-28) is a requirement on the room: a quiet room,
# or noise-cancelling headphones, said prominently on the first intro screen and
# again at the audio check. Read from the markup, because it IS markup: both
# overlays ship it, and no script has to run for it to be there.
# =========================================================================== #

def _overlay(src: str, overlay_id: str) -> str:
    start = src.index(f'id="{overlay_id}"')
    end = src.find('<div class="consent-overlay"', start)
    return src[start:end if end > 0 else len(src)]


def _quiet_notice(block: str) -> str:
    m = re.search(r'<div class="quiet-notice"[^>]*>(.*?)</div>', block, re.S)
    assert m, "no quiet-room notice in this overlay"
    return " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())


def test_the_quiet_room_requirement_is_first_on_the_first_screen_and_again_at_the_check():
    src = V2.read_text(encoding="utf-8")
    first = _overlay(src, "fictionOverlay")
    check = _overlay(src, "audioCheckOverlay")
    said_first, said_again = _quiet_notice(first), _quiet_notice(check)
    assert re.search(r"quiet room", said_first, re.I), said_first
    assert re.search(r"noise-cancelling headphones also work", said_first, re.I), said_first
    assert re.search(r"other people's voices", said_first, re.I), (
        "the notice does not say why: " + said_first)
    assert said_first == said_again, "the audio check says something different:\n" \
        f"  first screen: {said_first}\n  audio check:  {said_again}"
    # Prominent: before the fiction notice's own heading, not under it.
    assert first.index('class="quiet-notice"') < first.index("<h2>"), \
        "the requirement sits below the fiction notice on the first screen"
    # And styled as a requirement, not as the muted small print around it.
    assert re.search(r"\.quiet-notice\s*\{[^}]*border", src), "the notice has no box"


# =========================================================================== #
# The build tag (the BUILD contract, 2026-09-28): the image carries its commit,
# /api/run/config serves it as `build`, and the page shows it small and out of
# the way — "build 4798e64" — or nothing at all when the server does not say.
# =========================================================================== #

BUILD_TAG = r"""
  const tag = async (config, search) => {
    const b = boot(search || '?scenario=S4A', (b) => [
      { match: '/api/run/config', fn: () => b.net.res(200, config) },
      { match: '/api/run/', fn: () => b.net.res(503, {}) },
      { match: '/api/scenarios/', fn: () => b.net.res(200, BRIEF) },
    ]);
    await b.clock.advance(50);
    const el = b.dom.$('buildTag');
    return { text: el.textContent, visible: el.style.display !== 'none' };
  };

  let t = await tag({ return_url: '', build: '4798e64' });
  assert.deepStrictEqual(t, { text: 'build 4798e64', visible: true });

  t = await tag({ return_url: '', build: '4798e64b2f0c9a1d3e5f7a9b0c1d2e3f4a5b6c7d' });
  assert.strictEqual(t.text, 'build 4798e64', 'a full hash was not shortened: ' + t.text);

  t = await tag({ return_url: '', build: null });
  assert.deepStrictEqual(t, { text: '', visible: false }, 'an unstamped build showed a tag');
  t = await tag({ return_url: '' });
  assert.deepStrictEqual(t, { text: '', visible: false }, 'a server without the field showed a tag');

  // On a run that cannot be loaded too: that blocking card is the screen a bug
  // report is most often a picture of.
  t = await tag({ return_url: '', build: 'a1b2c3d' }, '?run=r_1');
  assert.deepStrictEqual(t, { text: 'build a1b2c3d', visible: true });
"""


def test_the_page_shows_which_build_served_it(tmp_path):
    _run(tmp_path, BUILD_TAG, "BUILD TAG OK")
