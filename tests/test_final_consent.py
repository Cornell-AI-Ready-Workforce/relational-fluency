"""The consent form, and the three screens that point back at it.

Two defects live here and they are one defect. The shipped `config/consent.yaml`
described a smaller study than the one that runs -- "In this session you'll have
a voice conversation", where a run is four encounters of 7 to 12 minutes -- and
its only mention of a model provider was a *negative* one, that webcam video is
never transmitted to any. Participant microphone audio is streamed live to
Google's Gemini through Cornell's gateway, and the form did not say so. At the
same time the form named no principal investigator, no email and no IRB protocol
number, while three participant screens -- the withdrawal card, the decline card
and the closing card -- told anyone who wanted their data deleted to "contact the
researcher named on the consent form". The one route the study gave a person for
exercising the deletion right the same form promised them pointed at a blank.

What can be tested is not whether the wording is right; no program can tell
approved consent language from a plausible draft, and this repository does not
know the PI's name, the protocol number or the retention period. What is tested
is the mechanism around it:

  * `server.consent_check.consent_fielding_blocker` refuses a config that is
    still unfilled -- including the exact state this repository ships in -- and
    passes one that has been filled in and marked reviewed, so the guard is a
    thing a study can actually satisfy rather than a permanent no;
  * the negative statement about video does not satisfy the audio disclosure,
    which is the specific misreading that let the missing statement survive a
    whole template;
  * `config/consent.yaml` now describes the study that actually runs, and every
    fact this repository does not know is left as a visible marker rather than
    invented;
  * `static/v2.html` names its contact out of that config on every card, prints
    a placeholder at nobody, and still says something true when the config
    carries no contact at all -- driven by opening the page's own consent form
    and pressing its own decline button, not by reading the file.

Run from the repo root:

    python -m pytest tests/test_final_consent.py
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    # pytest puts tests/ on sys.path, not the repo root.
    sys.path.insert(0, str(REPO_ROOT))

from server.consent_check import consent_fielding_blocker  # noqa: E402

CONSENT_YAML = REPO_ROOT / "config" / "consent.yaml"
V2 = REPO_ROOT / "static" / "v2.html"

#: The version string the repository shipped with. A wave collected under it is
#: indistinguishable afterwards from a wave collected under any other unedited
#: copy of the template, which is why the guard refuses it by name.
TEMPLATE_VERSION = "v0.1-2026-06"

#: The template as it was shipped, kept here because it is the exact input the
#: guard exists to refuse. Abridged only in the paragraphs no check reads.
SHIPPED_TEMPLATE = {
    "version": TEMPLATE_VERSION,
    "title": "Conversational AI study — consent",
    "body": (
        "In this session you'll have a voice conversation with an AI partner about a\n"
        "workplace scenario. We are studying how AI can support relational skills\n"
        "such as perspective taking, emotional regulation, apology, and creating\n"
        "psychological safety.\n\n"
        "**What we record:**\n"
        "- Your microphone audio for the duration of the session\n"
        "- Webcam video of you during the session, so that human raters can score\n"
        "  non-verbal conduct (this video is never transmitted to any AI model\n"
        "  provider)\n"
        "- The AI's spoken responses\n"
        "- A transcript of the conversation\n\n"
        "**Your rights:**\n"
        "- You may stop at any time by clicking Stop\n"
        "- You can request that your session data be deleted by contacting the\n"
        "  researcher\n"
        "- Participation is voluntary\n"
    ),
    "confirm_checkbox": "I have read the above and consent to participate.",
}


def _shipped() -> dict:
    return yaml.safe_load(CONSENT_YAML.read_text(encoding="utf-8"))


def _fielded(cfg: dict) -> dict:
    """The same config with every human act performed: the contact details
    supplied, the markers resolved, the text marked reviewed and the version
    bumped off the draft string."""
    out = copy.deepcopy(cfg)
    out["version"] = "v1.0-2026-10"
    out["irb_status"] = {"reviewed": True, "reviewed_by": "A. Reviewer",
                         "reviewed_on": "2026-10-01"}
    out["contact"] = {"pi_name": "Dr Rivera", "email": "rf-study@example.invalid",
                      "irb_protocol": "IRB-2026-9999"}
    out["body"] = re.sub(r"\[FILL IN[^\]]*\]\s*", "", out.get("body", ""))
    return out


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------

def test_the_shipped_template_is_refused_and_the_reason_says_why():
    """Every one of the template's three faults, named in one answer.

    Not the first one found: an operator who fixes the reason they were given
    and boots again should not discover the next one the same way, four times.
    """
    reason = consent_fielding_blocker(SHIPPED_TEMPLATE)
    assert isinstance(reason, str) and reason, "the shipped template was accepted for fielding"
    assert TEMPLATE_VERSION in reason, reason
    assert "contact" in reason.lower(), reason
    assert "microphone audio" in reason, reason


def test_a_denial_that_video_is_transmitted_is_not_a_statement_about_audio():
    """The template's only mention of a model provider was that webcam video is
    never sent to one. Reading that as disclosure is exactly the mistake that
    let the missing audio statement survive; a negated sentence must not satisfy
    the check that the participant has been told where their voice goes."""
    cfg = _fielded(SHIPPED_TEMPLATE)
    cfg["body"] = ("We record your microphone audio and webcam video. The video is "
                   "never transmitted to any AI model provider.")
    reason = consent_fielding_blocker(cfg)
    assert reason and "microphone audio" in reason, reason

    # And the affirmative statement, in one sentence, does satisfy it.
    cfg["body"] += (" While you are speaking, your microphone audio is transmitted "
                    "live to a Google speech model through Cornell's gateway.")
    assert consent_fielding_blocker(cfg) is None, consent_fielding_blocker(cfg)


def test_a_filled_in_and_reviewed_form_is_allowed_to_field():
    """The guard has to be satisfiable, or it becomes something a researcher
    edits around rather than answers."""
    assert consent_fielding_blocker(_fielded(_shipped())) is None


@pytest.mark.parametrize("value", [
    "", "   ", None,
    "[FILL IN: principal investigator's name]",
    "TBD", "todo", "<the PI's name>", "[study contact]",
])
def test_a_placeholder_is_not_a_contact_detail(value):
    cfg = _fielded(_shipped())
    cfg["contact"]["pi_name"] = value
    reason = consent_fielding_blocker(cfg)
    assert reason and "pi_name" in reason, f"{value!r} was accepted as a researcher's name"


def test_an_address_that_is_not_an_address_is_refused():
    cfg = _fielded(_shipped())
    cfg["contact"]["email"] = "the study team"
    reason = consent_fielding_blocker(cfg)
    assert reason and "email" in reason.lower(), reason


def test_the_template_version_is_refused_even_when_everything_else_is_filled():
    """consent_text_version is recorded against every participant and is the
    only way to tell afterwards which wording somebody agreed to. Left at the
    template's string it cannot distinguish this wave from an unedited copy."""
    cfg = _fielded(_shipped())
    cfg["version"] = TEMPLATE_VERSION
    reason = consent_fielding_blocker(cfg)
    assert reason and TEMPLATE_VERSION in reason, reason

    cfg["version"] = "v0.3-2026-09-draft"
    reason = consent_fielding_blocker(cfg)
    assert reason and "draft" in reason.lower(), reason


def test_review_is_a_human_act_and_the_guard_will_not_infer_it():
    cfg = _fielded(_shipped())
    for absent in ({}, {"reviewed": False}, {"reviewed": "later"}, None):
        cfg["irb_status"] = absent
        reason = consent_fielding_blocker(cfg)
        assert reason and "reviewed" in reason, f"{absent!r} passed as a review"


def test_an_unreadable_config_is_a_reason_rather_than_a_traceback():
    """The caller is a startup hook. A consent file that did not parse must
    produce the same loud refusal as one that is unfilled, not an exception
    that takes the server down or, worse, is swallowed into a silent pass."""
    for junk in (None, {}, [], "version: v1", 7):
        assert isinstance(consent_fielding_blocker(junk), str), junk


# --------------------------------------------------------------------------
# The file this repository actually ships
# --------------------------------------------------------------------------

def test_the_shipped_file_is_blocked_and_only_a_person_can_unblock_it():
    """It must not be fieldable as it stands -- it is a draft nobody approved --
    and every reason it is blocked for must be one a human can act on."""
    cfg = _shipped()
    reason = consent_fielding_blocker(cfg)
    assert reason, "the draft in config/consent.yaml would be served to participants"
    for expected in ("contact", "reviewed", "FILL IN"):
        assert expected in reason, f"{expected!r} missing from: {reason}"
    # ...but NOT for the disclosure defect. The body already says where the
    # participant's voice goes, so this assertion is what keeps that sentence in
    # the file: delete it and the guard starts complaining, and this test fails
    # before a participant ever sees the shortfall.
    assert "microphone audio is transmitted" not in reason, reason


def test_the_consent_text_describes_the_study_that_actually_runs():
    cfg = _shipped()
    body = cfg["body"].lower()
    assert cfg["version"] != TEMPLATE_VERSION, "the template's version was never bumped"
    # Four encounters, not "this session". server/runs.py CONSTRUCT_ORDER
    # assigns four and the page renders "Encounter N of 4".
    assert "four" in body and "conversations" in body, body
    assert "7 to 12" in body or "7-12" in body, "no per-conversation duration is stated"
    # Where the participant's voice actually goes.
    assert "gemini" in body or "google" in body, "the model provider is not named"
    assert "cornell" in body and "gateway" in body, "the gateway is not named"
    assert "transmitted live" in body, "the form never says the audio leaves in real time"
    # The three recorded channels, and the completion code the payment hangs on.
    for topic in ("microphone", "webcam", "transcript", "completion code"):
        assert topic in body, f"the form does not mention {topic}"


def test_every_fact_this_repository_does_not_know_is_left_visible():
    """The retention period, the money, the eligibility rule and the people are
    not in this repository, and a plausible-looking invented number in a consent
    form is worse than a blank: it would be read as the study's actual answer.
    They stay as markers, and the guard refuses to field a form that still
    carries one."""
    raw = CONSENT_YAML.read_text(encoding="utf-8")
    cfg = _shipped()
    assert "REQUIRES IRB REVIEW BEFORE FIELDING" in raw, \
        "nothing in the file says out loud that this is not approved text"
    assert cfg["irb_status"]["reviewed"] is False
    for unknown in ("retention period", "compensation", "age minimum", "IRB protocol number"):
        assert unknown in raw, f"no marker for the unknown {unknown!r}"
    assert set(cfg["contact"]) == {"pi_name", "email", "irb_protocol"}
    assert all("FILL IN" in str(v) for v in cfg["contact"].values()), \
        "a contact value was invented; the guard is the only thing that should fill these"


# --------------------------------------------------------------------------
# The participant page
# --------------------------------------------------------------------------

def _page_code() -> str:
    """static/v2.html with its whole-line // comments removed.

    The comments describe the defect on purpose and quoting it there is how the
    next reader learns why the code is shaped this way; what must not survive is
    the string in a line the page paints.
    """
    lines = V2.read_text(encoding="utf-8").splitlines()
    return "\n".join(l for l in lines if not l.strip().startswith("//"))


def test_no_screen_sends_a_participant_to_a_name_the_form_does_not_carry():
    code = _page_code()
    assert "researcher named on the consent form" not in code, \
        "a card still asserts a fact about the consent form that nothing enforces"
    # "please tell the researcher" was the same instruction with the channel
    # left out, which is the half of the defect that is easy to reintroduce: it
    # reads as an answer. A card may name a researcher, but only where it also
    # says how to reach one, so every mention has to sit next to the contact.
    for m in re.finditer(r"tell the researcher\b", code):
        following = code[m.end():m.end() + 160]
        assert "contact" in following, \
            f"a card names a researcher and no way to reach one: ...{following[:80]!r}"


def test_the_contact_the_page_prints_can_only_come_from_the_consent_config():
    """Every screen that points a participant at a researcher builds the phrase
    through one function, and that function reads `contact:` off /api/consent.
    A hardcoded name anywhere here is how a card comes to assert a contact the
    form does not carry -- which is the defect, exactly."""
    code = _page_code()
    assert code.count("function contactPhrase") == 1
    assert "fetch('/api/consent'" in code
    # The three closing surfaces: the withdrawal/decline card, the end-of-run
    # card, and the consent form itself.
    assert code.count("contactWho()") >= 2
    assert "contactPhrase(cfg)" in code


# --------------------------------------------------------------------------
# ...driven, rather than read
# --------------------------------------------------------------------------

DOM_STUB = r"""/* A browser thin enough to render a card and no thinner.

   The pages under test set textContent and innerHTML, toggle style.display,
   hang handlers off buttons and fetch two endpoints; that is all this
   reproduces. What is asserted is what the page painted, so the DOM has to
   remember innerHTML and nothing else about it. */
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

/* Every request the page makes is answered here or it is a test bug: an
   unrouted URL rejects loudly rather than resolving to something plausible. */
function makeFetch() {
  const calls = [];
  let routes = [];
  function res(status, body) {
    return { ok: status >= 200 && status < 300, status, json: async () => body,
             text: async () => JSON.stringify(body) };
  }
  function fetchStub(url, opts) {
    opts = opts || {};
    const call = { url: String(url), method: (opts.method || 'GET') };
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
           countOf(sub) { return calls.filter(c => c.url.includes(sub)).length; } };
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
  // The run refuses to resolve, so the boot sequence stops before consent,
  // the brief and the audio check: each card under test is then driven on its
  // own, from a page in a known state.
  net.route([{ match: '/api/run/', fn: () => net.res(503, {}) }]);
  vm.runInContext(src, ctx, { filename: 'v2.html' });
  return { ctx, sandbox, clock, dom, net, $: (id) => dom.document.getElementById(id) };
}

module.exports = { bootV2, vm };
"""

CONSENT_HARNESS = r"""/* Drives static/v2.html's consent form and its three closing cards against a
   consent config the test supplies, and reads what the page painted.

   Nothing here inspects the file: the form is opened through
   ensureParticipant(), the decline button is pressed, and the withdrawal and
   completion cards are called the way the page calls them. */
'use strict';
const assert = require('assert');
const fs = require('fs');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];
const CFG = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const set = (b, code) => vm.runInContext(code, b.ctx);
const html = (b, id) => String(b.$(id).innerHTML);

function routes(b, consent) {
  // '/api/consent/decline' first: the router matches on a substring and the
  // decline URL contains the form's own path.
  b.net.route([
    { match: '/api/run/config', fn: () => b.net.res(200, { return_url: '' }) },
    { match: '/api/run/', fn: () => b.net.res(503, {}) },
    { match: '/api/consent/decline', fn: () => b.net.res(200, { recorded: true }) },
    { match: '/api/consent', fn: () => (consent === 'down' ? b.net.res(503, {})
                                                           : b.net.res(200, consent)) },
  ]);
}

async function openForm(b) {
  b.ctx.ensureParticipant();
  await b.clock.advance(1000);
}

(async () => {
  // --- the form itself, with contact details filled in -------------------
  {
    const b = bootV2(PAGE, '?run=r_1');
    routes(b, CFG.filled);
    await openForm(b);
    const body = html(b, 'consentBody');
    assert(body.includes('four separate voice conversations'),
           'the consent overlay did not render the config body');
    assert(body.includes('Rivera'), 'the form does not name the PI from `contact:`');
    assert(body.includes('mailto:pi@example.invalid'),
           'the form gives no way to reach anyone: ' + body.slice(-400));
    assert(body.includes('IRB-9999'), 'the form does not carry the protocol number');

    // Declining is the first card that used to say "please tell the researcher"
    // without saying which one or how.
    b.$('consentDecline').click();
    await b.clock.advance(1000);
    const card = html(b, 'nextBody');
    assert(card.includes('Rivera') && card.includes('mailto:pi@example.invalid'),
           'the decline card names nobody: ' + card);
  }

  // --- the withdrawal card ------------------------------------------------
  {
    const b = bootV2(PAGE, '?run=r_1');
    routes(b, CFG.filled);
    const p = b.ctx.showClosing('You have stopped the study',
                                '<p>Nothing further has been recorded.</p>', 'CODE123');
    await b.clock.advance(1000);
    await p;
    const card = html(b, 'nextBody');
    assert(card.includes('CODE123'), 'the withdrawal card lost the completion code');
    assert(card.includes('Rivera') && card.includes('mailto:pi@example.invalid') &&
           card.includes('IRB-9999'),
           'the withdrawal card does not say who to contact: ' + card);
  }

  // --- the card at the end of a completed run ------------------------------
  {
    const b = bootV2(PAGE, '?run=r_1');
    routes(b, CFG.filled);
    set(b, "run = { run_id: 'r_1', total: 4, position: 4, completed: [], " +
           "completion_code: 'CODE456', participant_id: 'p_1' };");
    const p = b.ctx.showRunComplete();
    await b.clock.advance(1000);
    await p;
    const card = html(b, 'nextBody');
    assert(card.includes('CODE456'), 'the completion card lost the code');
    assert(card.includes('Rivera') && card.includes('mailto:pi@example.invalid'),
           'the completion card does not say who to contact: ' + card);
  }

  // --- the same cards over the shipped template ----------------------------
  // Every contact field is still "[FILL IN: ...]". Printing one of those at a
  // participant would be worse than the blank it replaced.
  {
    const b = bootV2(PAGE, '?run=r_1');
    routes(b, CFG.template);
    await openForm(b);
    const p = b.ctx.showClosing('You have stopped the study', '<p>x</p>', '');
    await b.clock.advance(1000);
    await p;
    for (const id of ['consentBody', 'nextBody']) {
      const painted = html(b, id);
      assert(!/FILL[ _-]?IN/i.test(painted), id + ' printed a placeholder: ' + painted);
      assert(!/named on the consent form/i.test(painted),
             id + ' still points at a name the form does not carry: ' + painted);
      assert(/whoever sent you this study link/i.test(painted),
             id + ' says nothing about how to reach anyone: ' + painted);
    }
  }

  // --- and when the consent endpoint does not answer at all ----------------
  // The card still has to be painted, with the code, and must not print
  // "undefined" at somebody who has just withdrawn.
  {
    const b = bootV2(PAGE, '?run=r_1');
    routes(b, 'down');
    const p = b.ctx.showClosing('You have stopped the study', '<p>x</p>', 'CODE789');
    await b.clock.advance(1000);
    await p;
    const card = html(b, 'nextBody');
    assert(card.includes('CODE789'), 'a card that could not load the contact lost the code too');
    assert(!/undefined|null|\[object/.test(card), 'the card printed a broken value: ' + card);
    assert(/whoever sent you this study link/i.test(card), card);
  }

  console.log('CONSENT CARDS OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def test_the_cards_name_a_contact_out_of_the_config_and_never_a_placeholder(tmp_path):
    """Open the page's own consent form, press its own decline button, and call
    the two closing cards the way the page calls them -- against a filled
    config, against the shipped placeholders, and against a consent endpoint
    that does not answer at all."""
    (tmp_path / "stub.js").write_text(DOM_STUB, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(CONSENT_HARNESS, encoding="utf-8")
    cfgs = tmp_path / "cfgs.json"
    cfgs.write_text(json.dumps({
        "filled": {
            "version": "v1.0-2026-10", "title": "Consent",
            "body": "You will have four separate voice conversations with an AI partner.",
            "confirm_checkbox": "I consent.",
            "contact": {"pi_name": "Dr Rivera", "email": "pi@example.invalid",
                        "irb_protocol": "IRB-9999"},
        },
        "template": {
            "version": TEMPLATE_VERSION, "title": "Consent",
            "body": SHIPPED_TEMPLATE["body"],
            "confirm_checkbox": "I consent.",
            "contact": {"pi_name": "[FILL IN: principal investigator's name]",
                        "email": "[FILL IN: study contact email address]",
                        "irb_protocol": "[FILL IN: IRB protocol number]"},
        },
    }), encoding="utf-8")
    # encoding pinned: node emits UTF-8 whatever the machine's locale is, and
    # this page carries characters cp1252 cannot represent.
    proc = subprocess.run([_node(), str(harness), str(V2), str(cfgs)],
                          capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CONSENT CARDS OK" in proc.stdout, proc.stdout
