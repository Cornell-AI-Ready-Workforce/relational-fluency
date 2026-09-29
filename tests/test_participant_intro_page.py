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
// The page's notices: the capture failure's in its slot under the header, the
// rest in the transcript.
const notes = (b) => ['captureSlot', 'transcript'].flatMap(id => b.dom.$(id).children)
  .filter(c => /system-note/.test(c.className || ''));

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

  // The blocked-microphone help, as the audio check shows it, and the room's:
  // one instruction, each ending on its own button (FLOW-08).
  const room = vm.runInContext('CAPTURE_MESSAGES.denied', b.ctx);
  assert.strictEqual(room, 'Your browser has blocked the microphone for this page. Click the mic or '
    + 'camera icon at the end of the address bar, choose Allow, then reload this page and press '
    + 'Start conversation again.');
  const denied = vm.runInContext('MIC_CHECK_HELP.denied', b.ctx);
  assert.strictEqual(denied, room.replace(' and press Start conversation again.', ' and try again.'));
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
  assert(/blocked the microphone for this page/.test(n[0].textContent), n[0].textContent);
  // Under the header, next to Start: at the end of the transcript it was
  // 800 px below Start on a phone, and nothing on screen changed.
  assert.strictEqual(b.dom.$('captureSlot').children.length, 1, 'the notice is not next to Start');
  assert.strictEqual(b.dom.$('startBtn').disabled, false, 'Start was not offered again');

  // A different failure on the next press replaces the text, not the count.
  cfg.micError = 'NotFoundError';
  await press();
  n = notes(b);
  assert.strictEqual(n.length, 1, 'a second kind of failure added a second notice');
  assert(/find a working microphone/i.test(n[0].textContent), n[0].textContent);
  assert(!/blocked the microphone/.test(n[0].textContent), 'the old reason stayed on screen');

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
    // FLOW-08: the old failure is not left on screen, and what to do next is.
    assert.strictEqual(b.dom.$('captureSlot').children.length, 0, 'the old failure stayed up after the check');
    assert(b.dom.$('gateNote').classList.contains('show')
      && /Press Start conversation when you are ready/.test(b.dom.$('gateNote').textContent),
      'nothing says to press Start: ' + b.dom.$('gateNote').textContent);

    // Still blocked; this time they finish.
    b.dom.$('startBtn').click();
    await b.clock.advance(50);
    assert.strictEqual(b.dom.$('captureSlot').children.length, 1);
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

  // --- a direct researcher link: there is no run to write the note on -------
  {
    const b = boot('?scenario=S4A&participant_id=p_rec', (b) => [
      { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
      { match: '/api/scenarios/', fn: () => b.net.res(200, BRIEF) },
    ], { cfg: { micError: 'NotFoundError' } });
    await b.clock.advance(50);
    b.dom.$('startBtn').click();
    await b.clock.advance(50);
    button(notes(b)[0]).click();
    await b.clock.advance(50);
    assert(!/we note/.test(b.dom.$('micHelpBody').innerHTML),
      'a direct link promises a note it has nowhere to write: ' + b.dom.$('micHelpBody').innerHTML);
    b.dom.$('micHelpLeave').onclick();
    await b.clock.advance(100);
    assert.strictEqual(b.sent.filter(c => c.url.includes('/exit')).length, 0);
    assert(!/noted that/.test(b.dom.$('nextBody').innerHTML), b.dom.$('nextBody').innerHTML);
    assert.strictEqual(b.dom.$('nextTitle').textContent, 'Finishing here');
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
    assert first.index('class="quiet-notice"') < first.index("<h2"), \
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


# =========================================================================== #
# #43. The intro screens are not usable with a screen reader.
#
# Seen on production (2026-09-25, /v2?scenario=S4A): the pop-up screens were
# not announced as dialogs, focus never moved into them, and Tab walked the
# page behind the scrim. Every card is now a dialog named by its heading, takes
# focus when it opens and hands it back when it closes, and a card that covers
# the page leaves the page behind it inert (aria-hidden and a focus guard where
# the engine has no inert). The drop card is a dialog but not a modal one: the
# page around it, "Stop and leave the study" included, stays in reach.
# =========================================================================== #

def _open_tag(markup: str, element_id: str) -> str:
    m = re.search(r'<[a-z0-9]+\b[^>]*\bid="%s"[^>]*>' % re.escape(element_id), markup)
    assert m, f"no element with id {element_id}"
    return m.group(0)


def _attrs(tag: str) -> dict:
    return dict(re.findall(r'([\w-]+)="([^"]*)"', tag))


def test_every_card_is_a_dialog_named_by_its_own_heading():
    src = V2.read_text(encoding="utf-8")
    markup = src[:src.index("<script>")]
    cards = re.findall(r'<div class="consent-overlay" id="(\w+)"', markup)
    assert set(cards) == {"situationOverlay", "fictionOverlay", "audioCheckOverlay",
                          "micHelpOverlay", "nextOverlay"}, cards
    for card in cards:
        a = _attrs(_open_tag(markup, card))
        assert (a.get("role"), a.get("aria-modal"), a.get("tabindex")) == ("dialog", "true", "-1"), (
            f"{card} is not a modal dialog that can take focus: {a}")
        block = _overlay(markup, card)
        assert re.search(r'<h2 id="%s">' % re.escape(a.get("aria-labelledby", "")), block), (
            f"{card} is not named by a heading of its own")
        # A scroll region in a card is a named tab stop, so a keyboard can
        # scroll it: Safari does not make a scroller focusable, and Chrome
        # made this one an unnamed stop.
        for tag in re.findall(r'<div class="[^"]*card-scroll[^"]*"[^>]*>', block):
            t = _attrs(tag)
            assert (t.get("tabindex"), t.get("role"), t.get("aria-labelledby")) == (
                "0", "region", a["aria-labelledby"]), f"{card}: {tag}"

    drop = _attrs(_open_tag(markup, "dropNote"))
    assert drop.get("role") == "dialog" and drop.get("aria-labelledby") == "dropTitle", drop
    assert drop.get("aria-modal") != "true", "the drop card would lock the page, and the way to leave the study"
    assert '<b id="dropTitle">' in markup

    # The one card built in script.
    fn = src[src.index("function showBlockingError("):src.index("// ---------- situation popup")]
    for needle in ("setAttribute('role', 'dialog')", "setAttribute('aria-modal', 'true')",
                   "setAttribute('aria-labelledby', 'errTitle')", '<h2 id="errTitle">'):
        assert needle in fn, needle

    # And no card is shown or hidden except through the helper, which is what
    # moves focus and sets the page behind it inert.
    script = src[src.index("<script>"):]
    assert not re.search(r"Overlay'\)\.style\.display\s*=", script)
    assert "$('dropNote').classList" not in script


# Focus goes where the page puts it, and a card knows what is in it: the stub's
# elements do neither, so these harnesses give them both, and stand one element
# in for the page behind the cards among the body's children.
A11Y_DOM = r"""
function a11y(b) {
  const doc = b.dom.document;
  const track = (e) => {
    if (e && !e.__a11y) {
      e.__a11y = true;
      e.attrs = {};
      e.focus = function () { doc.activeElement = this; };
      e.setAttribute = function (k, v) { this.attrs[k] = String(v); };
      e.removeAttribute = function (k) { delete this.attrs[k]; };
      e.contains = function (x) { return x === this || this.children.includes(x); };
    }
    return e;
  };
  const byId = doc.getElementById.bind(doc);
  doc.getElementById = (id) => track(byId(id));
  const make = doc.createElement.bind(doc);
  doc.createElement = (t) => track(make(t));
  const page = b.dom.$('pageBehind');
  doc.body.children.push(page, ...['fictionOverlay', 'audioCheckOverlay', 'situationOverlay',
    'micHelpOverlay', 'nextOverlay', 'dropNote', 'errOverlay'].map(id => b.dom.$(id)));
  doc.activeElement = doc.body;
  return { doc, page };
}
const button = (el) => (el.children || []).find(c => c.tagName === 'BUTTON');
"""

DIALOGS = RUN_VIEW + A11Y_DOM + r"""
  // --- the intro, card by card ----------------------------------------------
  {
    const b = boot('?run=r_1&participant_id=p_rec', studyRoutes);
    const { doc, page } = a11y(b);
    await b.clock.advance(50);
    for (const [id, next] of [['fictionOverlay', 'fictionAck'], ['audioCheckOverlay', 'audioCheckSkip'],
                              ['situationOverlay', 'situationStart']]) {
      assert(shown(b, id), id + ' did not open');
      assert.strictEqual(doc.activeElement, b.dom.$(id), 'focus did not move into ' + id);
      assert.strictEqual(page.inert, true, 'the page behind ' + id + ' can still be reached');
      assert.strictEqual(b.dom.$(id).inert, false, id + ' is inert itself');
      b.dom.$(next).click();
      await b.clock.advance(50);
    }
    assert(!shown(b, 'situationOverlay'));
    assert.strictEqual(b.set('started'), true, 'the conversation did not start');
    assert.strictEqual(page.inert, false, 'the page stayed inert after the last card closed');

    // The drop card: focus moves to it, and the page stays in reach.
    const end = b.dom.$('stopBtn');
    end.focus();
    b.ctx.onConnectionDropped();
    const drop = b.dom.$('dropNote');
    assert(drop.classList.contains('show'), 'no drop card');
    assert.strictEqual(doc.activeElement, drop, 'focus did not move to the drop card');
    assert.strictEqual(page.inert, false, 'the drop card locked the page, "Stop and leave the study" with it');
    b.dom.$('dropReconnect').click();
    await b.clock.advance(50);
    assert(!drop.classList.contains('show'));
    assert.strictEqual(doc.activeElement, end, 'focus was not handed back when the drop card closed');
  }

  // --- a card's focus goes back to what opened it --------------------------
  {
    const b = boot('?scenario=S4A&participant_id=p_rec', studyRoutes, { cfg: { micError: 'NotAllowedError' } });
    const { doc, page } = a11y(b);
    await b.clock.advance(50);
    b.dom.$('fictionAck').click(); await b.clock.advance(50);
    b.dom.$('audioCheckSkip').click(); await b.clock.advance(50);
    b.dom.$('situationStart').click(); await b.clock.advance(50);
    const door = button(notes(b)[0]);
    assert.strictEqual(doc.activeElement, door, 'the capture failure did not take focus to its way out');

    door.click(); await b.clock.advance(50);
    assert.strictEqual(doc.activeElement, b.dom.$('micHelpOverlay'), 'focus did not move into the help card');
    assert.strictEqual(page.inert, true);
    b.dom.$('micHelpBack').onclick();
    assert(!shown(b, 'micHelpOverlay'));
    assert.strictEqual(doc.activeElement, door, 'closing the card did not give focus back to its button');
    assert.strictEqual(page.inert, false, 'the page stayed inert behind a closed card');

    // One card after another: the audio check, opened from the help card,
    // hands focus back to the same button.
    door.click(); await b.clock.advance(50);
    b.dom.$('micHelpCheck').onclick(); await b.clock.advance(10);
    assert.strictEqual(doc.activeElement, b.dom.$('audioCheckOverlay'));
    assert.strictEqual(page.inert, true);
    b.dom.$('audioCheckSkip').click(); await b.clock.advance(10);
    // To Start: the button that opened the cards went with its notice
    // (FLOW-08), and Start is what the note after the check names.
    assert.strictEqual(doc.activeElement, b.dom.$('startBtn'), 'focus was left on a removed button');
    assert.strictEqual(page.inert, false);
  }

  // --- the blocking error is a card like the others ------------------------
  {
    const b = boot('?run=r_1', (b) => [{ match: '/api/run/', fn: () => b.net.res(503, {}) },
                                        { match: '/api/run/config', fn: () => b.net.res(200, {}) }]);
    const { doc, page } = a11y(b);
    await b.clock.advance(50);
    assert(shown(b, 'errOverlay'));
    assert.strictEqual(doc.activeElement, b.dom.$('errOverlay'), 'focus did not move into the error card');
    assert.strictEqual(page.inert, true);
  }

  // --- an engine without inert: aria-hidden, and focus sent back -----------
  {
    const b = boot('?run=r_1&participant_id=p_rec', studyRoutes,
                   { extra: { HTMLElement: function HTMLElement() {} } });
    const { doc, page } = a11y(b);
    await b.clock.advance(50);
    const card = b.dom.$('fictionOverlay');
    assert.strictEqual(page.attrs['aria-hidden'], 'true', 'the page behind is still read out');
    assert(!('aria-hidden' in card.attrs), 'the card itself was hidden');
    const guard = (b.dom.docListeners.focusin || [])[0];
    assert(guard, 'nothing keeps focus in the card');
    page.focus(); guard({ target: page });
    assert.strictEqual(doc.activeElement, card, 'focus that left the card was not sent back');
    b.dom.$('fictionAck').click(); await b.clock.advance(50);
    b.dom.$('audioCheckSkip').click(); await b.clock.advance(50);
    b.dom.$('situationStart').click(); await b.clock.advance(50);
    assert(!('aria-hidden' in page.attrs), 'the page stayed hidden after the last card closed');
  }
"""


def test_every_card_takes_focus_gives_it_back_and_leaves_the_page_behind_inert(tmp_path):
    _run(tmp_path, DIALOGS, "DIALOGS OK")


# The drop card and the gate note are placed under the header (placeNote), and
# again whenever the page moves under them. Placed only when they came up, a
# drop while the page was scrolled left the card 35px from the top, over "Stop
# and leave the study" and Start once the participant scrolled back up (1280
# and 1440 wide, measured), and after a rotation the card kept the old
# screen's top and height, so Reconnect was below the new one. The stub has no
# layout, so the header's place is given here, as a browser would report it.
PLACED = RUN_VIEW + A11Y_DOM + r"""
  const b = boot('?run=r_1&participant_id=p_rec', studyRoutes);
  a11y(b);
  await b.clock.advance(50);
  b.dom.$('fictionAck').click(); await b.clock.advance(50);
  b.dom.$('audioCheckSkip').click(); await b.clock.advance(50);
  b.dom.$('situationStart').click(); await b.clock.advance(50);
  let headerBottom = -193;   // scrolled 300px: only the banner is on screen
  b.dom.$('aiBanner').getBoundingClientRect = () => ({ bottom: 27 });
  b.dom.document.querySelector('header').getBoundingClientRect = () => ({ bottom: headerBottom });
  b.sandbox.innerHeight = 800;
  b.sandbox.requestAnimationFrame = (f) => { f(); return 1; };
  const drop = b.dom.$('dropNote');
  b.dom.document.querySelectorAll = (sel) =>
    (sel === '.gate-note.show' && drop.classList.contains('show') ? [drop] : []);

  b.ctx.onConnectionDropped();
  assert(drop.classList.contains('show'), 'no drop card');
  assert.strictEqual(drop.style.top, '35px');
  headerBottom = 107;        // back at the top
  b.fire('scroll');
  assert.strictEqual(drop.style.top, '115px', 'the card stayed over the header: ' + drop.style.top);
  assert.strictEqual(drop.style.maxHeight, '673px');
  b.sandbox.innerHeight = 375;   // turned on its side
  headerBottom = 158;
  b.fire('resize');
  assert.strictEqual(drop.style.top, '166px');
  assert.strictEqual(drop.style.maxHeight, '197px', 'the card kept the old screen\'s height');
"""


def test_a_note_follows_the_header_when_the_page_scrolls_or_turns(tmp_path):
    _run(tmp_path, PLACED, "PLACED OK")


# L7, and the review of it. On a phone the whole header was pinned: 261-375px,
# half a 667px screen and more for the whole conversation, and 40-70px taller
# in S2/S4 (Move on) than in S1/S3. What is pinned now is the banner and one
# status row under it, whose turn it is, the ring and the timer, which is the
# same row in every condition; the title and every button scroll with the
# page. Measured in headless Chrome: 119px at 320-390 wide in S1A-S4A alike.

def _css_block(src: str, opener: str) -> str:
    start = src.index(opener)
    at, depth = src.index("{", start), 0
    for i in range(at, len(src)):
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[at + 1:i]
    raise AssertionError(f"unclosed {opener}")


def test_on_a_phone_only_the_banner_and_the_status_row_are_pinned():
    src = V2.read_text(encoding="utf-8")
    markup = src[:src.index("<script>")]
    status = markup[markup.index('id="statusGroup"'):markup.index('id="actionGroup"')]
    actions = markup[markup.index('id="actionGroup"'):markup.index("</header>")]
    assert re.findall(r'\bid="(\w+)"', status) == ["statusGroup", "turnState", "gate", "gateFill",
                                                   "gateLabel", "timer"], status
    assert "<button" not in status
    assert re.findall(r'<button id="(\w+)"', actions) == ["startBtn", "advanceBtn", "stopBtn", "leaveBtn"]

    css = src[src.index("<style>"):src.index("</style>")]
    sticky = sorted(sel.strip().split("\n")[-1].strip()
                    for sel in re.findall(r"([^{}]+?)\s*\{[^}]*position:\s*sticky", css))
    assert sticky == [".ai-banner", ".status-group"], sticky
    phone = _css_block(css, "/* ---------- Mobile / small screens ---------- */")
    assert re.search(r"header, \.controls \{ display: contents; \}", phone)
    # Above a phone the groups have no box: the header row is as it was.
    assert re.search(r"\.status-group, \.action-group \{ display: contents; \}",
                     css[:css.index("/* ---------- Mobile / small screens")])


BANNER = RUN_VIEW + r"""
  const b = boot('?run=r_1&participant_id=p_rec', studyRoutes);
  await b.clock.advance(50);
  const set = {};
  b.dom.document.documentElement = { style: { setProperty: (k, v) => { set[k] = v; } } };
  b.dom.$('aiBanner').offsetHeight = 46;   // two lines, on a narrow phone
  b.fire('resize');
  assert.strictEqual(set['--banner-h'], '46px', 'the status row does not know where the banner ends');
"""


def test_the_pinned_status_row_sits_under_the_banner_however_tall_it_is(tmp_path):
    _run(tmp_path, BANNER, "BANNER OK")


def test_the_page_has_one_top_level_heading_and_it_is_the_scenario():
    """#43: 'the page has no top-level heading'. The header's title is it."""
    src = V2.read_text(encoding="utf-8")
    assert len(re.findall(r"<h1\b", src)) == 1, re.findall(r"<h1\b[^>]*>", src)
    assert re.search(r'<h1 class="title"><span id="title">', src), "the h1 is not the scenario title"
    assert "createElement('h1')" not in src


# =========================================================================== #
# A11Y-02 (UX audit, 2026-09-28). The transcript was aria-live, so a screen
# reader read every caption as it was rewritten: a character's line sentence by
# sentence over the character's own voice, and the participant's words back to
# them on every interim result while they spoke; on speakers the microphone can
# hear that. And the one cue a screen-reader user needs, "You can speak now",
# was never announced. The transcript is now a named log that is not read out,
# and one visually hidden status region says what the app itself says, the
# start cue and "You can speak now", and never "<Name> is speaking".
# =========================================================================== #

def test_the_transcript_is_a_quiet_log_and_the_page_has_one_status_region():
    src = V2.read_text(encoding="utf-8")
    markup = src[:src.index("<script>")]
    t = _attrs(_open_tag(markup, "transcript"))
    assert (t.get("role"), t.get("aria-live"), t.get("tabindex")) == ("log", "off", "0"), t
    assert t.get("aria-label"), "the transcript has no name"
    s = _attrs(_open_tag(markup, "srStatus"))
    assert (s.get("role"), s.get("class")) == ("status", "sr-only"), s
    assert re.search(r"\.sr-only\s*\{[^}]*clip", src), "the status region is not visually hidden"
    # The gate note is said through the status region, not a second one.
    assert "role" not in _attrs(_open_tag(markup, "gateNote"))


LIVE = RUN_VIEW + A11Y_DOM + r"""
  const b = boot('?run=r_1&participant_id=p_rec', studyRoutes);
  a11y(b);
  await b.clock.advance(50);
  b.dom.$('fictionAck').click(); await b.clock.advance(50);
  b.dom.$('audioCheckSkip').click(); await b.clock.advance(50);
  b.dom.$('situationStart').click(); await b.clock.advance(50);
  assert.strictEqual(b.set('started'), true);
  const said = () => b.dom.$('srStatus').textContent;
  const frame = (m) => b.ctx.handleServerFrame({ data: JSON.stringify(m) });
  frame({ type: 'session', session_id: 's_1', scenario: { title: 'T', mode: 'group' }, cast: BRIEF.cast });

  frame({ type: 'awaiting_participant', reason: 'start', names: ['Dan'] });
  assert.strictEqual(said(), "You start the conversation. Say hello when you're ready.",
    'the start cue was not said: ' + JSON.stringify(said()));

  frame({ type: 'turn_open' });
  assert.strictEqual(said(), 'You can speak now');
  b.dom.$('srStatus').textContent = '';
  frame({ type: 'turn_open' });
  assert.strictEqual(said(), '', 'a floor that was already open was said again');

  // A character talking is the voice's to say, not the status region's.
  b.ctx.setActiveSpeaker('dan');
  assert.strictEqual(b.dom.$('turnState').textContent, 'Dan is speaking');
  assert.strictEqual(said(), '', '"Dan is speaking" was announced: ' + JSON.stringify(said()));
  b.ctx.setActiveSpeaker(null);
  frame({ type: 'turn_open' });
  assert.strictEqual(said(), 'You can speak now', 'the floor opening again was not said');

  // A caption is not said at all: the transcript is not the live region.
  frame({ type: 'user_transcript', text: 'I think we should', final: false });
  assert.strictEqual(said(), 'You can speak now', 'a caption reached the status region');

  // What the app itself says is said, and the same words twice are said twice.
  b.ctx.appendNotice('The other person’s line broke up for a moment.');
  assert.strictEqual(said(), 'The other person’s line broke up for a moment.');
  b.ctx.showGateNote('Keep going.');
  const first = said();
  b.ctx.showGateNote('Keep going.');
  assert.strictEqual(first.trim(), 'Keep going.');
  assert.notStrictEqual(said(), first, 'a second press of End was not said again');
  assert.strictEqual(said().trim(), 'Keep going.');

  // And the status region is not hidden with the page behind a card.
  b.dom.document.body.children.push(b.dom.$('srStatus'));
  b.ctx.showOverlay(b.dom.$('nextOverlay'));
  assert.strictEqual(b.dom.$('pageBehind').inert, true);
  assert.strictEqual(b.dom.$('srStatus').inert, false, 'the status region went inert behind a card');
"""


def test_the_status_region_says_the_notices_and_the_floor_and_not_the_captions(tmp_path):
    _run(tmp_path, LIVE, "LIVE OK")


# =========================================================================== #
# A11Y-04. The app's notices in the transcript (a lost voice, a microphone that
# stopped, the gate notes) were muted italic 12px on grey, 3.79:1: the faintest
# text on the page for the notices a participant most needs. Ink, upright, 14px.
# =========================================================================== #

def _rule(src: str, selector: str) -> str:
    m = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", src, re.M)
    assert m, f"no rule for {selector}"
    return m.group(1)


def test_the_page_notices_are_readable():
    src = V2.read_text(encoding="utf-8")
    note = _rule(src, ".transcript .system-note")
    assert re.search(r"(?<!-)color:\s*var\(--fg\)", note), note
    assert "italic" not in note, note
    size = float(re.search(r"font-size:\s*([\d.]+)px", note).group(1))
    assert size >= 14, note


# =========================================================================== #
# A11Y-06 and FLOW-13. The audio check's verdicts changed with nothing said,
# "I heard it" appeared in the status column without focus, the card never
# said why Continue stayed disabled, and "Didn't hear it?" opened with the
# first play, before the participant had answered, reading as a fault.
# =========================================================================== #

def test_the_audio_check_says_its_verdicts_and_why_continue_waits():
    src = V2.read_text(encoding="utf-8")
    markup = src[:src.index("<script>")]
    for row in ("mic", "spk", "cam"):
        assert _attrs(_open_tag(markup, f"{row}Status")).get("role") == "status", row
        assert _attrs(_open_tag(markup, f"{row}TestBtn")).get("aria-describedby") == f"{row}Status", row
    cont = _attrs(_open_tag(markup, "audioCheckContinue"))
    note = re.search(r'<p class="check-note" id="(\w+)">([^<]*)</p>', markup)
    assert note and cont.get("aria-describedby") == note.group(1), cont
    assert note.group(2) == "Continue unlocks when all three checks have passed.", note.group(2)


SPEAKER_CHECK = A11Y_DOM + r"""
  const b = boot('?run=r_1', (b) => [{ match: '/api/run/', fn: () => b.net.res(503, {}) }]);
  const { doc } = a11y(b);
  await b.clock.flush();
  // The stub's audio graph has no oscillator; the chime needs three.
  b.sandbox.AudioContext.prototype.createOscillator = () =>
    ({ type: '', frequency: {}, connect() {}, start() {}, stop() {} });
  b.ctx.runAudioCheck();
  await b.clock.advance(10);
  const help = b.dom.$('spkHelp');

  b.dom.$('spkTestBtn').click();
  await b.clock.advance(10);
  const heard = () => b.dom.$('spkStatus').children.filter(c => c.tagName === 'BUTTON').pop();
  assert(heard() && heard().textContent === 'I heard it', 'no "I heard it" after the chime');
  assert.strictEqual(doc.activeElement, heard(), 'focus did not move to "I heard it"');
  assert(!help.classList.contains('show'), '"Didn\'t hear it?" opened before they had answered');

  // Played again without answering: now the help.
  b.dom.$('spkTestBtn').click();
  await b.clock.advance(10);
  assert(help.classList.contains('show'), 'a second play did not bring up the help');
  assert.strictEqual(doc.activeElement, heard());

  heard().click();
  assert.strictEqual(b.dom.$('spkStatus').textContent, 'Sound works');
  assert(!help.classList.contains('show'));
  assert.strictEqual(doc.activeElement, b.dom.$('spkTestBtn'),
    'focus went with the button that was pressed and removed');
"""


def test_i_heard_it_takes_focus_and_the_help_waits_for_a_second_play(tmp_path):
    _run(tmp_path, SPEAKER_CHECK, "SPEAKER CHECK OK")


# =========================================================================== #
# A11Y-07. "SPEAKING" was hidden with opacity alone, so it stayed in the
# accessibility tree on every tile: a screen reader read "YOU | You | SPEAKING"
# and "M | Morgan | your manager | SPEAKING" with only Morgan talking. The badge
# is out of the tree unless its tile is speaking, and the initials, which only
# repeat the name under them, are hidden from it.
# =========================================================================== #

def test_the_speaking_badge_is_only_there_for_the_tile_that_is_speaking():
    src = V2.read_text(encoding="utf-8")
    assert "visibility: hidden" in _rule(src, ".speaking-indicator")
    assert "visibility: visible" in _rule(src, ".tile.speaking .speaking-indicator")
    assert '<div class="avatar self" aria-hidden="true">YOU</div>' in src
    make = src[src.index("function makeInitials("):src.index("// Characters in the current interaction")]
    grid = src[src.index("function renderGrid("):src.index("// Bind the captured webcam stream")]
    for body in (make, grid):
        assert "setAttribute('aria-hidden', 'true')" in body, body[:80]


# =========================================================================== #
# A11Y-12. The rest of the low-contrast text: the between-encounter step list's
# "To come" (2.82:1), the build tag faded to 2.91:1, the one disabled look's
# muted label on grey (3.79:1; the locked End is the control people look for
# at 7:00), and the audio check's mic meter against its track (2.30:1).
# =========================================================================== #

def _contrast(a: str, b: str) -> float:
    def lum(h):
        rgb = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        rgb = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_remaining_small_print_is_readable():
    src = V2.read_text(encoding="utf-8")
    token = lambda name: re.search(r"--%s:\s*(#[0-9a-f]{6})" % name, src).group(1)
    colour = lambda rule, prop="color": re.search(
        r"(?<![-\w])%s:\s*(#[0-9a-f]{6}|var\(--[\w-]+\))" % prop, rule).group(1)
    value = lambda c: token(c[6:-1]) if c.startswith("var(") else c

    todo = value(colour(_rule(src, "#nextBody .run-steps li.todo")))
    assert _contrast(todo, "#ffffff") >= 4.5, todo

    tag = _rule(src, ".build-tag")
    assert "opacity" not in tag, tag
    assert _contrast(value(colour(tag)), token("bg")) >= 4.5

    disabled = _rule(src, "button:disabled, button#stopBtn.locked, .tile .talk-btn[disabled]")
    assert _contrast(value(colour(disabled)), value(colour(disabled, "background"))) >= 4.5, disabled

    fill = value(colour(_rule(src, ".meter > i"), "background"))
    track = value(colour(_rule(src, ".meter"), "background"))
    assert _contrast(fill, track) >= 3, (fill, track)


# =========================================================================== #
# FLOW-09 (UX audit, 2026-09-28). The audio check said "about 10 minutes at a
# time" and the ring's tooltip "at least 7 minutes", both typed in, while the
# first screen was filled from the clock (7 and 12). Both are the clock's now,
# so a run whose timing is changed says the new numbers everywhere.
# =========================================================================== #

CLOCK_COPY = RUN_VIEW + r"""
  // RUN_VIEW's stop is 13:00, so only copy that reads the clock gets it right.
  const b = boot('?run=r_1&participant_id=p_rec', studyRoutes);
  const text = (id) => b.dom.$(id).textContent;
  await b.clock.advance(50);
  assert.deepStrictEqual([text('fictionMin'), text('fictionMax')], ['7', '13']);
  b.dom.$('fictionAck').click(); await b.clock.advance(50);
  assert(shown(b, 'audioCheckOverlay'));
  assert.deepStrictEqual([text('checkMin'), text('checkMax')], ['7', '13'],
    'the audio check does not say what the first screen says');

  // Shown again without the first screen ("Check my audio again"), after the
  // clock has moved: its own numbers follow.
  b.dom.$('audioCheckSkip').click(); await b.clock.advance(50);
  b.set('MIN_S = 480; MAX_S = 900;');
  b.ctx.runAudioCheck(); await b.clock.advance(10);
  assert.deepStrictEqual([text('checkMin'), text('checkMax')], ['8', '15']);
  b.dom.$('audioCheckSkip').click(); await b.clock.advance(10);
  b.set('MIN_S = 420; MAX_S = 780;');

  // The ring's tooltip, from the floor, and again when the runner resets it.
  b.dom.$('situationStart').click(); await b.clock.advance(1000);
  assert.strictEqual(b.set('started'), true);
  assert(/^Each conversation runs at least 7 minutes; End unlocks then\./.test(b.dom.$('gate').title),
    b.dom.$('gate').title);
  b.ctx.handleServerFrame({ data: JSON.stringify(
    { type: 'encounter_clock', min_seconds: 540, wrap_seconds: 720, max_seconds: 780 }) });
  await b.clock.advance(1000);
  assert(/at least 9 minutes/.test(b.dom.$('gate').title), b.dom.$('gate').title);
"""


def test_the_audio_check_and_the_ring_say_the_clock_s_minutes(tmp_path):
    _run(tmp_path, CLOCK_COPY, "CLOCK COPY OK")


def test_no_duration_is_typed_into_the_audio_check_or_the_ring():
    src = V2.read_text(encoding="utf-8")
    check = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", _overlay(src, "audioCheckOverlay")))
    assert "about 10 minutes" not in check, check
    assert "7 to 12 minutes at a time" in check, check
    assert "title" not in _attrs(_open_tag(src[:src.index("<script>")], "gate")), (
        "the ring's tooltip is typed into the markup again")


# =========================================================================== #
# FLOW-07. Three messages pointed at things that are not there: "Something
# went wrong. Please try again." with nothing to retry, "If you stop hearing
# the other people, click Reconnect." with no Reconnect on screen (and nobody
# but one other person in 1:1), and a card that said "use the contact below"
# with no contact on it.
# =========================================================================== #

ERROR_COPY = RUN_VIEW + r"""
  const lastNote = (b) => notes(b).pop().textContent;
  {
    const b = boot('?run=r_1&participant_id=p_rec', studyRoutes);
    await b.clock.advance(50);
    b.dom.$('fictionAck').click(); await b.clock.advance(50);
    b.dom.$('audioCheckSkip').click(); await b.clock.advance(50);
    b.dom.$('situationStart').click(); await b.clock.advance(50);
    assert.strictEqual(b.set('started'), true);

    b.ctx.handleServerFrame({ data: JSON.stringify({ type: 'error', message: 'The transcription channel was lost.' }) });
    assert.strictEqual(lastNote(b), 'There was a problem on our side. You can keep talking; if nobody '
      + 'answers you, reload this page to start this conversation again.');

    // A frame the page cannot handle, through the socket's own listener.
    b.set('ws').listeners.message[0]({ data: '{not json' });
    assert.strictEqual(lastNote(b), 'Something went wrong on this page. If you stop hearing anyone, '
      + 'reload the page to start this conversation again.');
    assert(!notes(b).some(n => /Reconnect|other people|try again\./.test(n.textContent)),
      JSON.stringify(notes(b).map(n => n.textContent)));
  }

  // The participant record cannot be made: the card says to use the contact
  // below, so the contact is below.
  {
    const b = boot('?run=r_1', (b) => [
      { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '', contact_name: 'Dr Rivera',
                                                              contact_email: 'rf@example.edu' }) },
      { match: '/api/run/r_1', fn: () => b.net.res(200, RUN_VIEW) },
      { match: '/api/scenarios/S4A', fn: () => b.net.res(200, BRIEF) },
      { match: '/api/participant', fn: () => b.net.res(503, {}) },
    ]);
    await b.clock.advance(50);
    b.dom.$('fictionAck').click(); await b.clock.advance(50);
    assert(shown(b, 'errOverlay'), 'no blocking card for a record that could not be made');
    const body = b.dom.$('errOverlay').querySelector('#errBody');
    assert(/use the contact below/.test(body.textContent), body.textContent);
    const contact = body.children.map(c => c.innerHTML).join(' ');
    assert(/Contact Dr Rivera at .*rf@example\.edu/.test(contact), 'no contact below: ' + contact);
  }
"""


def test_error_messages_point_at_what_is_there(tmp_path):
    _run(tmp_path, ERROR_COPY, "ERROR COPY OK")


# =========================================================================== #
# FLOW-11. One unit had four names on the participant's screens: "encounter"
# (the chip, End, the cards between), "conversation" (the first screen, Start,
# the last card), "part" (the drop card) and "scene" (the scenario's howto,
# which is scenario content and not the page's). The page says "conversation"
# for the unit, and "part" only for a phase inside one.
# =========================================================================== #

UNIT_NAME = RUN_VIEW + r"""
  const b = boot('?run=r_1&participant_id=p_rec', studyRoutes);
  await b.clock.advance(50);
  assert.strictEqual(b.dom.$('runChip').textContent, 'Conversation 1 of 4');
  b.dom.$('fictionAck').click(); await b.clock.advance(50);
  b.dom.$('audioCheckSkip').click(); await b.clock.advance(50);
  b.dom.$('situationStart').click(); await b.clock.advance(50);
  assert.strictEqual(b.dom.$('stopBtn').textContent, 'End conversation (go to conversation 2 of 4)');
  assert.strictEqual(b.ctx.skipDoorLabel(), 'Go on to conversation 2 of 4 without this one');
"""


def test_the_unit_is_a_conversation_on_every_screen(tmp_path):
    _run(tmp_path, UNIT_NAME, "UNIT NAME OK")


def test_no_participant_string_calls_the_unit_an_encounter():
    src = V2.read_text(encoding="utf-8")
    markup = re.sub(r"<!--.*?-->", "", src[:src.index("<script>")], flags=re.S)
    text = re.sub(r"<[^>]+>", " ", markup[markup.index("<body>"):])
    assert not re.search(r"\b[Ee]ncounters?\b", text), re.findall(r".{30}[Ee]ncounter.{30}", text)
    code = [ln for ln in src[src.index("<script>"):].splitlines()
            if not ln.lstrip().startswith(("//", "*", "/*"))]
    said = [s for ln in code for s in re.findall(r"""(['"`])((?:(?!\1).)*)\1""", ln)]
    bad = [s for _, s in said if re.search(r"\b[Ee]ncounters?\b", s)]
    assert not bad, bad
    assert "next part of the study" not in src and "go on to the next part" not in src
