"""The run embedded in a Qualtrics survey (embed=qualtrics, step=1..4).

The survey frames the same /start link once per conversation block and moves on
with its own Next button, shown only when the room page says the block's
conversation is over (docs/qualtrics-embed.md). These tests pin both halves:

  * /start carries the two flags through to the room page, and drops anything
    else in them rather than refusing the participant;
  * the room page, embedded, tells the survey (and only a Qualtrics survey)
    what happened, never starts the next conversation in the same block, never
    navigates to a survey link inside the frame, and never lets the survey's
    Next unload a recording that is still being saved.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import app as appmod

from tests.test_client_blockers import DOM_STUB, _node  # noqa: F401  (the page stub)

import subprocess
from pathlib import Path

V2 = Path(__file__).resolve().parents[1] / "static" / "v2.html"

BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# /start
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("embed, step, want", [
    ("qualtrics", "2", "&embed=qualtrics&step=2"),
    ("Qualtrics", " 4 ", "&embed=qualtrics&step=4"),
    ("qualtrics", None, "&embed=qualtrics"),
    ("qualtrics", "0", "&embed=qualtrics"),
    ("qualtrics", "two", "&embed=qualtrics"),
    ("qualtrics", "99", "&embed=qualtrics"),
    ("somewhere", "2", ""),
    (None, "2", ""),
])
def test_only_the_known_embed_mode_and_a_sane_step_are_carried(embed, step, want):
    assert appmod._embed_query(embed, step) == want


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from server import runs, storage

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


def test_every_block_lands_in_the_same_run_with_its_own_step(client):
    """Four survey pages, one link each: one run, and each page told its step."""
    runs_seen = set()
    for step in ("1", "2", "3", "4"):
        r = client.get("/start", params={"participantId": "RFEMBED01", "qid": "R_abc",
                                         "embed": "qualtrics", "step": step},
                       headers={"User-Agent": BROWSER}, follow_redirects=False)
        assert r.status_code == 307, r.text
        loc = r.headers["location"]
        assert loc.startswith("/v2?run="), loc
        assert loc.endswith(f"&embed=qualtrics&step={step}"), loc
        runs_seen.add(loc.split("run=")[1].split("&")[0])
    assert len(runs_seen) == 1, runs_seen


def test_a_plain_entry_link_is_unchanged(client):
    r = client.get("/start", params={"participantId": "RFEMBED02"},
                   headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert "embed=" not in r.headers["location"] and "step=" not in r.headers["location"]


def test_the_continue_button_keeps_the_flags(client):
    """A framed link that lost its key gets the entry check page; its Continue
    posts back to the same URL, so the flags must survive that hop too."""
    r = client.post("/start", params={"participantId": "RFEMBED03", "embed": "qualtrics",
                                      "step": "3"},
                    headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("&embed=qualtrics&step=3")


# --------------------------------------------------------------------------- #
# The room page
# --------------------------------------------------------------------------- #

PAGE_HARNESS = r"""'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');
const PAGE = process.argv[2];
const set = (b, code) => vm.runInContext(code, b.ctx);
const $ = (b, id) => b.dom.document.getElementById(id);
const vis = (el) => el.style.display && el.style.display !== 'none';

const AFTER = (pos, done) => ({ done, position: pos, total: 4, completed: [], run_id: 'r_1',
  completion_code: 'CODE9', participant_id: 'p_1', current: done ? null : { id: 'S3A' } });

// The page as the survey frames it, the instant a conversation ends.
function framed(search, parentOrigin, advanced, uploadJs) {
  const b = bootV2(PAGE, search);
  const sent = [];
  let gone = null;
  b.sandbox.location = { search, hostname: 'app.example', get href() { return ''; },
                         set href(v) { gone = v; }, reload() {},
                         ancestorOrigins: parentOrigin ? [parentOrigin] : [] };
  b.sandbox.parent = { postMessage(m, o) { sent.push({ m, o }); } };
  b.sandbox.URL = URL;   // every browser has it; the stub does not
  b.net.route([
    { match: '/advance', fn: () => b.net.res(200, advanced) },
    { match: '/api/run/config', fn: () => b.net.res(200, { return_url: 'https://survey.invalid/x' }) },
  ]);
  set(b, "sessionId = 's_x'; advancing = false; scenario = 'S1A'; " + (uploadJs || ''));
  return { b, sent, gone: () => gone };
}

(async () => {
  const Q = 'https://cornell.qualtrics.com';
  const SEARCH = '?run=r_1&embed=qualtrics&step=1';

  // --- a block's conversation ends: the survey is told, nothing else moves --
  {
    const p = framed(SEARCH, Q, AFTER(2, false));
    p.b.ctx.onEncounterComplete();
    await p.b.clock.advance(1000);
    assert(/Conversation 1 of 4 complete/.test($(p.b, 'nextTitle').textContent), $(p.b, 'nextTitle').textContent);
    assert(/Press <b>Next<\/b> below this window/.test($(p.b, 'nextBody').innerHTML), 'no word about Next');
    assert(!vis($(p.b, 'nextBtn')), 'the page offers to start the next conversation itself');
    assert.strictEqual(p.gone(), null, 'the frame navigated: ' + p.gone());
    assert.strictEqual(p.sent.length, 1, JSON.stringify(p.sent));
    const { m, o } = p.sent[0];
    assert.strictEqual(o, Q, 'the message was not addressed to the survey only');
    assert.deepStrictEqual([m.source, m.type, m.run, m.step, m.position, m.total, m.scenario],
      ['relational-fluency', 'conversation_complete', 'r_1', 1, 1, 4, 'S1A']);
  }

  // --- a recording still uploading holds the message, with no way past it --
  {
    let land = null;
    const p = framed(SEARCH, Q, AFTER(2, false),
      "videoUpload = new Promise(r => { __land = r; });");
    set(p.b, "liveUpload = { promise: videoUpload, settled: false, result: null };" +
             "videoUpload.then(v => { liveUpload.settled = true; liveUpload.result = v; });");
    land = () => vm.runInContext('__land', p.b.ctx)({ ok: true });
    p.b.ctx.onEncounterComplete();
    await p.b.clock.advance(1000);
    assert(/Saving your recording/.test($(p.b, 'nextTitle').textContent), 'no saving screen');
    assert(!vis($(p.b, 'nextBtn')), '"Continue without waiting" is offered inside the survey');
    assert.strictEqual(p.sent.length, 0, 'the survey was told before the recording was saved');
    land();
    await p.b.clock.advance(100);
    assert.strictEqual(p.sent.length, 1, 'the survey was never told');
    assert.strictEqual(p.sent[0].m.type, 'conversation_complete');
  }

  // --- the last conversation: the code goes to the survey ------------------
  {
    const p = framed('?run=r_1&embed=qualtrics&step=4', Q, AFTER(5, true));
    p.b.ctx.onEncounterComplete();
    await p.b.clock.advance(1000);
    assert(/All conversations complete/.test($(p.b, 'nextTitle').textContent));
    assert(/CODE9/.test($(p.b, 'nextBody').innerHTML), 'the code is not on screen');
    assert(!/Go back to the Qualtrics survey now/.test($(p.b, 'nextBody').innerHTML),
      'the participant is told to leave a survey they are inside');
    assert(!vis($(p.b, 'nextBtn')), 'a return-to-survey button inside the survey');
    assert.strictEqual(p.gone(), null);
    const last = p.sent[p.sent.length - 1].m;
    assert.deepStrictEqual([last.type, last.code, last.step], ['run_complete', 'CODE9', 4]);
  }

  // --- any other parent hears nothing --------------------------------------
  for (const other of ['https://evil.example', 'http://cornell.qualtrics.com', 'https://qualtrics.com.evil.example']) {
    const p = framed(SEARCH, other, AFTER(2, false));
    p.b.ctx.onEncounterComplete();
    await p.b.clock.advance(1000);
    assert.strictEqual(p.sent.length, 0, other + ' was sent ' + JSON.stringify(p.sent));
  }

  // --- not embedded: the run goes on as it always did ----------------------
  {
    const p = framed('?run=r_1', Q, AFTER(2, false));
    p.b.ctx.onEncounterComplete();
    await p.b.clock.advance(1000);
    assert.strictEqual(p.sent.length, 0, 'a plain run link talks to its parent');
    assert(/Start conversation 2 of 4/.test($(p.b, 'nextBtn').textContent), $(p.b, 'nextBtn').textContent);
  }

  // --- a reloaded block whose conversation is already done -----------------
  {
    const p = framed('?run=r_1&embed=qualtrics&step=2', Q, AFTER(3, false));
    p.b.net.route([{ match: '/api/run/r_1', fn: () => p.b.net.res(200, AFTER(3, false)) }]);
    const goOn = await p.b.ctx.loadRun();
    assert.strictEqual(goOn, false, 'the next conversation was loaded into this block');
    assert(/Conversation 2 of 4 is done/.test($(p.b, 'nextTitle').textContent), $(p.b, 'nextTitle').textContent);
    const m = p.sent[p.sent.length - 1].m;
    assert.deepStrictEqual([m.type, m.position, m.already], ['conversation_complete', 2, true]);
  }

  console.log('EMBED OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def test_the_room_page_hands_each_block_back_to_the_survey(tmp_path):
    import shutil
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    (tmp_path / "stub.js").write_text(DOM_STUB, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(PAGE_HARNESS, encoding="utf-8")
    proc = subprocess.run([shutil.which("node"), str(harness), str(V2)],
                          capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "EMBED OK" in proc.stdout
