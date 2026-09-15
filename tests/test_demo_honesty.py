"""What the demo screens tell the room, held to what the server actually does.

static/demo.html is the one page in this repository whose purpose is to be
projected onto a wall in front of a research lab. Everything on it is a claim
made out loud to colleagues, and a claim that is not true is worse than no claim
at all: the room cannot check it, the researcher cannot take it back, and the
part of the platform it was supposed to reassure them about is the part they now
have a reason to doubt.

tests/test_demo_door.py holds that page to being a demo door — one entrance, an
honest consent record, no failing in front of an audience. This file holds the
three screens the demo actually puts on the wall — the door, the evidence trace
it sends the room to, and the researcher console it links — to saying only what
is true of THIS tree. Six claims were reproduced false or unkept before it:

1. "It is a genuine encounter in every way except one: it can never be study
   data." A guarantee, restated from a previous round, about a mechanism that
   has since been rebuilt. The mechanism is real now (tests/test_cohort_integrity
   .py pins it: a demo's cohort is decided when /test mints the run, bound onto
   the participant record, and not re-derivable by a later run) — but "never" is
   not what the code promises, because an explicit session_ids list handed to
   POST /api/rating/assign never consults a cohort at all. The page has to say
   the mechanism it has, not the absolute it does not.

2. The printed containment map was wrong on two of the three surfaces it named.
   `study` in /api/encounters does not mean "study cohort"; it means load_spec
   found a v3 scenario, and a demo draws v3 scenarios, so an internal encounter
   reports study=true. And the researcher console said nothing about cohort at
   all: the internal encounter was the first row of the session picker, rendered
   exactly like the 26 study rows under it.

3. The evidence trace painted its STUDY badge from the same `spec`, so the
   screen demo.html sends the room to badged a demo STUDY.

4. The key. demo.html strips the researcher key from its own address bar and
   says so in a banner, then handed it straight to pages that never stripped it.
   A key on a wall in front of a room has been given to the room, and stripping
   it from one page of four is not stripping it.

5. The live button stayed enabled with /health reporting the gateway down —
   with the row directly above it reading "A live encounter will fail. Use the
   replay lane." That is the exact failure the page exists to prevent, arrived
   at by the page's own preflight.

6. The cohort mark in the encounter picker was " · internal" appended to a
   title, and S4A is titled "Planning an internal rollout" — four of the demo
   wave's 27 encounters. Five rows with the word "internal" in them, one of
   them internal. (The page and this docstring both used to say four of the
   eight SCENARIOS carried that title; the v3 titles are all different strings
   — eight of them then, ten since S1 C and S3 C. The count that makes five
   rows is the encounter count.)

A close-out round then found six more, all of them about what the preflight
strip says and what the buttons under it do:

7. A WRONG key got the green tick. The row was `keyed && !KEY` — present, not
   working — and /health cannot answer the other question. The live lane stayed
   armed over a /test that answers 401, while the replay lane, behind the same
   check_key, was the one switched off.

8. A /health that never answered left the live lane armed and the steering card
   hidden, permanently: `fetch` has no timeout, the live buttons carried no
   `disabled` in the markup, and a check that never completes was being read as
   a pass.

9. The gateway gate latches for the life of the process — /health serves the
   boot-time preflight — and the page's remedy was "re-check the server".

10. "Neither lane will work until it can" over a replay lane that does not read
    /health, contradicted by the page's own note two sections down.

11. An unset SESSION_KEY printed as a green tick, with a claim about who can
    reach the server that the page has no way to make.

12. An encounter with no cohort at all got no mark, which the picker's own
    comment said it would.

Most of what follows drives the real pages in node, through the same browser
stub the console harnesses use, because every one of these defects was a
question about what the screen SAYS and not about what the file contains.

Run from the repo root:

    python -m pytest tests/test_demo_honesty.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server import storage

# One browser stub for the repository, and this is deliberately the console
# harnesses' own: a page that answers these questions under it answers
# tests/test_final_console.py's under the same DOM, so the two files cannot
# disagree about what "the page rendered" means.
from test_final_console import CONSOLE_STUB_JS

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
DEMO = STATIC / "demo.html"
EVIDENCE = STATIC / "evidence.html"
RESEARCHER = STATIC / "researcher.html"


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def _run_harness(tmp_path, page, harness_src, name, *args):
    (tmp_path / "stub.js").write_text(CONSOLE_STUB_JS, encoding="utf-8")
    harness = tmp_path / name
    harness.write_text(harness_src, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(page), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _demo() -> str:
    return DEMO.read_text(encoding="utf-8")


def _visible(src: str) -> str:
    """The page's markup with the HTML comments taken out.

    The questions below are about what a reader in the room sees. This page
    explains its own rules at length in comments — including the rules it used
    to break — and a test reading the raw file cannot tell the explanation from
    the claim. tests/test_demo_door.py strips comments for the same reason.
    """
    out, i = [], 0
    while True:
        j = src.find("<!--", i)
        if j < 0:
            out.append(src[i:])
            return "".join(out)
        out.append(src[i:j])
        k = src.find("-->", j)
        if k < 0:
            return "".join(out)
        i = k + 3


def _prose(src: str) -> str:
    """Just the markup, without the script and without the comments: the words
    that are on the screen before a line of JavaScript runs."""
    body = _visible(src)
    i = body.find("<script>")
    return body[:i] if i > 0 else body


# =============================================================================
# 1. The central claim, and the surfaces it is about
# =============================================================================

@pytest.fixture()
def store(tmp_path, monkeypatch):
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    storage.init_storage()
    return storage


@pytest.fixture()
def open_client(store, monkeypatch):
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


def _record_encounter(store, session_id: str, cohort: str, scenario: str = "S4A"):
    """One finished encounter on disk, tagged the way a real one is."""
    d = store.SESSIONS_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "session_id": session_id, "scenario": scenario, "cohort": cohort,
        "participant_id": f"p_{session_id}", "run_id": f"r_{session_id}",
        "participant_key": "test_demo-lab_1", "encounter_index": 1,
        "started_at": time.time(), "status": "closed", "n_turns": 7,
    }), encoding="utf-8")
    return d


def test_the_api_reports_a_demo_encounter_as_study_true(open_client, store):
    """A PIN, not a repair, and the reason claim 2 was false.

    `study` on an /api/encounters row is the answer to "did load_spec find a v3
    scenario for this", which is a statement about the instrument and not about
    the dataset. A demo draws from the same v3 bank a participant does — that
    is the point of a demo — so an internal encounter reports study=true, and a
    page that tells a room "a demo appears here only with study: false" is
    telling them to read the one field that cannot answer the question.

    Reproduced on the demo wave first: s_1773142745_384dad, cohort internal,
    study true.
    """
    _record_encounter(store, "s_demo_1", "internal")
    rows = open_client.get("/api/encounters").json()
    row = next(r for r in rows if r["id"] == "s_demo_1")
    assert row["cohort"] == "internal"
    assert row["study"] is True, (
        "the `study` flag has started meaning cohort; if that is deliberate, "
        "static/demo.html's containment map has to be rewritten again")


def test_the_cohort_filters_are_what_actually_drop_a_demo(open_client, store):
    """The other half of the pin, and what the page's replacement sentence
    claims: a cohort filter is the thing that excludes a demo, on every list
    that offers one."""
    _record_encounter(store, "s_demo_2", "internal")
    _record_encounter(store, "s_study_2", "study")
    study = open_client.get("/api/encounters", params={"cohort": "study"}).json()
    assert [r["id"] for r in study] == ["s_study_2"]
    internal = open_client.get("/api/encounters",
                               params={"cohort": "internal"}).json()
    assert [r["id"] for r in internal] == ["s_demo_2"]
    unfiltered = {r["id"] for r in open_client.get("/api/encounters").json()}
    assert {"s_demo_2", "s_study_2"} <= unfiltered, (
        "an unfiltered list no longer carries the demo, which would make the "
        "page's 'excluded, not hidden' wording wrong in the other direction")


def test_the_live_lane_no_longer_promises_what_it_cannot_keep():
    """The sentence itself.

    "It can never be study data" is an absolute about a system whose cohort
    tag is a field in a file and whose rater assignment accepts an explicit
    list of session ids that consults no cohort at all. What the code does
    guarantee is narrower and worth saying: the tag is chosen before the
    encounter exists and cannot be re-derived afterwards.
    """
    prose = _prose(_demo())
    assert "it can never be study data" not in prose.lower(), (
        "the live lane still makes the unqualified guarantee that was false "
        "when it was written and is still not what the code promises")
    lane = _lane_two(prose)
    for phrase in ("cohort=internal", "?cohort=study"):
        assert phrase in lane, (
            f"the live lane does not name {phrase}, so it states no mechanism "
            "in place of the guarantee it dropped")
    assert "before" in lane.lower() and "participant record" in lane.lower(), (
        "the live lane does not say that the cohort is settled before the "
        "encounter starts and bound to the participant record, which is the "
        "only reason any of the rest of it holds")


# =============================================================================
# 2. The containment map
# =============================================================================

def test_the_containment_map_does_not_send_anyone_to_the_study_flag():
    """Two of the three surfaces it named were wrong; this is the first."""
    m = _map(_prose(_demo()))
    claims = [s for s in _sentences(m)
              if "study: false" in s and "study: true" not in s]
    assert not claims, (
        "the map still tells the room that a demo reads study: false; "
        "s_1773142745_384dad is cohort internal and study: true. The only "
        "honest mention of the flag is one that gives both of its values and "
        "says what it is really answering: " + str(claims))
    assert "study: true" in m and "v3" in m, (
        "the map does not warn that /api/encounters' `study` flag says only "
        "whether the scenario is a v3 study scenario, so the next reader makes "
        "the same mistake")
    assert "cohort" in m


def test_the_containment_map_names_the_screens_a_demo_appears_on():
    """The second wrong surface, and the honest version of it.

    A demo is not hidden anywhere. It is a row in the console's session picker
    and in the unfiltered /api/encounters, usually the newest one, which is the
    top. The map has to say that and say where the mark is, because "where a
    demo shows up afterwards" is precisely the question a colleague asks.
    """
    m = _map(_prose(_demo()))
    low = m.lower()
    assert "evidence trace" in low, (
        "the map does not mention the evidence trace, which is the screen this "
        "page's own button opens and the one that badged a demo STUDY")
    assert "session picker" in low or "session list" in low, (
        "the map does not say a demo is a row in the console's session list "
        "beside the study's")


def test_the_two_marks_that_belong_to_this_page_are_not_claimed_of_every_demo(
        open_client, store):
    """"In the participant store the consent record reads consent_source:
    "internal_test", and the run's participant key begins test_demo-."

    Both true of a demo started from THIS page and neither true of an internal
    encounter as such — and the comment above the map claimed every line of it
    had been checked against one internal encounter on this tree. Checked:
    the demo wave's s_1773142745_384dad was started by GET /test?name=bjordan
    before this page existed. Its participant key is test_bjordan_1773142745,
    not test_demo-anything, and its participant record carries no
    consent_source field at all. Two of the lines the comment vouched for were
    false against the record it named.

    The mechanism is pinned here rather than the wave, since the wave is not on
    the test path: /test mints an internal run under whatever name it is given,
    and the two marks come from this page forcing a prefix and from the /v2
    handoff, not from the cohort.
    """
    from server import runs as runsmod

    r = open_client.get("/test", params={"name": "bjordan"},
                        follow_redirects=False)
    assert r.status_code == 307, r.text
    rows = open_client.get("/api/runs", params={"cohort": "internal"}).json()
    assert rows, "GET /test did not mint an internal run"
    row = rows[-1]
    assert row["cohort"] == "internal"
    assert not str(row["participant_id"]).startswith("test_demo-"), (
        "the test_demo- prefix has become a property of the /test entrance; if "
        "that is deliberate the page can widen the claim again")
    run = json.loads(
        (runsmod.RUNS_DIR / f"{row['run_id']}.json").read_text(encoding="utf-8"))
    record = json.loads(
        (store.PARTICIPANTS_DIR / f"{run['participant_record_id']}.json")
        .read_text(encoding="utf-8"))
    assert "consent_source" not in record or record["consent_source"] is None, (
        "an internal run now carries consent_source before any consent has "
        "been recorded; the page's scoping of that claim needs re-reading")

    # So the page must scope both marks to a demo started from it.
    m = _map(_prose(_demo()))
    claims = [s for s in _sentences(m)
              if "internal_test" in s or "test_demo-" in s]
    assert claims, "the map no longer mentions either mark at all"
    assert any("this page" in s for s in claims), (
        "the map states the consent_source and test_demo- marks without saying "
        "they belong to a demo started from this page: " + str(claims))
    src = _demo()
    assert "Every line of this was checked against" not in src, (
        "the comment above the map still vouches for every line of it against "
        "one encounter, and two of those lines are false against that "
        "encounter")


# =============================================================================
# 3. The screens the demo opens: they have to mark a demo themselves
# =============================================================================

EVIDENCE_HARNESS = r"""/* Drives static/evidence.html against a wave whose newest encounter is an
   internal one — the shape of the demo wave, where s_1773142745_384dad is the
   first row and the default selection — and reads what the page painted. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];
const WANT = process.argv[3] || 's_internal';

const ROWS = [
  { id: 's_internal', scenario: 'S3B', cohort: 'internal', status: 'complete',
    started_at: 1773142745, title: 'After a commission cut', variant: 'B', study: true },
  { id: 's_study', scenario: 'S4A', cohort: 'study', status: 'complete',
    started_at: 1772764657, title: 'Planning an internal rollout', variant: 'A', study: true },
  { id: 's_untagged', scenario: 'S1B', cohort: null, status: 'complete',
    started_at: 1772000000, title: 'Hostile after-hours message', variant: 'B', study: true },
];

const REC = {
  scenario: 'S3B',
  spec: { construct: 'inspirational_leadership', variant: 'B',
          title: 'After a commission cut', interactions: [], esci_items: {} },
  provenance: { realtime_model: 'nto.gemini-live-2.5-flash',
                text_model: 'nto.gemini-3.1-flash-lite', gateway: 'https://g' },
  counts: {}, transcript: [], triggers_fired: [], audio: {}, video: [],
};

const replaced = [];

(async () => {
  const b = bootPage(PAGE, [
    { match: '/record', fn: () => ({ ok: true, status: 200, json: async () => REC }) },
    { match: '/api/encounters', fn: () => ({ ok: true, status: 200, json: async () => ROWS }) },
  ], {
    location: { search: '?session=' + WANT + '&key=k7', pathname: '/evidence',
                href: 'http://t/evidence?session=' + WANT + '&key=k7',
                protocol: 'http:', host: 't', reload() {} },
    history: { replaceState(s, t, url) { replaced.push(String(url)); } },
  });
  await b.clock.flush();

  // --- the key must not still be in the bar -------------------------------
  assert(replaced.length,
    'static/evidence.html never rewrote its own address bar, so the researcher key ' +
    'demo.html took out of its own URL is sitting in this one in front of the room');
  const bar = replaced[replaced.length - 1];
  assert(!/[?&]key=/.test(bar),
    'the rewritten address bar still carries the key: ' + bar);
  assert(/session=/.test(bar),
    'the rewrite dropped ?session= as well, so the link the console hands out no ' +
    'longer opens the encounter it names: ' + bar);

  // --- the badge ----------------------------------------------------------
  // Matched on the badge's own TEXT, never on a class name: a rule called
  // .b-internal would satisfy a substring test without a word reaching the
  // screen, which is the shape of the defect this is about.
  const bar2 = b.$('appbar').innerHTML;
  assert(/s_internal/.test(bar2), 'the page did not open the encounter it was asked for');
  assert(!/>\s*Study\s*</i.test(bar2),
    'the evidence trace badges an internal encounter STUDY — this is the screen ' +
    "demo.html's own button opens: " + bar2);
  assert(/>\s*internal\s*</i.test(bar2),
    'nothing in the app bar says this encounter is not study data: ' + bar2);

  // The study encounter must still read as one: a page that badges nothing is
  // not a fix, it is the same absence pointed the other way.
  b.ctx.select('s_study');
  await b.clock.flush();
  const studyBar = b.$('appbar').innerHTML;
  assert(/>\s*study\s*</i.test(studyBar),
    'a study encounter no longer says so: ' + studyBar);
  assert(!/>\s*internal\s*</i.test(studyBar),
    'a study encounter is marked internal: ' + studyBar);

  // An encounter recorded before the cohort tag existed is neither.
  b.ctx.select('s_untagged');
  await b.clock.flush();
  const untagged = b.$('appbar').innerHTML;
  assert(!/>\s*Study\s*</i.test(untagged),
    'an encounter whose manifest carries no cohort at all is badged STUDY: ' + untagged);

  // --- and the row in the list --------------------------------------------
  const list = b.$('sessList').innerHTML;
  const rows = list.split('<button').slice(1);
  assert.strictEqual(rows.length, 3, 'the session list did not render three rows');
  assert(/>\s*internal\s*</i.test(rows[0]),
    'the internal encounter is the first row of the list and nothing in the row ' +
    'says it is not study data: ' + rows[0]);
  assert(!/>\s*internal\s*</i.test(rows[1]),
    'a study row is marked internal: ' + rows[1]);

  console.log('EVIDENCE OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def test_the_evidence_trace_marks_the_cohort_instead_of_the_scenario(tmp_path):
    """The badge came off `spec`, which is the instrument, not the dataset.

    static/evidence.html:302 painted it from `spec ? … : ''` and the file did
    not contain the word cohort anywhere, so /evidence?session=s_1773142745_384dad
    — an internal encounter, and the exact link this page's own button builds —
    read "COMPLETE / scenario v3 / STUDY".
    """
    code, out = _run_harness(tmp_path, EVIDENCE, EVIDENCE_HARNESS, "evidence.js")
    assert code == 0, out
    assert "EVIDENCE OK" in out, out


CONSOLE_HARNESS = r"""/* Drives static/researcher.html's session picker with a wave whose newest
   session is internal, and reads the option text a researcher actually sees. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];

const SESSIONS = [
  { id: 's_internal', title: 'After a commission cut (inspirational leadership, var. B)',
    status: 'closed', turn_count: 7, duration_s: 451, cohort: 'internal' },
  { id: 's_rollout', title: 'Planning an internal rollout (teamwork, var. A)',
    status: 'closed', turn_count: 7, duration_s: 395, cohort: 'study' },
  { id: 's_live', title: 'Hostile after-hours message (conflict management, var. B)',
    status: 'active', turn_count: 2, cohort: 'internal' },
];

const replaced = [];

function optionTexts(node) {
  const out = [];
  (node.children || []).forEach(c => {
    if (c.tagName === 'OPTGROUP') (c.children || []).forEach(o => out.push(o.textContent));
    else out.push(c.textContent);
  });
  return out;
}

(async () => {
  const b = bootPage(PAGE, [
    { match: '/api/sessions', fn: () => ({ ok: true, status: 200, json: async () => SESSIONS }) },
    { match: '/api/', fn: () => ({ ok: true, status: 200, json: async () => [] }) },
    { match: '/', fn: () => ({ ok: true, status: 200, json: async () => [] }) },
  ], {
    location: { search: '?key=k7', pathname: '/researcher',
                href: 'http://t/researcher?key=k7', protocol: 'http:', host: 't',
                reload() {} },
    history: { replaceState(s, t, url) { replaced.push(String(url)); } },
  });
  await b.clock.flush();

  assert(replaced.length,
    'static/researcher.html never rewrote its own address bar, so the researcher key ' +
    'is on the wall for as long as the console is up');
  assert(!/[?&]key=/.test(replaced[replaced.length - 1]),
    'the rewritten address bar still carries the key: ' + replaced[replaced.length - 1]);

  await b.ctx.refreshSessions();
  await b.clock.flush();
  const texts = optionTexts(b.$('sessionPicker'));
  assert(texts.length >= 3, 'the picker did not render the sessions: ' + JSON.stringify(texts));
  const internal = texts.filter(t => /s_internal|s_live/.test(t));
  const study = texts.filter(t => /s_rollout/.test(t));
  assert.strictEqual(internal.length, 2, 'the two internal sessions are not both in the picker');
  assert.strictEqual(study.length, 1, 'the study session is not in the picker');
  internal.forEach(t => assert(/internal/i.test(t),
    'an internal session is rendered identically to a study one, which is how the ' +
    'demo encounter came to be the unmarked first row of the picker: ' + t));
  study.forEach(t => assert(!/internal/i.test(t.replace(/Planning an internal rollout/g, '')),
    'a study session is marked internal: ' + t));

  console.log('CONSOLE COHORT OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def test_the_researcher_console_marks_a_session_that_is_not_study_data(tmp_path):
    """The console said nothing about cohort at all.

    The word did not appear in the file. On the demo wave the internal
    encounter is the newest session, so it is the first row of the picker,
    printed exactly like the 26 study rows beneath it — and demo.html told the
    room it would be marked there.
    """
    code, out = _run_harness(tmp_path, RESEARCHER, CONSOLE_HARNESS, "console.js")
    assert code == 0, out
    assert "CONSOLE COHORT OK" in out, out


# =============================================================================
# 4. The key, and the live button
# =============================================================================

DEMO_HARNESS = r"""/* Drives static/demo.html: the preflight, the picker and every door it opens.
   argv[3] is the /health shape to answer with, argv[4] the ?key= it was opened
   with. */
'use strict';
const assert = require('assert');
const { bootPage } = require('./stub.js');

const PAGE = process.argv[2];
const MODE = process.argv[3];
const SEARCH = process.argv[4] || '';

const HEALTH = {
  'gateway-up':   { status: 'ok', gateway: { ok: true, realtime_model: 'nto.gemini-live-2.5-flash' },
                    storage: { ok: true }, session_key_configured: false,
                    active_sessions: 0 },
  'keyed':        { status: 'ok', gateway: { ok: true, realtime_model: 'nto.gemini-live-2.5-flash' },
                    storage: { ok: true }, session_key_configured: true,
                    active_sessions: 0 },
  'gateway-down': { status: 'ok', gateway: { ok: false, status: 502,
                                             realtime_model: 'nto.gemini-live-2.5-flash' },
                    storage: { ok: true }, session_key_configured: false,
                    active_sessions: 0 },
  'gateway-unchecked': { status: 'ok', gateway: { ok: null, checked: false,
                                                  realtime_model: 'nto.gemini-live-2.5-flash' },
                         storage: { ok: true }, session_key_configured: false,
                         active_sessions: 0 },
};

/* The mode that is not a /health shape at all: a socket that is accepted and
   then never written to. `fetch` neither resolves nor rejects, which is the one
   thing this page's preflight had no answer for — it is not a failure it can
   catch, it is an answer that never arrives, and everything downstream of the
   await waits for it forever. Reproduced against the real page through a proxy
   that holds the connection open; reproduced here so it stays reproduced. */
const HANGS = 'health-hangs';

const ENCOUNTERS = [
  { id: 's_internal', scenario: 'S3B', cohort: 'internal', title: 'After a commission cut', study: true },
  { id: 's_rollout_1', scenario: 'S4A', cohort: 'study', title: 'Planning an internal rollout', study: true },
  { id: 's_rollout_2', scenario: 'S4A', cohort: 'study', title: 'Planning an internal rollout', study: true },
  { id: 's_unattributed', scenario: 'S1B', cohort: 'unattributed', title: 'Hostile after-hours message', study: true },
  // A manifest with no cohort field: recorded before the tag existed, or
  // started outside a run. /api/encounters returns cohort: null for it. Titled
  // the way four of the demo wave's study rows are titled, because that is the
  // combination the picker has to survive.
  { id: 's_no_cohort', scenario: 'S4A', cohort: null, title: 'Planning an internal rollout', study: true },
];

/* One internal run for the stage chrome to READ. The chip and the foot strip
   used to be literal strings asserting a cohort and a consent record; they are
   filled from this row now, so the harness hands the page a row to fill them
   from and checks it was the row and not the string. */
const RUNS = [
  { run_id: 'r_demo1', participant_id: 'test_demo-lab_1789000001',
    cohort: 'internal', created_at: 1789000001 },
];

const opened = [];
const replaced = [];

function optionTexts(node) {
  const out = [];
  (node.children || []).forEach(c => {
    if (c.tagName === 'OPTGROUP')
      (c.children || []).forEach(o => out.push({ group: c.attrs.label || c.label || '', text: o.textContent, value: o.value }));
    else out.push({ group: '', text: c.textContent, value: c.value });
  });
  return out;
}

(async () => {
  const health = HEALTH[MODE];
  // Modelled on check_key rather than invented: an unkeyed server answers
  // everyone, and a keyed one answers only the right key. The page's key row
  // is computed from this route's status now, so a harness that answered 200
  // whatever key it was handed would be testing a question nobody asks.
  const keyed = !!(health && health.session_key_configured === true);
  const goodKey = /[?&]key=k7(&|$)/.test(SEARCH);
  const encountersRefused = keyed && !goodKey;

  const b = bootPage(PAGE, [
    { match: '/health', fn: () => {
        if (MODE === HANGS) return new Promise(() => {});   // never settles
        return health
          ? { ok: true, status: 200, json: async () => health }
          : new Error('gateway unreachable');
      } },
    { match: '/api/encounters', fn: () => encountersRefused
        ? { ok: false, status: 401, json: async () => ({ detail: 'Bad or missing key' }) }
        : { ok: true, status: 200, json: async () => ENCOUNTERS } },
    { match: '/api/runs', fn: () => ({ ok: true, status: 200, json: async () => RUNS }) },
  ], {
    location: { search: SEARCH, pathname: '/static/demo.html',
                href: 'http://t/static/demo.html' + SEARCH,
                protocol: 'http:', host: 't', reload() {} },
    history: { replaceState(s, t, url) { replaced.push(String(url)); } },
    open: (url) => { opened.push(String(url)); return null; },
  });
  // The stub does not read the markup's attributes, so #demoName starts empty
  // where the page ships value="lab". Set it to what the markup says, so
  // demoName() produces the slug the RUNS row above is named for.
  b.$('demoName').value = 'lab';

  const snap = () => ({
    liveDisabled: !!b.$('liveBtn').disabled,
    liveTabDisabled: !!b.$('liveTabBtn').disabled,
    gateNoteHidden: b.$('liveGateNote').hidden !== false,
    gateNote: b.$('liveGateNote').innerHTML || '',
    checks: b.$('checks').innerHTML || '',
    steerHidden: b.$('steerCard').hidden !== false,
    steerTitle: b.$('steerTitle').textContent || '',
    steerBody: b.$('steerBody').innerHTML || '',
    encHint: b.$('encHint').textContent || '',
    encSelectDisabled: !!b.$('encSelect').disabled,
    evidenceDisabled: !!b.$('evidenceBtn').disabled,
  });

  await b.clock.flush();
  // What the page is while a check is still in flight. On every mode but HANGS
  // this is already the settled answer; on HANGS it is the whole question.
  const waiting = snap();

  // Past the cap the page puts on an answer. Nothing is pending on the other
  // modes, so this is a no-op for them.
  await b.clock.advance(20000);

  const out = Object.assign({ mode: MODE }, snap(), {
    waiting: waiting,
    keyTravelHidden: b.$('keyTravelNote').hidden !== false,
    navHome: b.$('navHome').href || '',
    navResearcher: b.$('navResearcher').href || '',
    options: optionTexts(b.$('encSelect')),
    replaced: replaced,
  });

  // The doors that are opened by script rather than by an href.
  b.$('encSelect').value = 's_internal';
  b.$('evidenceBtn').fire('click');
  out.evidenceUrl = opened[opened.length - 1] || '';
  b.$('raterBtn').fire('click');
  out.raterUrl = opened[opened.length - 1] || '';

  /* The stage chrome, in both of its states. Called directly rather than
     through the button: the button is disabled on most of these modes and the
     question here is what the chrome SAYS, not who is allowed to open it. */
  b.ctx.openStage();
  out.stageOnOpen = { cohort: b.$('stageCohort').textContent,
                      foot: b.$('stageFoot').textContent,
                      run: b.$('stageRun').textContent };
  await b.ctx.namePresentRun();
  await b.clock.flush();
  out.stageAfterRead = { cohort: b.$('stageCohort').textContent,
                         foot: b.$('stageFoot').textContent,
                         run: b.$('stageRun').textContent };

  console.log('JSON:' + JSON.stringify(out));
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _drive_demo(tmp_path, mode, search=""):
    code, out = _run_harness(tmp_path, DEMO, DEMO_HARNESS, "demo.js", mode, search)
    assert code == 0, out
    line = next((l for l in out.splitlines() if l.startswith("JSON:")), None)
    assert line, out
    return json.loads(line[len("JSON:"):])


def _rows(checks_html: str) -> list:
    """The preflight strip, one card at a time, as (mark class, name, text)."""
    import re

    out = []
    for card in checks_html.split('<div class="check">')[1:]:
        mark = re.search(r'<div class="mark ([a-z]+)">', card)
        name = re.search(r'<div class="name">(.*?)</div>', card, re.S)
        what = re.search(r'<div class="what">(.*?)</div></div></div>', card, re.S)
        out.append((mark.group(1) if mark else "",
                    name.group(1) if name else "",
                    what.group(1) if what else card))
    return out


def _key_row(checks_html: str):
    """The row about the researcher key, whatever this page is calling it."""
    rows = [r for r in _rows(checks_html) if "esearcher key" in r[1]]
    assert len(rows) == 1, (
        "the preflight strip has no single row about the researcher key, so "
        "this test cannot tell the room what it was told: " + checks_html)
    return rows[0]


def _open_tag(src: str, element_id: str) -> str:
    """The opening tag carrying id="…", straight out of the markup.

    Asked of the file and not of the harness on purpose: the stub builds its
    nodes from ids alone and never reads an attribute, so `disabled` in the
    markup is invisible to it — and `disabled` in the markup is exactly the
    half of this that covers the window between the page painting and a line of
    script running.
    """
    i = src.index(f'id="{element_id}"')
    start = src.rindex("<", 0, i)
    return src[start:src.index(">", i) + 1]


#: Which of this repository's own pages take the researcher key out of their
#: address bar. Read from the files rather than listed by hand, so a page that
#: loses its strip is caught by the destination test below rather than by
#: nobody.
def _strips_its_own_bar(page: Path) -> bool:
    src = page.read_text(encoding="utf-8")
    return "replaceState" in src and "delete('key')" in src


def _sentences(prose: str) -> list:
    """The page's visible text, one sentence at a time.

    Asked of the whole page, "does it warn about the landing page" is answered
    yes by a nav link reading "Participant landing page" fifteen lines away from
    a banner reading "Researcher key removed from the address bar" — two true
    statements that together warn nobody of anything. The warning has to be one
    sentence a person reads as one thought.
    """
    text, depth, buf = [], 0, []
    for ch in prose:
        if ch == "<":
            depth += 1
            buf.append(" ")
        elif ch == ">":
            depth -= 1
        elif depth <= 0:
            buf.append(ch)
    flat = " ".join("".join(buf).split())
    return [s.strip().lower() for s in flat.split(".") if s.strip()]


#: Where the live lane ends and the containment map begins. Anchored on the
#: callout's own class rather than on its heading, because the lane's prose
#: points the reader AT that heading by name and a text search finds the
#: pointer first.
_MAP_ANCHOR = 'class="callout quiet"'


def _lane_two(prose: str) -> str:
    """The live lane's own prose: what it says in sentences, before the list of
    marks and before the steering card.

    Ends at the marks list rather than at the map, because the card's heading is
    the literal word "Steering" and an empty card would otherwise answer "does
    this lane mention steering" yes.
    """
    lane = prose[prose.index("Lane 2 · the real thing"):prose.index(_MAP_ANCHOR)]
    return lane[:lane.index('<ul class="marks">')]


def _map(prose: str) -> str:
    return prose[prose.index(_MAP_ANCHOR):]


def _warns_the_landing_page_keeps_the_key(prose: str) -> bool:
    """One sentence that names the page and says what it does with the key."""
    return any("landing page" in s and ("address bar" in s or "keeps the key" in s)
               for s in _sentences(prose))


def test_the_key_reaches_no_page_that_will_leave_it_on_the_wall(tmp_path):
    """The stripping either covers the doors this page opens or it is theatre.

    demo.html takes the key out of its own bar and prints a banner saying so,
    which is the right instinct and was one page out of four. Reproduced: open
    /evidence?session=…&key=… from this page's own button and location.href
    still contains the key, on the screen the room is now looking at.

    Two of those three destinations are pages in this repository, and the fix
    for them is the same one demo.html already has — a page that takes a key in
    its URL takes it out of its bar, wherever it was opened from, including a
    bookmark. The third is the participant landing page, which is the
    participant's own screen and has no researcher chrome to strip it: the key
    still has to travel there, so this page has to SAY so rather than quietly
    hand it over.
    """
    out = _drive_demo(tmp_path, "gateway-up", "?key=k7")
    assert not any("key=" in u for u in out["replaced"]), (
        "demo.html stopped stripping its own bar: " + str(out["replaced"]))

    doors = {"nav to the participant landing page": out["navHome"],
             "nav to the researcher console": out["navResearcher"],
             "the evidence trace button": out["evidenceUrl"],
             "the rating console button": out["raterUrl"]}
    prose = _prose(_demo())
    warned = _warns_the_landing_page_keeps_the_key(prose)
    leaks = []
    for what, url in doors.items():
        assert url, f"{what} produced no URL at all"
        if "key=" not in url:
            continue
        path = url.split("?")[0].split("#")[0]
        page = {"/researcher": RESEARCHER, "/evidence": EVIDENCE}.get(path)
        if page is not None and _strips_its_own_bar(page):
            continue
        # Not a page we can strip: it has to be named on screen instead.
        if path == "/" and warned:
            continue
        leaks.append(f"{what} -> {path}")
    assert not leaks, (
        "the researcher key is handed to pages that do not take it out of their "
        "own address bar, and the page does not warn that they will keep it: "
        + ", ".join(leaks))
    assert not out["keyTravelHidden"], (
        "the warning about the one link that keeps the key is in the markup but "
        "hidden on a page that was opened with a key, so nobody in the room "
        "reads it")


def test_the_landing_page_key_is_named_rather_than_handed_over_quietly():
    """The one destination this repository's demo screens cannot fix.

    GET / is check_key'd, so on a keyed server the participant landing page
    needs the key in its URL and there is nowhere else to put it. It is the
    participant's page; it has no researcher banner to strip it. So the page
    says which link keeps the key visible, rather than leaving the researcher
    to notice it on the projector.
    """
    assert _warns_the_landing_page_keeps_the_key(_prose(_demo())), (
        "no sentence on the page tells the researcher that the participant "
        "landing page keeps the key in its address bar")


def test_the_live_button_is_off_when_the_page_already_knows_the_gateway_is_down(tmp_path):
    """The failure the page exists to prevent, produced by the page's own row.

    Reproduced against a server whose gateway answers nothing: the preflight
    printed "Model gateway — Not answering… A live encounter will fail. Use the
    replay lane." and liveBtn.disabled was false. Clicking it opened the stage
    and minted a run against a gateway that cannot answer, in front of the room.
    """
    out = _drive_demo(tmp_path, "gateway-down")
    assert "A live encounter will fail" in out["checks"], (
        "the preflight no longer says the gateway is down, so this test is not "
        "measuring what it claims: " + out["checks"])
    assert out["liveDisabled"], (
        "the live button is enabled with the gateway already known down")
    assert out["liveTabDisabled"], (
        "'Start it in its own tab' is the same encounter through a different "
        "window and is still enabled")
    assert not out["gateNoteHidden"], "nothing on screen says why the lane is off"
    assert "gateway" in out["gateNote"].lower(), (
        "the note does not name the gateway as the reason: " + out["gateNote"])
    assert "replay" in out["gateNote"].lower(), (
        "the note does not point at the lane that still works: " + out["gateNote"])


def test_the_live_button_is_off_when_the_page_cannot_reach_health_at_all(tmp_path):
    """The neighbouring case, and the one the previous round would have left.

    /health unreachable is not "everything is fine": it is a page that knows
    nothing, on a machine that could not answer its own liveness probe. The
    checks strip already says so — "Neither lane will work until it can" — and
    the button underneath was live.
    """
    out = _drive_demo(tmp_path, "unreachable")
    assert out["liveDisabled"] and out["liveTabDisabled"], (
        "the live button is enabled on a page that could not read /health")
    assert not out["gateNoteHidden"]


def test_an_unchecked_gateway_is_not_treated_as_a_working_one(tmp_path):
    """`ok: null, checked: false` is a process that never ran its startup
    hooks. /health's own docstring calls it a genuine unknown, and unknown is
    not the same as yes — the same rule the steering card already applies to a
    model nobody has probed."""
    out = _drive_demo(tmp_path, "gateway-unchecked")
    assert out["liveDisabled"], (
        "an unchecked gateway is being read as a working one")


def test_the_live_button_is_still_open_when_the_preflight_is_green(tmp_path):
    """The other side of it. A page that disables the live lane whatever
    /health says has not fixed anything — it has removed the lane."""
    out = _drive_demo(tmp_path, "gateway-up")
    assert not out["liveDisabled"], (
        "the live lane is switched off on a server where everything the page "
        "checked is working")
    assert not out["liveTabDisabled"]
    assert out["gateNoteHidden"], "the page is explaining a gate that is not closed"


def test_the_live_lane_is_off_when_the_key_is_missing_as_it_already_was(tmp_path):
    """Pin: the 401 gate this page already had must survive the gateway gate
    being added beside it."""
    out = _drive_demo(tmp_path, "gateway-up", "")
    assert not out["liveDisabled"]
    out = _drive_demo(tmp_path, "keyed", "")
    assert out["liveDisabled"], (
        "a keyed server with no key no longer switches the live lane off")
    assert "401" in out["gateNote"], (
        "the note stopped saying that /test would answer 401: " + out["gateNote"])


# =============================================================================
# 4b. A key that is PRESENT is not a key that WORKS
# =============================================================================

def test_a_wrong_key_does_not_get_the_green_tick(tmp_path):
    """The row asked whether a key existed, never whether it opened anything.

    `keyed && !KEY` is the whole computation it used to do, and /health cannot
    do better than that: its own docstring says an absent or wrong key "is
    never an error here, it just gets the narrow answer", because refusing the
    liveness probe would take the task out of service.

    Reproduced in a real browser against a server keyed with a value this page
    was not given — /static/demo.html?key=wrong-key-xyz on a server keyed
    otherwise. The strip printed, in green:

        ✓ Researcher key — This server is keyed and you opened this page with
          the key. Every door below carries it.

    liveBtn.disabled and liveTabBtn.disabled were both false. Pressing the
    button opened the full-screen demo stage over the room, chrome reading
    "cohort: internal" and "consent recorded as internal_test", with
    {"detail":"Bad or missing key"} in the frame underneath. /test answered 401
    to the same key; so did /api/encounters.
    """
    out = _drive_demo(tmp_path, "keyed", "?key=nope")
    mark, name, what = _key_row(out["checks"])
    assert mark != "ok", (
        "a key this server refuses still gets the green tick: " + name + " — " + what)
    assert "401" in what or "refused" in (name + what).lower(), (
        "the row does not say the key was refused, so the room is told nothing "
        "it can act on: " + name + " — " + what)
    assert out["liveDisabled"] and out["liveTabDisabled"], (
        "the live lane is armed on a page whose key a gated route just refused; "
        "pressing it puts a 401 on the projector inside the demo stage")
    assert not out["gateNoteHidden"], "nothing on screen says why the lane is off"


def test_a_refused_key_shuts_the_live_lane_and_not_only_the_replay_lane(tmp_path):
    """It was exactly backwards, and one gate is the reason.

    check_key guards /test, /evidence and /api/encounters alike. On a wrong key
    the page disabled the encounter picker and "Open the evidence trace" — the
    lane whose own eyebrow reads "cannot fail live" — and left the live button
    armed. The lane it refused was the one that was going to work as soon as
    the key was fixed; the lane it kept was the one that could not.
    """
    out = _drive_demo(tmp_path, "keyed", "?key=nope")
    assert out["encSelectDisabled"] and out["evidenceDisabled"], (
        "the replay lane is open on a key /api/encounters refused, so the "
        "picker is about to be empty in front of the room")
    assert out["liveDisabled"], (
        "the page shuts the replay lane and arms the live lane on the same "
        "401 — the refusal is pointed at the wrong lane")
    note = out["gateNote"].lower()
    assert "same gate" in note or "both lanes" in note, (
        "the note does not say that one key shuts both lanes, so a researcher "
        "reading it goes to the replay lane it has already closed: "
        + out["gateNote"])
    # The other half, and the one that matters more: the right key opens both.
    good = _drive_demo(tmp_path, "keyed", "?key=k7")
    assert not good["liveDisabled"] and not good["liveTabDisabled"], (
        "POSITIVE CONTROL FAILED: a researcher on a keyed server holding the "
        "right key cannot start a live demo")
    assert not good["encSelectDisabled"] and not good["evidenceDisabled"], (
        "POSITIVE CONTROL FAILED: the right key does not open the replay lane")
    assert _key_row(good["checks"])[0] == "ok", (
        "POSITIVE CONTROL FAILED: the right key does not get the green tick: "
        + str(_key_row(good["checks"])))


def test_an_unset_session_key_is_not_a_reassuring_green_fact(tmp_path):
    """What this row said, in green, on the page designed to be projected:

        ✓ Researcher key — Not configured, so this is a loopback server and
          every door below is open. Nothing here is reachable from anywhere
          else.

    Two problems in one card. An unset SESSION_KEY means the researcher
    surface — /test, /evidence, the encounter list, the whole dataset — is
    unauthenticated, which is a thing to notice rather than a tick. And the
    second sentence is not something this page can know: server/app.py decides
    reachability by reading HOST and the allowlist, none of which reaches
    /health, so "nothing here is reachable from anywhere else" is a guess
    wearing a tick on a screen a room is reading.
    """
    out = _drive_demo(tmp_path, "gateway-up", "")
    mark, name, what = _key_row(out["checks"])
    assert mark != "ok", (
        "an unauthenticated researcher surface is still printed as a green "
        "tick: " + name + " — " + what)
    low = what.lower()
    assert "reachable from anywhere else" not in low, (
        "the page still tells the room it knows who can reach this server: " + what)
    assert "open" in low or "waves everyone through" in low, (
        "the row no longer says that the doors are unauthenticated, which is "
        "the fact it is for: " + what)
    # And it must not gate: an unkeyed development box is where most demos are
    # given, and a warning that switched the lane off would remove the lane.
    assert not out["liveDisabled"] and not out["liveTabDisabled"], (
        "POSITIVE CONTROL FAILED: the live lane is off on an unkeyed "
        "development server where everything the page checked is working")
    assert out["gateNoteHidden"], "the page is explaining a gate that is not closed"


# =============================================================================
# 4c. A check that has not completed is not a pass
# =============================================================================

def test_the_live_buttons_are_off_in_the_markup_and_not_only_in_the_script():
    """The window this page had no cover for at all.

    encSelect and evidenceBtn ship `disabled` in the markup and are switched on
    by an answer. liveBtn and liveTabBtn shipped with no such attribute, so
    between the browser painting them and some fetch resolving they were live —
    and if no fetch ever resolves, that is the state they stay in. The script
    half is below; this is the half that does not depend on the script running
    at all.
    """
    src = _demo()
    for element_id in ("liveBtn", "liveTabBtn"):
        tag = _open_tag(src, element_id)
        assert "disabled" in tag, (
            f"#{element_id} is live in the markup, so it is live before the "
            f"preflight has said anything: {tag}")
    # The pin in the other direction: the replay lane's two controls kept it.
    for element_id in ("encSelect", "evidenceBtn"):
        assert "disabled" in _open_tag(src, element_id)


def test_a_health_that_never_answers_does_not_leave_the_live_lane_armed(tmp_path):
    """A fetch that hangs is not a fetch that fails, and only one of the two
    was handled.

    Reproduced in a real browser through a proxy whose GET /health accepts the
    socket and writes nothing: the strip sat at "… Checking — Asking the server
    what it can reach" for as long as the page was open, the steering card
    stayed hidden, liveBtn.disabled and liveTabBtn.disabled were both false,
    and nothing was ever going to change any of that. `fetch` has no timeout;
    the page had no cap; so the preflight never finished and therefore never
    decided anything, while the buttons it was deciding about sat enabled.

    Two things are asked here. While the answer is outstanding the lane is off
    and says so — that is the markup default plus liveLaneWaiting(). And the
    wait ends: the page stops waiting, says it could not read /health, and the
    steering card speaks, because "what will steering do" is the question the
    researcher gets asked whatever the preflight did.
    """
    out = _drive_demo(tmp_path, "health-hangs", "")

    waiting = out["waiting"]
    assert waiting["liveDisabled"] and waiting["liveTabDisabled"], (
        "with /health still outstanding both live buttons are armed: a check "
        "that has not completed is being read as a pass")
    assert not waiting["gateNoteHidden"], (
        "the lane is off and nothing on screen says so, which is a button that "
        "looks broken rather than a page that explains itself")

    assert out["liveDisabled"] and out["liveTabDisabled"], (
        "the live lane is armed on a page whose /health never answered")
    assert "Checking" not in out["checks"], (
        "the preflight strip is still saying it is checking, so the page waits "
        "on this fetch for the length of the demo: " + out["checks"])
    assert not out["steerHidden"], (
        "the steering card is hidden for the life of the page, so the page is "
        "silent about the one thing the room will ask about")
    assert "steer" in (out["steerTitle"] + out["steerBody"]).lower()

    # And the replay lane, which does not read /health, is untouched by any of
    # it — including by the cap.
    assert not out["encSelectDisabled"] and not out["evidenceDisabled"], (
        "POSITIVE CONTROL FAILED: a hanging /health took the replay lane down "
        "with it, and the replay lane never asked /health")


def test_a_working_server_is_not_held_off_by_the_cap(tmp_path):
    """The positive control for the cap, said as its own question.

    A page that refuses to arm the live lane until some timer has elapsed, or
    that arms it and then takes it away, has not fixed the hang — it has broken
    the demo. On a server that answers, the answer is what decides, and it
    decides before any timer fires.
    """
    out = _drive_demo(tmp_path, "gateway-up", "")
    assert not out["waiting"]["liveDisabled"], (
        "POSITIVE CONTROL FAILED: on a server that answered /health "
        "immediately the live lane was still off before the cap elapsed")
    assert not out["liveDisabled"] and not out["liveTabDisabled"], (
        "POSITIVE CONTROL FAILED: the cap elapsed and took a working live "
        "lane away with it")
    assert out["gateNoteHidden"]


# =============================================================================
# 4d. Two rows this page cannot re-check, and one sentence that said it could
# =============================================================================

def test_the_page_does_not_offer_a_recheck_that_cannot_recheck():
    """"Present that, and re-check the server once this is sorted out."

    server/app.py probes the gateway in _check_gateway and the bucket in
    _check_storage, both wired to the startup hook only, and /health serves
    those two cached dicts on every request — the route's own comment says
    "storage is the startup answer, not a fresh call". So the gateway row is
    latched for the life of the process and the page's own remedy cannot move
    it.

    Reproduced in a real browser: a server booted against a gateway that
    refused the connection, printing "✕ Model gateway — Not answering"; the
    gateway then brought up and answering GET /v1/models 200; "Re-check the
    server" pressed; the row still read ✕ and the live lane was still off. The
    positive control is on the other side of it and was also driven — the same
    server restarted with the gateway up reads ✓ and the lane comes back — so
    what the page has to say is "restart", not "press this".
    """
    prose = _prose(_demo())
    low = prose.lower()
    assert "re-check the server once this is sorted out" not in low, (
        "the page still sends the researcher to a button that cannot change "
        "the row they are looking at")
    # It has to say what it IS: cached from boot, and what actually clears it.
    assert "boot" in low or "startup" in low, (
        "nothing on the page says the gateway and bucket rows are the server's "
        "boot-time preflight, so the next reader presses the button again")
    assert "restart" in low, (
        "the page names no way to clear a latched gateway row, which leaves "
        "the researcher with a red row and no instruction")


def test_the_gate_note_names_a_restart_rather_than_a_recheck(tmp_path):
    """The same sentence, at the place it is actually read: under the button it
    is about, on the machine it is about."""
    out = _drive_demo(tmp_path, "gateway-down", "")
    note = out["gateNote"].lower()
    assert "restart" in note, (
        "the note under the disabled live button does not say the server has "
        "to be restarted: " + out["gateNote"])
    assert "re-check the server once this is sorted out" not in note
    # And on a key problem the remedy is the opposite one, because the key IS
    # asked again — a note that said "restart" there would send somebody to
    # bounce a healthy server.
    keynote = _drive_demo(tmp_path, "keyed", "?key=nope")["gateNote"].lower()
    assert "restart" not in keynote, (
        "a refused key is being reported as something a server restart fixes: "
        + keynote)


def test_the_two_lanes_do_not_contradict_each_other_about_health(tmp_path):
    """"Neither lane will work until it can", printed two sections above "The
    replay lane reads recorded encounters off disk and is unaffected."

    Both sentences were on one screen and one of them was false. Reproduced in
    a real browser through a proxy answering /health 503 and proxying
    everything else: the strip read "Neither lane will work until it can" while
    the picker under it read "28 recorded on this server, 26 of them study
    data" with both of its controls enabled.
    """
    out = _drive_demo(tmp_path, "unreachable", "")
    assert "Neither lane will work" not in out["checks"], (
        "the strip still says a dead /health stops the replay lane, which it "
        "does not: " + out["checks"])
    assert "replay" in out["checks"].lower(), (
        "the strip says nothing about the lane that still works, which is the "
        "one thing a researcher looking at it needs: " + out["checks"])
    # The proof that the first sentence was false, on the same run.
    assert not out["encSelectDisabled"] and not out["evidenceDisabled"], (
        "POSITIVE CONTROL FAILED: /health being unreadable took the replay "
        "lane down, so the sentence that was removed was true after all")
    assert out["liveDisabled"], "pin: the live lane is still off"


# =============================================================================
# 4e. The picker, and the row with no cohort at all
# =============================================================================

def test_an_encounter_with_no_cohort_is_marked_like_the_comment_says(tmp_path):
    """The picker's own comment claimed it and the code did not do it.

        const mark = (c) => (!c || c === 'study') ? '' : `[${…}] `;

    `!c` fell into the same branch as 'study', so a manifest with no cohort
    field — /api/encounters returns cohort: null for it — landed in the "Not
    study data" group rendered exactly like a study row. Driven against a real
    server with such a manifest in its wave: it sorted newest-first to the top
    of that group and read "S4A — Planning an internal rollout", which is the
    title four of the study rows above it also carry. No ?cohort= filter
    returns it, so it is not study data, and a row that does not say so is the
    unmarked demo row this whole section exists about.
    """
    out = _drive_demo(tmp_path, "gateway-up", "")
    opts = {o["value"]: o for o in out["options"]}
    assert "s_no_cohort" in opts, out["options"]
    text = opts["s_no_cohort"]["text"].strip()
    assert text.startswith("["), (
        "an encounter with no cohort at all carries no mark, so it is a study "
        "row to anyone reading the picker: " + text)
    assert "internal" not in text.upper().split("]")[0], (
        "an encounter with no cohort is being marked INTERNAL, which says "
        "something about it that its manifest does not: " + text)
    assert (opts["s_no_cohort"]["group"] or "").lower() == \
        (opts["s_internal"]["group"] or "").lower(), (
        "the untagged encounter is not in the non-study group")
    # Pin: the rows that ARE study data still carry no mark.
    for v in ("s_rollout_1", "s_rollout_2"):
        assert not opts[v]["text"].strip().startswith("[")


def test_the_page_does_not_assert_a_scenario_count_it_got_wrong():
    """"Four of the eight v3 scenarios are titled 'Planning an internal
    rollout'."

    Counted on this tree: the eight v3 titles are eight different strings and
    exactly one of them is "Planning an internal rollout" (S4A). The arithmetic
    the sentence was explaining is right — five rows with the word "internal"
    in them on the demo wave, four of them study data — but it comes from the
    ENCOUNTER count, four S4A encounters out of 27, not from the scenario
    catalogue. A sentence that explains a real defect with a made-up number
    teaches the next reader the wrong lesson about the catalogue.

    The catalogue is TEN now — S1 C and S3 C joined it — and this test used to
    pin the literal 8, which meant a third form of a construct turned a test
    about one sentence on a demo page into a red suite. What it is actually
    holding is that the titles are distinct and that exactly one of them is the
    one the sentence named, so that is what it asserts; the size of the bank is
    checked where the bank is the subject (tests/test_scenarios_v3.py and the
    scenario-integrity job in CI), not here.
    """
    import re

    titles = []
    for f in sorted((ROOT / "scenarios" / "v3").glob("*.yaml")):
        m = re.search(r'^title:\s*"(.*)"\s*$', f.read_text(encoding="utf-8"), re.M)
        assert m, f"{f.name} has no title line"
        titles.append(m.group(1))
    assert len(titles) >= 8, titles
    assert len(set(titles)) == len(titles), (
        "two v3 specs carry the same title, so no sentence about the catalogue "
        f"can identify either of them: {sorted(titles)}")
    n = sum(1 for t in titles if t == "Planning an internal rollout")
    assert n == 1, (
        "the v3 catalogue has changed; whatever this page says about it has to "
        f"be recounted: {titles}")

    src = _demo()
    assert "Four of the eight v3" not in src, (
        "the page still tells the next reader that four of the eight v3 "
        "scenarios carry that title; one does")


# =============================================================================
# 4f. Chrome that asserts, over a run that may not exist
# =============================================================================

def test_the_stage_chrome_reads_the_run_rather_than_asserting_it(tmp_path):
    """Four assertions painted before the run existed.

    stageCohort was the literal string "cohort: internal" and the foot strip
    the literal "Demo run · cohort internal · excluded from the study dataset ·
    consent recorded as internal_test". Reproduced in a real browser: this page
    opened with a key the server refuses, the live button armed (see the key
    row above), the stage opened full-screen with all four of those on screen,
    and the frame under them held {"detail":"Bad or missing key"}. No run, no
    cohort, no consent record, and the chrome asserted all three.

    The consent line is gone rather than fixed: this page has no route that
    reads the participant store, so it has no way to know what the consent
    record says and no business saying it on a screen a room is reading.
    """
    src = _demo()
    chip = src[src.index('id="stageCohort"'):]
    chip = chip[:chip.index("</span>")]
    assert "internal" not in chip.lower(), (
        "the cohort chip still ships asserting a cohort: " + chip)
    foot = src[src.index('id="stageFoot"'):]
    foot = foot[:foot.index("</div>")]
    assert "internal_test" not in foot and "cohort internal" not in foot, (
        "the foot strip still ships asserting this run's cohort and its "
        "consent record: " + foot)

    out = _drive_demo(tmp_path, "gateway-up", "")
    on_open = out["stageOnOpen"]
    assert "internal" not in (on_open["cohort"] + on_open["foot"]).lower(), (
        "the stage opens asserting a cohort before anything has read the run: "
        + str(on_open))
    assert "internal_test" not in on_open["foot"], (
        "the stage opens asserting a consent record: " + on_open["foot"])

    after = out["stageAfterRead"]
    assert "r_demo1" in after["run"], (
        "the chrome did not read the run it was given: " + str(after))
    assert "internal" in after["cohort"].lower(), (
        "the chrome read the run and then printed no cohort, which is the "
        "same absence pointed the other way: " + str(after))
    assert "r_demo1" in after["foot"] and "internal" in after["foot"].lower(), (
        "the foot strip does not say what was read: " + after["foot"])
    assert "internal_test" not in after["foot"], (
        "the foot strip is asserting a consent record it has not read: "
        + after["foot"])


# =============================================================================
# 5. The mark in the picker
# =============================================================================

def test_the_cohort_mark_is_a_mark_and_not_a_word_in_a_title(tmp_path):
    """S4A is titled "Planning an internal rollout", and it is four of the demo
    wave's 27 encounters.

    The picker appended " · internal" to non-study rows, so on that wave it
    showed five rows containing the word internal, four of which are study data
    and one of which is not. A mark that reads as part of a title is not a mark.
    """
    out = _drive_demo(tmp_path, "gateway-up")
    opts = {o["value"]: o for o in out["options"]}
    assert set(opts) == {"s_internal", "s_rollout_1", "s_rollout_2",
                         "s_unattributed", "s_no_cohort"}, out["options"]

    groups = {o["value"]: (o["group"] or "").lower() for o in out["options"]}
    assert groups["s_internal"] and groups["s_unattributed"], (
        "the encounters that are not study data are not separated from the ones "
        "that are; they are still rows in one undivided list")
    assert "study" in groups["s_internal"], (
        "the group holding the non-study encounters does not say so: "
        + groups["s_internal"])
    assert groups["s_rollout_1"] != groups["s_internal"], (
        "a study encounter is in the same group as the demo")

    # And the row itself carries the mark at its head, where a title cannot
    # imitate it.
    assert opts["s_internal"]["text"].strip().upper().startswith("[INTERNAL]"), (
        "the internal row does not open with its cohort: "
        + opts["s_internal"]["text"])
    assert opts["s_unattributed"]["text"].strip().upper().startswith("[UNATTRIBUTED]"), (
        "the unattributed cohort — a participant whose survey key did not pipe "
        "— is not marked either: " + opts["s_unattributed"]["text"])
    for v in ("s_rollout_1", "s_rollout_2"):
        assert not opts[v]["text"].strip().startswith("["), (
            "a study encounter carries a cohort mark: " + opts[v]["text"])


# =============================================================================
# 6. The steering the room will not see
# =============================================================================

def test_the_steering_warning_is_on_the_screen_before_the_button(tmp_path):
    """The thing the researcher will be asked about in that room.

    On nto.gemini-live-2.5-flash a mid-session session.update is inert, so a
    live demo is a conversation that runs and transcribes and contains no
    steering at all. tests/test_demo_door.py already pins what the card says;
    what this pins is that it is on the screen ABOVE the button, in the lane's
    own prose as well as in the card, and that it does not disappear on the one
    machine most likely to need it.
    """
    prose = _prose(_demo())
    assert prose.index('id="steerCard"') < prose.index('id="liveBtn"'), (
        "the steering card is below the button it is a warning about")
    lane = _lane_two(prose)
    assert "steering" in lane.lower(), (
        "the live lane's own prose does not mention steering, so the only "
        "warning is a card that is empty until /health answers")

    # And on a machine that cannot reach /health, the card still says something
    # rather than vanishing — this is the page's whole job.
    out = _drive_demo(tmp_path, "unreachable")
    assert not out["steerHidden"], (
        "the steering card is hidden entirely when /health cannot be read, so "
        "the page is silent about steering on exactly the machine that is "
        "already failing its own preflight")
    assert "steer" in (out["steerTitle"] + out["steerBody"]).lower()


def test_the_steering_card_still_says_it_on_the_model_this_study_runs(tmp_path):
    """Pin, across the harness rather than the file: /health hands the page
    nto.gemini-live-2.5-flash and the card that comes back is the one that says
    the room will see no steering."""
    out = _drive_demo(tmp_path, "gateway-up")
    assert not out["steerHidden"]
    body = out["steerBody"].lower()
    assert "no steering" in (out["steerTitle"] + " " + body).lower()
    assert "steer_unacked" in body


def test_the_live_entrance_does_not_carry_the_key_into_the_participant_page(
        store, monkeypatch):
    """The fourth door, and why it is not in the enumeration above.

    The live lane opens <span class="mono">GET /test?name=…&key=…</span> — in
    the stage frame, and in its own tab through "Start it in its own tab". That
    URL carries the key, and /v2 is the screen the room looks at for the whole
    encounter, so if the key travelled through the redirect it would be on the
    wall for longer than anything else on this page.

    It does not: the 307's Location is built from the run, not from the request.
    Pinned here rather than assumed, because it is the one door of the four that
    the page cannot do anything about from its own side.
    """
    monkeypatch.setattr(appmod, "SESSION_KEY", "test-session-key", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    client = TestClient(appmod.app, raise_server_exceptions=False)
    r = client.get("/test", params={"name": "demo-lab", "key": "test-session-key"},
                   follow_redirects=False)
    assert r.status_code == 307, r.text
    where = r.headers["location"]
    assert where.startswith("/v2"), where
    assert "key=" not in where, (
        "the researcher key survives /test's redirect into the participant "
        f"page the demo stage shows the room: {where}")
