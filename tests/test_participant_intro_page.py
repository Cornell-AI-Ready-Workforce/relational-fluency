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
