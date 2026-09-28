"""What the last screens tell a participant about the survey and their code.

PR #36 added a line to the stop/decline/fault card, the branch shown when no
return link is configured (production today):

    '<p>You must return to the Qualtrics survey now and enter the code
     <b class="code">${RF-COMPLETE}</b> ...'

Inside a single-quoted string ``${...}`` is not substituted, so the page
printed the characters "${RF-COMPLETE}" as the code. It was also the wrong
card: finishing the run goes through showRunComplete, and showClosing is the
exit that, since 2026-09-23, hands out no code. The real code is per run
(server/runs.py completion_code, "RF-" + 8 hex).

These tests paint both cards with the page's own functions, the way the other
page harnesses do, against the production config (no return_url).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_client_blockers import V2, _run  # noqa: E402


CLOSING_HARNESS = r"""'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
const set = (b, code) => vm.runInContext(code, b.ctx);
const $ = (b, id) => b.dom.document.getElementById(id);
const vis = (el) => el.style.display && el.style.display !== 'none';

const RUN = { done: true, position: 5, total: 4, completed: [1, 2, 3, 4], run_id: 'r_1',
              completion_code: 'RF-AB12CD34', participant_id: 'p_test', current: null };

// Production: /api/run/config has no return_url, so there is no button and
// the text is the only thing sending them back.
function booted() {
  const b = bootV2(PAGE, '?run=r_1');
  b.net.route([
    { match: '/api/run/config', fn: () => b.net.res(200, {
        return_url: '', return_label: 'Return to the survey', contact_name: '', contact_email: '' }) },
  ]);
  set(b, 'run = ' + JSON.stringify(RUN) + ';');
  return b;
}

(async () => {
  // --- finishing: the real code, and where to take it --------------------
  {
    const b = booted();
    b.ctx.showRunComplete();
    await b.clock.advance(500);
    const body = $(b, 'nextBody').innerHTML;
    assert(/RF-AB12CD34/.test(body), 'the finished screen does not show the run\'s code: ' + body);
    assert(/Qualtrics survey/.test(body), 'the finished screen does not send them back to Qualtrics: ' + body);
    assert(!/\$\{/.test(body), 'an unfilled placeholder reached the page: ' + body);
    assert(!vis($(b, 'nextBtn')), 'a return button with no return link');
  }

  // --- stopping between encounters: no code, still the way back ----------
  {
    const b = booted();
    b.ctx.showClosing('Finishing here', '<p>bye</p>', '');
    await b.clock.advance(500);
    const body = $(b, 'nextBody').innerHTML;
    assert(!/\$\{/.test(body), 'an unfilled placeholder reached the page: ' + body);
    assert(!/enter the code|RF-/.test(body), 'the no-code exit told them to enter a code: ' + body);
    assert(/Qualtrics survey/.test(body), 'the closing card does not send them back to Qualtrics: ' + body);
  }

  // --- withdrawing mid-run: the partial code the run really has ----------
  {
    const b = booted();
    b.ctx.showClosing('You have stopped the study', '<p>bye</p>', 'RF-PARTIAL-0A1B2C3D');
    await b.clock.advance(500);
    const body = $(b, 'nextBody').innerHTML;
    assert(/RF-PARTIAL-0A1B2C3D/.test(body), 'the partial code is missing: ' + body);
    assert(!/\$\{/.test(body), 'an unfilled placeholder reached the page: ' + body);
  }

  console.log('CLOSING OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def test_the_last_screens_show_the_real_code_and_send_them_to_qualtrics(tmp_path):
    assert "CLOSING OK" in _run(tmp_path, CLOSING_HARNESS, V2)


def test_no_html_fragment_in_a_plain_string_carries_a_placeholder():
    """``'<p>...${x}...</p>'`` prints "${x}" verbatim; only a backtick
    template fills it. Checked over the whole participant page, not only the
    closing cards, because this is an easy slip anywhere HTML is concatenated."""
    src = V2.read_text(encoding="utf-8")
    bad = [src.count("\n", 0, m.start()) + 1
           for m in re.finditer(r"'<[^'\n`]*\$\{[^'\n`]*'", src)]
    assert not bad, f"static/v2.html lines {bad}: ${{...}} inside a single-quoted string"
