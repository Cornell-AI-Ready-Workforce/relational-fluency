"""Consent is taken upstream now, and the record has to say so.

Cornell's IRB consent is shown in Qualtrics, before the participant is ever
redirected here. So this platform no longer asks anybody to agree to anything:
the form is gone from static/v2.html. What did NOT go is the RECORD. The voice
socket closes 4403 for any participant whose record does not carry consent
(server/app.py's _consented_participant), and that record is the only thing
standing between this platform and recording someone who never agreed.

A record that merely says "consent_given: true" is not enough once the form is
gone. Read in an audit two years from now it asserts that this platform obtained
consent, which is a false statement about where the affirmative act happened and
about which text the participant read. So every consent this platform writes has
to name three things:

    consent_source            where the act happened
    consent_reference         which act, specifically (the Qualtrics response id
                              that /start was handed as ?qid=)
    consent_text_version      which approved wording they saw

and "unknown" is not one of the permitted answers for any of them. The tests
below are about those three fields, about the participant who arrives with no
upstream signal at all (who must not be recorded, and must not be stranded
either), and about the two properties that must survive all of it: a refusal is
still terminal, and the 4403 gate is no wider than it was.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from server import runs, storage

REPO_ROOT = Path(__file__).resolve().parent.parent
V2 = REPO_ROOT / "static" / "v2.html"

#: A Qualtrics response id, in the shape Qualtrics actually mints them.
QID = "R_2aBcDeFgHiJkLmN"

#: What the operator sets to name the approved upstream wording. Any string is
#: accepted; the point is that the deployment has to state one, because nothing
#: in this repository knows which text the survey is showing this month.
UPSTREAM_VERSION = "cornell-irb-2026-09-v3"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """An empty participant store and run directory, wired through the modules'
    own globals, with the upstream consent version configured.

    runs.RUNS_DIR is bound at import from storage.DATA_DIR, so repointing
    storage alone would leave run lookups reading the repository's real data
    directory — which is both a live participant store and, on a collection
    machine, PII.
    """
    root = tmp_path / "data"
    monkeypatch.setattr(storage, "DATA_DIR", root)
    monkeypatch.setattr(storage, "SESSIONS_DIR", root / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", root / "participants")
    monkeypatch.setattr(storage, "DB_PATH", root / "index.db")
    monkeypatch.setattr(runs, "RUNS_DIR", root / "runs")
    monkeypatch.setenv("UPSTREAM_CONSENT_VERSION", UPSTREAM_VERSION)
    storage.init_storage()
    return root


def _arrival(qid=QID, key="RF_UPSTREAM_1", **kw) -> tuple:
    """One participant as /start leaves them: a run, and a pending record.

    Built through runs.create and storage.create_participant rather than by
    hand, because the join these tests turn on (run.participant_record_id) is
    exactly what /start writes and a hand-made pair could quietly stop matching
    it.
    """
    run = runs.create(key, qualtrics_id=qid, cohort="study", **kw)
    pid = storage.create_participant(code=key, consent_given=False,
                                     consent_version="")
    run["participant_record_id"] = pid
    runs.save(run)
    return run, pid


def _on_disk(store_root: Path, pid: str) -> dict:
    return json.loads(
        (store_root / "participants" / f"{pid}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The record names where consent was actually obtained
# --------------------------------------------------------------------------

def test_an_upstream_arrival_gets_a_record_that_names_the_survey_response(store):
    """The three fields, from the one arrival that is entitled to them."""
    _run, pid = _arrival()

    rec = storage.record_consent(pid, "ignored-in-app-version")

    assert rec is not None, "a participant who consented in Qualtrics was refused"
    assert rec["consent_given"] is True
    assert rec["consent_source"] == storage.CONSENT_SOURCE_UPSTREAM
    assert rec["consent_reference"] == QID
    assert rec["consent_reference_kind"] == "qualtrics_response_id"
    assert rec["consent_text_version"] == UPSTREAM_VERSION
    assert rec["consent_upstream_verified"] is True
    # and it is on disk, not merely in the returned dict
    assert _on_disk(store, pid)["consent_reference"] == QID


def test_the_record_never_claims_this_platform_took_the_consent(store):
    """The defect this whole change exists to prevent: a record that reads, in
    an audit, as though the participant agreed here."""
    _run, pid = _arrival()

    rec = storage.record_consent(pid, "v0.2-2026-09-draft")

    # config/consent.yaml's version describes the text this platform used to
    # show. Stamping it on a participant who never saw it would name the wrong
    # document as the one they agreed to.
    assert rec["consent_text_version"] != "v0.2-2026-09-draft"
    assert "unknown" not in str(rec["consent_text_version"]).lower()
    assert str(rec["consent_source"]).strip() != ""


def test_the_index_carries_the_provenance_too(store):
    """An IRB audit asks "show me every consent and where it came from". That
    has to be one query, not a directory walk over participant JSON."""
    _run, pid = _arrival()
    storage.record_consent(pid, "ignored")

    with sqlite3.connect(storage.DB_PATH) as conn:
        row = conn.execute(
            "SELECT consent_given, consent_source, consent_reference, "
            "consent_text_version FROM participants WHERE id = ?", (pid,)
        ).fetchone()
    assert row == (1, storage.CONSENT_SOURCE_UPSTREAM, QID, UPSTREAM_VERSION)


# --------------------------------------------------------------------------
# No upstream signal, no record
# --------------------------------------------------------------------------

def test_a_run_with_no_qualtrics_response_id_is_not_recorded_as_consented(store):
    """Someone who reached /start without the survey's ?qid= — a hand-typed
    URL, a forwarded link, a broken Qualtrics redirect. There is no upstream
    consent to point at, so there is nothing truthful to write."""
    _run, pid = _arrival(qid=None)

    assert storage.record_consent(pid, "whatever") is None
    after = _on_disk(store, pid)
    assert after["consent_given"] is False
    assert "consent_recorded_at" not in after
    assert "consent_source" not in after


@pytest.mark.parametrize("leaked", [
    "${e://Field/ResponseID}",
    "e://Field/ResponseID",
    "",
    "   ",
])
def test_an_unpiped_qualtrics_field_is_not_a_reference(store, leaked):
    """Qualtrics piping fails by rendering the field's own spelling, or nothing
    at all (see runs.normalize_participant_key, which already knows this about
    the participant key). A reference that is the literal placeholder points at
    no response, so it is the same case as having none."""
    _run, pid = _arrival(qid=leaked)

    assert storage.record_consent(pid, "whatever") is None
    assert _on_disk(store, pid)["consent_given"] is False


def test_without_a_configured_upstream_version_nothing_is_recorded(
        store, monkeypatch, caplog):
    """The deployment has to say WHICH approved text the survey is showing.

    Nothing in this repository can work it out, and guessing produces exactly
    the "unknown" the requirement rules out — so this fails closed, loudly,
    the way server/consent_check.py fails closed on an unfielded form.
    """
    monkeypatch.delenv("UPSTREAM_CONSENT_VERSION", raising=False)
    _run, pid = _arrival()

    with caplog.at_level("ERROR"):
        assert storage.record_consent(pid, "v0.2-2026-09-draft") is None
    assert _on_disk(store, pid)["consent_given"] is False
    assert "UPSTREAM_CONSENT_VERSION" in caplog.text, \
        "the operator is not told which knob turns collection back on"


@pytest.mark.parametrize("version", ["v1", "v0", "2", "2026-09",
                                    "cornell-irb-2026-09-v3"])
def test_an_irb_wording_the_lab_really_named_is_recorded_as_itself(
        store, monkeypatch, version):
    """POSITIVE CONTROL, and the one that was failing for "v1".

    The rule that refuses a placeholder version held "v1" in its list of
    non-answers, so a lab whose approved wording is called v1 got the same
    outcome as a lab that never set the variable at all: record_consent refuses,
    POST /api/consent answers 404, every voice socket closes 4403, and the wave
    records nothing. The operator's error message does not name their value, so
    the likely repair is to rename the version — at which point
    consent_text_version, the one field an IRB reads, no longer names the
    document the participant actually saw.

    "v1" is far more likely to be somebody's real answer than anybody's
    admission of having none, and this list keeps the neighbours it was measured
    against beside it.
    """
    monkeypatch.setenv("UPSTREAM_CONSENT_VERSION", version)
    _run, pid = _arrival(key=f"RF_VER_{version}")

    rec = storage.record_consent(pid, "ignored-in-app-version")

    assert rec is not None, (
        f"a deployment whose approved wording is called {version!r} could "
        f"record no consent at all")
    assert rec["consent_text_version"] == version
    assert _on_disk(store, pid)["consent_text_version"] == version


def test_the_voice_gate_is_no_wider_than_it_was(store):
    """The end of the chain: a participant storage refused must still be
    refused by the socket that opens the microphone and the webcam."""
    from server import app as appmod

    _run, refused = _arrival(qid=None)
    storage.record_consent(refused, "whatever")

    _run2, allowed = _arrival(key="RF_UPSTREAM_2")
    storage.record_consent(allowed, "whatever")

    monkey = appmod.get_participant
    assert monkey is storage.get_participant or callable(monkey)
    assert appmod._consented_participant(refused) is None
    assert appmod._consented_participant(allowed) is not None


# --------------------------------------------------------------------------
# The properties that must survive
# --------------------------------------------------------------------------

def test_a_refusal_is_still_terminal_even_with_upstream_evidence(store):
    """record_consent refused a declined record before this change, and the
    upstream evidence is not a way around it: somebody who told this platform
    they did not want to take part has said something the survey's consent does
    not answer."""
    _run, pid = _arrival()
    assert storage.record_decline(pid, "v1") is not None

    assert storage.record_consent(pid, "whatever") is None
    assert _on_disk(store, pid)["consent_given"] is False


def test_an_unknown_participant_is_still_a_miss(store):
    assert storage.record_consent("p_0000000000_ffffff", "whatever") is None


def test_an_explicit_source_still_has_to_point_at_something(store):
    """A caller may name the source itself — that is how a future upstream that
    is not Qualtrics gets recorded — but a named source with no reference is
    "unknown" wearing a label."""
    _run, pid = _arrival()

    assert storage.record_consent(pid, "v1", source="some_other_irb_system",
                                  reference="") is None
    rec = storage.record_consent(pid, "v1", source="some_other_irb_system",
                                 reference="TICKET-77")
    assert rec["consent_source"] == "some_other_irb_system"
    assert rec["consent_reference"] == "TICKET-77"
    assert rec["consent_upstream_verified"] is False, \
        "only a verified Qualtrics response may claim the upstream flag"


def test_the_internal_test_entrance_still_works_and_says_what_it_is(store):
    """/test exists so the lab can walk the study before fielding it, and needs
    no Qualtrics setup by design. Requiring a response id there would protect
    nobody — there is no participant — and would mean nobody could check the
    platform works. It is recorded as what it is, not as a survey consent."""
    run = runs.create("test_ben_1789", cohort="internal")
    pid = storage.create_participant(code=run["participant_id"],
                                     consent_given=False, consent_version="")
    run["participant_record_id"] = pid
    runs.save(run)

    rec = storage.record_consent(pid, "v0.2-2026-09-draft")

    assert rec is not None, "the internal test entrance was blocked"
    assert rec["consent_source"] == storage.CONSENT_SOURCE_INTERNAL
    assert rec["consent_reference"] == run["run_id"]
    assert rec["consent_upstream_verified"] is False, \
        "an internal test run claimed the survey's provenance"


def test_a_study_run_is_not_let_through_by_calling_itself_internal(store):
    """The cohort is set by the entrance, not by the participant: /start writes
    "study" (or "unattributed"), /test writes "internal". The exemption must
    follow that and nothing else, or it is a way round the gate."""
    _run, pid = _arrival(qid=None)   # cohort="study", no response id

    assert storage.record_consent(pid, "whatever") is None


def test_a_record_minted_already_consented_never_claims_the_survey(store):
    """create_participant(consent_given=True) is the researcher/ad-hoc path.

    It is not a study arrival — there is no run, so per storage.py's own header
    the encounters are not study data — and it must not borrow the survey's
    provenance to look like one.
    """
    pid = storage.create_participant("RF_ADHOC", True, "local-v1")
    rec = storage.get_participant(pid)

    assert rec["consent_given"] is True
    assert rec["consent_source"] == storage.CONSENT_SOURCE_DIRECT
    assert rec["consent_upstream_verified"] is False
    assert rec["consent_reference"] == "RF_ADHOC"


def test_a_record_with_no_run_is_recorded_but_not_as_a_survey_consent(store):
    """The other half of the same seam: record_consent on a record that never
    came through /start. It is honest about being neither."""
    pid = storage.create_participant("RF_NO_RUN", False, "")

    rec = storage.record_consent(pid, "local-v1")

    assert rec is not None
    assert rec["consent_source"] == storage.CONSENT_SOURCE_DIRECT
    assert rec["consent_upstream_verified"] is False
    assert rec["consent_text_version"] == "local-v1"


# --------------------------------------------------------------------------
# The page: no form, and no silent proceeding
# --------------------------------------------------------------------------

def _page() -> str:
    return V2.read_text(encoding="utf-8")


def test_the_page_no_longer_asks_anybody_to_consent():
    """The lab's requirement, read off the file: there is no consent form.

    No checkbox to tick and no "I consent" label rendered out of the config.

    Whether the page ASKS anyone to agree is deliberately not asserted here.
    The string "consent to participate" does appear in this page — inside the
    filter that strips that very sentence out of the config before the card
    renders it, which is the opposite of asking for it, and a grep cannot tell
    a filter from a prompt. That question is settled against what the page
    actually paints, over the real config/consent.yaml, in the handoff harness
    below.
    """
    src = _page()
    assert 'id="consentCheck"' not in src, "the consent checkbox is still in the page"
    assert "consentCheck" not in src, "the page still reads a consent checkbox"
    assert "confirm_checkbox" not in src, \
        "the page still renders the config's 'I consent to participate' line"
    assert "consentConfirmLabel" not in src, \
        "the page still has somewhere to print an agreement label"


def test_the_page_still_carries_the_contact_and_the_data_description():
    """Removing the form does not remove what the platform tells people. The
    contact still comes from /api/consent's `contact:` block and nowhere else,
    and the card a blocked participant reads still describes what would be
    recorded."""
    src = _page()
    assert "fetch('/api/consent'" in src
    assert src.count("function contactPhrase") == 1
    assert "contactPhrase(cfg)" in src


NODE_STUB = r"""/* A browser thin enough to boot static/v2.html and read what it painted.

   Same shape as the stubs in tests/test_client_blockers.py and
   tests/test_final_consent.py; it carries its own copy for the same reason
   they do (each harness owns what it drives), plus one addition: this one
   records the BODY of every request, because what the page sends to
   /api/consent is half of what is under test here. */
'use strict';
const vm = require('vm');
const fs = require('fs');

function makeClock() {
  let now = 0, nextId = 1;
  const timers = [];
  const api = {
    setTimeout(fn, ms) { const t = { id: nextId++, at: now + (ms || 0), fn }; timers.push(t); return t.id; },
    clearTimeout(id) { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
    setInterval() { return 0; },
    clearInterval() {},
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
    async flush() { for (let i = 0; i < 60; i++) await new Promise(r => setImmediate(r)); },
  };
  return api;
}

function makeDom() {
  const byId = new Map();
  function el(id) {
    return {
      id: id || '', style: {}, dataset: {}, children: [],
      textContent: '', innerHTML: '', value: '', checked: false, disabled: false,
      className: '', scrollTop: 0, scrollHeight: 0, onclick: null,
      classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                   contains(c) { return this._s.has(c); },
                   toggle(c, on) { if (on === undefined) on = !this._s.has(c);
                                   on ? this._s.add(c) : this._s.delete(c); } },
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
  return { document, byId };
}

function makeFetch() {
  const calls = [];
  let routes = [];
  function res(status, body) {
    return { ok: status >= 200 && status < 300, status, json: async () => body,
             text: async () => JSON.stringify(body) };
  }
  function fetchStub(url, opts) {
    opts = opts || {};
    const call = { url: String(url), method: (opts.method || 'GET'), body: opts.body || '' };
    calls.push(call);
    const route = routes.find(r => call.url.includes(r.match));
    return new Promise((resolve, reject) => {
      if (!route) return reject(new Error('unrouted fetch: ' + call.url));
      const out = route.fn(call, calls.filter(c => c.url.includes(route.match)).length);
      if (out === 'hang') return;
      if (out instanceof Error) return reject(out);
      resolve(out);
    });
  }
  return { fetch: fetchStub, calls, res,
           route(list) { routes = list; calls.length = 0; },
           posts(sub) { return calls.filter(c => c.url.includes(sub) && c.method === 'POST'); } };
}

function bootV2(page, search) {
  const src = fs.readFileSync(page, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
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
    location: { search: search, href: 'http://t/v2' + search, reload() {}, replace() {} },
    Blob: function Blob() { this.size = 0; this.type = ''; },
    MediaStream: function MediaStream() {
      this.getVideoTracks = () => []; this.getAudioTracks = () => []; this.getTracks = () => [];
    },
    WebSocket: function WebSocket() { this.close = () => {}; this.send = () => {}; },
    localStorage: { _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; },
                    setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
    alert() {}, confirm: () => true,
  };
  sandbox.navigator = {
    mediaDevices: { getUserMedia: async () => { throw new Error('no camera in a test'); } },
    userAgent: 'node',
    sendBeacon: () => true,
  };
  sandbox.addEventListener = () => {};
  sandbox.removeEventListener = () => {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  const ctx = vm.createContext(sandbox);
  net.route([{ match: '/api/run/', fn: () => net.res(503, {}) }]);
  vm.runInContext(src, ctx, { filename: 'v2.html' });
  return { ctx, sandbox, clock, dom, net, $: (id) => dom.document.getElementById(id) };
}

module.exports = { bootV2, vm };
"""

def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


HANDOFF_HARNESS = r"""/* Drives static/v2.html's consent handoff: what it sends, what it waits for,
   and what a participant with no upstream consent is actually shown.

   Nothing here reads the file. The gate is opened through ensureParticipant()
   the way the boot sequence, Start and the situation card all open it. */
'use strict';
const assert = require('assert');
const fs = require('fs');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
/* The shipped config/consent.yaml, contact filled in the way a fielded study
   fills it. Driving the card off the REAL text is the point: the form language
   this card must not inherit ("by checking the box below...") and the headings
   the [FILL IN: ...] markers leave empty are both in that file and in no
   hand-made fixture. */
const SHIPPED = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const CFG = { version: 'v1.0', title: 'Consent',
              body: 'We record your microphone audio and webcam video.',
              contact: { pi_name: 'Dr Rivera', email: 'pi@example.invalid',
                         irb_protocol: 'IRB-9999' } };

/* Open the gate with `answer` as the POST /api/consent response, and report
   whether the gate ever resolved. */
async function gate(answer, search, setup, cfg) {
  const b = bootV2(PAGE, search || '?run=r_1&participant_id=p_test&consent=1');
  b.net.route([
    { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
    { match: '/api/run/', fn: () => b.net.res(503, {}) },
    { match: '/api/consent/decline', fn: () => b.net.res(200, { recorded: true }) },
    { match: '/api/consent', fn: (call) => (call.method === 'POST'
        ? answer(b, call) : b.net.res(200, cfg || CFG)) },
  ]);
  vm.runInContext(setup || "run = { run_id: 'r_1', participant_id: 'RF_1' };", b.ctx);
  let resolved = null;
  b.ctx.ensureParticipant().then(v => { resolved = v; });
  await b.clock.advance(2000);
  return { b, resolved: () => resolved };
}

(async () => {
  // --- the server wrote the record: the study runs, nothing is shown --------
  {
    const { b, resolved } = await gate(
      (bb) => bb.net.res(200, { participant_id: 'p_written', consent_text_version: 'v3' }));
    assert.strictEqual(resolved(), true, 'a verified participant was not let through');
    assert.notStrictEqual(b.$('consentOverlay').style.display, 'flex',
      'a verified participant was shown a card they did not need');
    // `let` at a vm script's top level lives in the context's lexical scope,
    // not on the global object, so it is read the way it is written.
    assert.strictEqual(vm.runInContext('participantId', b.ctx), 'p_written',
      'the page kept an id the server did not confirm');
    const sent = JSON.parse(b.net.posts('/api/consent')[0].body);
    assert.strictEqual(sent.participant_id, 'p_test',
      'the page did not ask the server to flip the record /start minted');
    assert(sent.consent_source, 'the POST does not say where the consent came from');
  }

  // --- 200, but no record came back: nothing proceeds -----------------------
  // The server answers 200 to things that are not the write (GET /api/consent
  // is the config). A written record is the only evidence, and its shape is
  // {participant_id, consent_text_version}.
  {
    const { b, resolved } = await gate((bb) => bb.net.res(200, { ok: true }));
    assert.strictEqual(resolved(), null, 'the study proceeded with no consent record');
    assert.strictEqual(b.$('consentOverlay').style.display, 'flex',
      'a participant with no consent record was left staring at nothing');
    const body = String(b.$('consentBody').innerHTML);
    assert(/survey/i.test(body), 'the card does not say where consent is given: ' + body);
    assert(/nothing/i.test(body) && /record/i.test(body),
      'the card does not say that nothing is being recorded: ' + body);
    assert(body.includes('Rivera') && body.includes('mailto:pi@example.invalid'),
      'the card offers no way to reach anybody: ' + body);
    assert(!/consent to participate/i.test(body),
      'the card still asks the participant to agree: ' + body);
  }

  // --- the server refused outright (404) ------------------------------------
  {
    const { b, resolved } = await gate((bb) => bb.net.res(404, { detail: 'nope' }));
    assert.strictEqual(resolved(), null, 'a refused participant was let through');
    assert.strictEqual(b.$('consentOverlay').style.display, 'flex',
      'a refused participant was shown nothing');
  }

  // --- the server could not be reached at all -------------------------------
  {
    const { b, resolved } = await gate(() => new Error('offline'));
    assert.strictEqual(resolved(), null, 'an unreachable server let the study start');
    assert.strictEqual(b.$('consentOverlay').style.display, 'flex', 'no card was shown');
    assert(/try again/i.test(String(b.$('consentSubmit').textContent)),
      'there is no way to retry: ' + b.$('consentSubmit').textContent);
    assert(!b.$('consentSubmit').disabled, 'the retry control is dead');
    // A participant on a bad connection must not be sent back to a survey
    // that was never the problem.
    const body = String(b.$('consentBody').innerHTML);
    assert(/could not reach the study server/i.test(body),
      'an unreachable server was blamed on the survey: ' + body);
    assert(!/completed the consent step/i.test(body),
      'an unreachable server was blamed on the survey: ' + body);
  }

  // --- no participant record at all: the page must not mint a consented one -
  // Without an id there is nothing for the server to verify against, and POST
  // /api/consent's other branch creates a record that asserts consent from
  // nothing but a code the page made up.
  {
    const { b, resolved } = await gate(
      (bb) => bb.net.res(200, { participant_id: 'p_invented' }), '?run=r_1');
    assert.strictEqual(resolved(), null, 'a participant with no record was let through');
    assert.strictEqual(b.net.posts('/api/consent').length, 0,
      'the page asked the server to mint a consent record out of nothing');
    assert.strictEqual(b.$('consentOverlay').style.display, 'flex', 'no card was shown');
  }

  // --- the same card, over the config a participant would really be shown ---
  // Everything above drives a three-line fixture. This one drives
  // config/consent.yaml, because that is where the form language and the
  // placeholder-emptied headings actually live.
  {
    const { b } = await gate((bb) => bb.net.res(404, {}), null, null, SHIPPED);
    const body = String(b.$('consentBody').innerHTML);
    assert(!/FILL[ _-]?IN/i.test(body), 'a placeholder was printed at a participant: ' + body);
    assert(!/consent to participate|checking the box|box below/i.test(body),
      'the card asks the participant to tick a box it does not have: ' + body);
    // A heading whose paragraph was nothing but a marker promises information
    // that is not there.
    assert(!/How long we keep it\.<\/strong>\s*(<strong>|$)/.test(body),
      'an empty heading survived the placeholder strip: ' + body);
    assert(/microphone audio/i.test(body),
      'the description of what is recorded was lost: ' + body);
  }

  // --- declining still works, and is still reported -------------------------
  {
    const { b } = await gate((bb) => bb.net.res(200, { ok: true }));
    assert(b.$('consentDecline').listeners.click, 'the decline button has no handler');
    b.$('consentDecline').click();
    await b.clock.advance(2000);
    assert(/You have not taken part/.test(b.$('nextTitle').textContent),
      'a refusal got no closing card: ' + b.$('nextTitle').textContent);
    assert.strictEqual(b.net.posts('/api/consent/decline').length, 1,
      'the refusal was never reported');
  }

  console.log('HANDOFF OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def test_the_page_verifies_the_handoff_and_stops_when_it_cannot(tmp_path):
    """Open the page's own gate and watch what it does with each answer the
    server can give. The one that matters: a participant with no upstream
    consent never reaches capture, and is never left staring at a blank page
    either."""
    (tmp_path / "stub.js").write_text(NODE_STUB, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HANDOFF_HARNESS, encoding="utf-8")
    # The real config, with the contact block filled the way a fielded study
    # fills it — the placeholders themselves are tests/test_final_consent.py's
    # subject, and contactPhrase already refuses to print one.
    import yaml
    cfg = yaml.safe_load((REPO_ROOT / "config" / "consent.yaml").read_text(encoding="utf-8"))
    cfg["contact"] = {"pi_name": "Dr A. Rivera", "email": "rf-study@example.invalid",
                      "irb_protocol": "IRB-2026-9999"}
    shipped = tmp_path / "shipped.json"
    shipped.write_text(json.dumps(cfg), encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(V2), str(shipped)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "HANDOFF OK" in proc.stdout, proc.stdout
