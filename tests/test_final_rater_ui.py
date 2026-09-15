"""The rating console's top-of-packet flag banner.

`server/rater_packet.py` computes a `counts` block and says in its own comment
why: "Surfaced as counts as well as per-turn markers so a console can warn once
at the top instead of hoping the rater notices a marker halfway down a ten-turn
transcript." Two of those counts — `script_mismatch_turns` and `unheard_turns`
— were added to the packet, asserted by the packet's own tests, and reached no
console surface at all, because static/rater.html's renderPacketNotes() built
the banner from an if-chain over the two keys somebody had remembered.

That is the audit's own bug class: a number the server is confident it has
published, and a rater who is never shown it. It is worse than a count that was
never computed, because the server-side suite goes green over it.

`unheard_turns` is the sharp end. When the group room's only participant
transcription channel dies and never comes back, there are no post-loss
participant turns to carry a per-turn note, so the count is the *only* thing on
the packet that says the transcript is truncated rather than short — and a
transcript whose last third has no participant in it reads as a participant who
disengaged.

These tests drive the page's own JavaScript in a Node vm, against packets shaped
like the ones server/rater_packet.py builds, and check four things:

  1. every flag the packet carries is named in the banner, including the two new
     ones;
  2. a flag key this build has never heard of is *shown*, not dropped — the
     if-chain would have swallowed the next one added just as it swallowed these
     two;
  3. the census counts (participant_turns, agent_turns) raise no alarm, so the
     banner does not cry wolf on every packet;
  4. none of it disables submit. These are markers, not blocks: a rater warned
     that part of a transcript is unreliable must still be able to score the
     encounter they watched.

No fixture wave and no network: the packets are built here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "static" / "rater.html"


# --------------------------------------------------------------------------- #
# packets
# --------------------------------------------------------------------------- #

def _packet(assignment_id: str, counts: dict) -> dict:
    """A packet the console can open, carrying `counts` and nothing surprising.

    `media` is the `absent` state on purpose: it is the one video state that
    neither blocks the rating nor needs a <video> element to exist, which keeps
    this test about the banner rather than about playback.
    """
    return {
        "assignment_id": assignment_id,
        "status": "pending",
        "rating_code": f"RC-{assignment_id[-4:]}",
        "construct": "listening",
        "situation": {"text": "The participant was told they had ten minutes.",
                      "people": [{"name": "Devi", "role": "colleague"}]},
        "transcript": [
            {"role": "agent", "speaker": "Devi", "t": 1.0, "text": "Morning."},
            {"role": "participant", "speaker": "Participant", "t": 4.0, "text": "Morning."},
        ],
        "duration_s": 300,
        "duration_display": "05:00",
        "counts": counts,
        "media": {"video_url": None, "video_available": False,
                  "note": "No webcam recording was made for this encounter."},
        "items": [{"id": "esci_01", "number": 1, "text": "They listened.",
                   "construct": "listening", "reverse": False}],
        "scale": {"min": 1, "max": 5, "labels": {}, "na_label": "N/A"},
        "scale_note": "Answer N/A where you do not have enough to judge.",
        "instrument_notice": "Licensed instrument. Do not redistribute.",
    }


CENSUS = {"participant_turns": 6, "agent_turns": 7}

CASES = {
    # No fault flags at all — the banner must stay off. The census counts are
    # not faults and must not be reported as if they were.
    "as_clean": {**CENSUS},
    # The two the console already knew about, kept here so the repair cannot
    # regress them while wiring the new ones in.
    "as_known": {**CENSUS, "interrupted_turns": 2, "untranscribed_turns": 1},
    # Participant speech the live transcriber wrote into another script. The
    # platform KNOWS the words on screen are wrong.
    "as_script": {**CENSUS, "interrupted_turns": 0, "untranscribed_turns": 0,
                  "script_mismatch_turns": 3, "unheard_turns": 0},
    # The terminal-loss case: the participant channel died, nothing after it
    # carries a per-turn note, and this count is the only signal there is.
    "as_unheard": {**CENSUS, "interrupted_turns": 0, "untranscribed_turns": 0,
                   "script_mismatch_turns": 0, "unheard_turns": 1},
    # All four at once, with a singular and a plural among them.
    "as_all": {**CENSUS, "interrupted_turns": 2, "untranscribed_turns": 1,
               "script_mismatch_turns": 1, "unheard_turns": 4},
    # The next count somebody adds to the packet. This build has no wording for
    # it and must say so rather than say nothing.
    "as_future": {**CENSUS, "overlapping_speech_turns": 5},
    # An unrecognised count that is not a count of turns. The banner may not
    # invent the unit: "1200 lines" on a two-turn transcript is a confident
    # false magnitude, which is the class of defect this whole file is about.
    "as_nonturn": {**CENSUS, "dropped_audio_ms": 1200},
    # Flagged AND blocked, and flagged AND already submitted. Every case above
    # uses the `absent` media state, which is the one video state that never
    # blocks — so none of them can see what the banner says on a packet the
    # console has refused. These two can.
    "as_blocked": {**CENSUS, "untranscribed_turns": 2},
    "as_submitted": {**CENSUS, "untranscribed_turns": 2},
}

# Packet overrides for the two cases above that are not about `counts`.
# `video_available` true with no url and status "failed" is the recorded-but-
# unstored state, which renderVideo blocks outright (blockedReason
# 'no-playback'); a submitted packet lands read-only.
CASE_OVERRIDES = {
    "as_blocked": {"media": {"video_url": None, "video_available": True,
                             "video_status": "failed",
                             "note": "Do not rate it — tell the study team."}},
    "as_submitted": {"status": "submitted"},
}


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

HARNESS = r"""/* Opens each packet in static/rater.html's own script, under a thin DOM stub,
   and reports what the flag banner said and whether submit was left usable.

   The stub is deliberately thin — nothing here parses the item markup, because
   nothing here is about the items. What it does not stub, it does not claim to
   have exercised. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const stub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const m = html.match(/<script>([\s\S]*)<\/script>/);
assert(m, 'no script block in the console');

const written = {};
function el(id) {
  return {
    id, style: {}, dataset: {}, value: '', disabled: false, className: '',
    set innerHTML(v) { written[id] = v; }, get innerHTML() { return written[id] || ''; },
    set textContent(v) { written[id + ':text'] = v; }, get textContent() { return written[id + ':text'] || ''; },
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, focus() {}, scrollIntoView() {}, appendChild() {},
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
}

const els = {};
const ctx = {
  console, JSON, Math, Date, Object, Array, String, Number, Boolean,
  parseInt, parseFloat, isNaN, isFinite, URLSearchParams, encodeURIComponent, Promise, Error,
  document: {
    getElementById: (id) => (els[id] = els[id] || el(id)),
    addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    hidden: false, activeElement: null,
  },
  window: { addEventListener() {}, scrollTo() {} },
  location: { search: '?token=rt_' + 'a'.repeat(32), pathname: '/rate' },
  performance: { now: () => Date.now() },
  setInterval() {}, setTimeout: (f) => f(),
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  alert() {}, confirm: () => true,
  CSS: { escape: (s) => s },
  Event: class { constructor(t) { this.type = t; } },
  fetch: async (url) => {
    const u = String(url).split('?')[0];
    let body = null, status = 200;
    if (u.endsWith('/api/rater/me')) body = stub.me;
    else if (u.endsWith('/api/rater/assignments')) body = stub.assignments;
    else if (u.includes('/api/rater/packet/')) { body = stub.packets[u.split('/').pop()]; if (!body) status = 404; }
    else status = 404;
    return { ok: status < 400, status, json: async () => body };
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(m[1], ctx, { filename: 'rater.html' });   // parses, and runs boot()

(async () => {
  await new Promise(r => setTimeout(r, 0));
  const out = {};
  for (const a of stub.assignments) {
    await ctx.openAssignment(a.assignment_id);
    out[a.assignment_id] = {
      banner: written['turnFlags:text'] || '',
      display: (els.turnFlags && els.turnFlags.style.display) || '',
      submit_disabled: !!(els.submitBtn && els.submitBtn.disabled),
      submit_msg: written['submitMsg:text'] || '',
      turns: written.turns || '',
    };
  }
  console.log('RESULT ' + JSON.stringify(out));
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


def _run(console_path: Path, tmp_path: Path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the console harness needs it")
    packets = {a: {**_packet(a, counts), **CASE_OVERRIDES.get(a, {})}
               for a, counts in CASES.items()}
    stub = {
        "me": {"rater_id": "rtr_ab12cd34", "name": "R. Okonkwo", "kind": "trained",
               "assignments_pending": len(packets)},
        "assignments": [{"assignment_id": a, "rating_code": p["rating_code"],
                         "status": "pending", "assigned_at": "2026-04-01T14:05:00Z"}
                        for a, p in packets.items()],
        "packets": packets,
    }
    stub_path = tmp_path / "stub.json"
    stub_path.write_text(json.dumps(stub), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    # encoding is not decoration here: the banner is prose with em dashes and
    # curly quotes in it, and Windows' default cp1252 pipe decoding raises on
    # them, which turns every assertion below into an unreadable UnicodeError
    # about the harness rather than a statement about the console.
    proc = subprocess.run([node, str(harness), str(console_path), str(stub_path)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hit = re.search(r"^RESULT (.*)$", proc.stdout, re.M)
    assert hit, proc.stdout + proc.stderr
    return json.loads(hit.group(1))


@pytest.fixture(scope="module")
def banners(tmp_path_factory):
    return _run(CONSOLE, tmp_path_factory.mktemp("rater_ui"))


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #

def test_script_mismatch_count_reaches_the_banner(banners):
    """A count the server publishes and the rater never sees is the bug.

    The per-turn note already renders (rater.html's renderTurns shows turn.note),
    but the whole reason the count exists is to warn once at the top, before the
    rater starts scoring.
    """
    b = banners["as_script"]
    assert b["display"] == "block", (
        "3 mis-transcribed participant turns and the banner stayed hidden: " + repr(b))
    text = b["banner"]
    assert "3 lines" in text, text
    # Named as the transcriber's fault, not the participant's — the words on
    # screen are wrong, and a rater who reads them as the speaker's will score
    # the platform's defect as the participant's incoherence.
    assert "alphabet" in text or "script" in text, text
    assert "transcriber" in text, text


def test_unheard_count_reaches_the_banner(banners):
    """The terminal-loss case has no post-loss turn to carry a note.

    When the participant's only transcription channel dies and stays dead, this
    count is the sole signal on the packet. Silence here means the rater is told
    nothing at all.
    """
    b = banners["as_unheard"]
    assert b["display"] == "block", (
        "the participant channel died and the banner said nothing: " + repr(b))
    text = b["banner"]
    assert "1 line" in text and "1 lines" not in text, text
    assert "transcription stopped" in text, text
    # The point of the sentence: missing from the transcript, not missing from
    # the encounter. Without that, a short-looking transcript reads as a
    # participant who gave up.
    assert "not missing from the encounter" in text, text


def test_every_flag_the_packet_carries_is_named_at_once(banners):
    text = banners["as_all"]["banner"]
    for fragment in ("1 line", "2 lines", "4 lines"):
        assert fragment in text, (fragment, text)
    assert "not transcribed" in text, text
    assert "spoke over" in text, text
    assert "transcriber" in text, text
    assert "transcription stopped" in text, text


def test_a_count_this_build_has_no_wording_for_is_still_shown(banners):
    """The structural half of the defect.

    The banner used to enumerate the keys this file remembered rather than the
    keys the packet sent, so the *next* count added to rater_packet.py would
    have been dropped exactly as these two were — the same failure, one release
    later. An unrecognised flag has to be visible and has to admit it is
    unrecognised; a rater told "something is wrong and we cannot say what" knows
    to ask, and a rater told nothing does not.
    """
    b = banners["as_future"]
    assert b["display"] == "block", (
        "an unknown count was silently dropped: " + repr(b))
    text = b["banner"]
    assert "5 lines" in text, text
    assert "overlapping speech" in text, text          # humanised, not the raw key
    assert "overlapping_speech_turns" not in text, text
    assert "no wording" in text, text
    assert "tell the study team" in text, text


def test_the_census_counts_do_not_raise_an_alarm(banners):
    """participant_turns and agent_turns describe the transcript; they are not
    faults, and a banner that fires on every packet is a banner raters learn to
    scroll past."""
    b = banners["as_clean"]
    assert b["display"] == "none", b
    assert b["banner"] == "", b
    assert "6" not in b["banner"] and "7" not in b["banner"], b


def test_the_two_original_flags_still_read_the_same(banners):
    b = banners["as_known"]
    assert b["display"] == "block", b
    assert "1 line spoken but not transcribed" in b["banner"], b["banner"]
    assert "2 lines the participant spoke over" in b["banner"], b["banner"]


def test_flags_are_markers_and_never_block_the_rating(banners):
    """A rater told a turn is unreliable must still be able to score the
    encounter. Blocking would throw away a rating the study can use and leave
    the rater with a dead control and a warning they cannot act on — and the
    instrument's own N/A is the honest answer for a moment that cannot be
    judged, which is what the banner points at instead."""
    for name in ("as_known", "as_script", "as_unheard", "as_all", "as_future"):
        b = banners[name]
        assert not b["submit_disabled"], f"{name} disabled submit over a marker: {b}"
        assert "do not rate" not in b["submit_msg"].lower(), (name, b["submit_msg"])
    # And the banner says so in words, so the rater does not decide for
    # themselves that a flagged encounter is one to abandon.
    assert "None of these stops you rating this encounter" in banners["as_all"]["banner"]
    assert "N/A" in banners["as_all"]["banner"]


def test_the_reassurance_is_withheld_where_the_rating_cannot_proceed(banners):
    """"None of these stops you rating this encounter" is a claim about this
    packet, and on a blocked or submitted one it is false.

    The console refuses a recording it could not store, a packet with no items,
    and one already rated: the submit button is dead and the screen says "do not
    rate it — tell the study team". Printed there, the sentence contradicts the
    control beside it and, with no items rendered, points at an N/A button that
    is not on the page. The flags themselves must still be named.
    """
    for name in ("as_blocked", "as_submitted"):
        b = banners[name]
        assert b["display"] == "block", (name, b)
        assert "2 lines spoken but not transcribed" in b["banner"], (name, b["banner"])
        assert "stops you rating" not in b["banner"], (name, b["banner"])
        assert "N/A" not in b["banner"], (name, b["banner"])
    # ...and the packet that really can be rated still gets it.
    assert "stops you rating" in banners["as_known"]["banner"]


def test_an_unrecognised_count_that_is_not_turns_is_given_no_unit(banners):
    """The fallback knows the key and the number and nothing else.

    It used to render every positive number as "N lines", so a count of
    milliseconds read as a magnitude of transcript: "1200 lines flagged 'dropped
    audio ms'" on a two-turn transcript. The `_turns` suffix is the only thing
    that licenses the unit.
    """
    b = banners["as_nonturn"]
    assert b["display"] == "block", b
    text = b["banner"]
    assert "1200 lines" not in text, text
    assert "lines" not in text, text
    assert "dropped_audio_ms: 1200" in text, text
    assert "tell the study team" in text, text


# --------------------------------------------------------------------------- #
# the same claim, read off the source, for hosts with no node
# --------------------------------------------------------------------------- #

def test_the_banner_enumerates_the_packets_counts_rather_than_a_hardcoded_list():
    """Source-level guard so the structural half of this survives on a host
    where the vm harness skips."""
    src = CONSOLE.read_text(encoding="utf-8")
    body = src.split("function renderPacketNotes()")[1].split("\nfunction ")[0]
    assert "Object.keys(counts)" in body, (
        "renderPacketNotes no longer walks the counts the packet actually sent")
    # Every flag the packet builder emits today has wording here.
    for key in ("untranscribed_turns", "interrupted_turns",
                "script_mismatch_turns", "unheard_turns"):
        assert re.search(rf"^\s*{key}:", src, re.M), f"no banner wording for {key}"
    # ...and an unrecognised one has a way out.
    assert "unknownFlagPhrase" in body, "no fallback for an unrecognised count key"


def test_packet_builder_and_console_agree_on_the_flag_keys():
    """If server/rater_packet.py grows a count, the console either has wording
    for it or falls through to the visible fallback — never to silence. This
    checks the first half: that today's keys all have real wording."""
    builder = ROOT / "server" / "rater_packet.py"
    if not builder.is_file():
        pytest.skip("server/rater_packet.py is not in this tree")
    text = builder.read_text(encoding="utf-8")
    block = text.split('"counts": {')[1].split("},")[0]
    keys = set(re.findall(r'"(\w+_turns)"\s*:', block))
    assert keys, block
    src = CONSOLE.read_text(encoding="utf-8")
    census = set(re.findall(r"'(\w+_turns)'", src.split("CENSUS_COUNTS = [")[1].split("]")[0]))
    for key in sorted(keys - census):
        assert re.search(rf"^\s*{key}:", src, re.M), (
            f"{key} is on the packet with no console wording; it would reach the "
            f"rater only through the unrecognised-flag fallback")


# =========================================================================== #
# The rating console's LAYOUT, and the two things a rater cannot work without.
#
# Measured in Chrome at 1500x900, on a real assignment out of the demo wave,
# with each column 855px tall:
#
#   LEFT   the recording sat at offset 306 of a 1925px scroll and the
#          transcript began at 813 — one pixel above the fold. Scrolling far
#          enough to read a turn put the player above the viewport: at the
#          bottom of that column 0px of a 414px recording was on screen. A
#          rater could WATCH or READ, never both, on an instrument whose items
#          are about how somebody sounded and looked while they spoke.
#
#   RIGHT  statement 1 sat at offset 604 behind 558px of licence notice, task
#          instructions and legend — 71% of the working column spent on
#          material the rater had read on the previous 26 encounters. In the
#          700px-tall window a laptop actually gives you, that put statement 1
#          off the first screen entirely.
#
#   PAGE   the document measured 901 against a 900 viewport, so every screen
#          carried a page-level scrollbar for one pixel it could not lose.
#
# These tests are about those outcomes, not about the CSS that delivers them.
# Where a property has to be named — sticky, an inset — the assertion says what
# the property has to ACHIEVE and accepts any arrangement that achieves it,
# because the requirement is "the recording stays with the transcript", not
# "position: sticky".
# =========================================================================== #


def _console_src() -> str:
    return CONSOLE.read_text(encoding="utf-8")


def _css(src: str) -> str:
    """The <style> block, comments stripped.

    Comments go first so that prose about a rule can quote a selector without
    the scanner below reading the quote as a rule.
    """
    block = src.split("<style>", 1)[1].split("</style>", 1)[0]
    return re.sub(r"/\*.*?\*/", "", block, flags=re.S)


def _rules(css: str):
    """Every rule in source order, as (selectors, declarations, at_rule, index).

    A hand-rolled scanner rather than a CSS library: the suite has no third
    party parser and this stylesheet is a few hundred flat rules with one level
    of @media nesting. `at_rule` is the enclosing @media prelude or None, and
    `index` is the offset in the stylesheet — which is what decides the cascade
    between two rules of equal specificity, and is therefore load-bearing here.
    """
    out, i, at, buf = [], 0, None, ""
    while i < len(css):
        ch = css[i]
        if ch == "{":
            prelude = buf.strip()
            buf = ""
            if prelude.startswith("@"):
                at = prelude
                i += 1
                continue
            depth, j = 1, i + 1
            while j < len(css) and depth:
                if css[j] == "{":
                    depth += 1
                elif css[j] == "}":
                    depth -= 1
                j += 1
            out.append((prelude, css[i + 1:j - 1], at, i))
            i = j
            continue
        if ch == "}":
            at = None
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    return out


def _declared(css: str, selector: str, prop: str):
    """Every value `prop` is given to `selector`, in cascade order.

    Returns (value, at_rule, source_index) triples. Selector matching is exact
    on one of the comma-separated parts, which is all this stylesheet needs and
    is far less of a lie than a substring match.
    """
    hits = []
    for prelude, decls, at, idx in _rules(css):
        parts = [p.strip() for p in prelude.split(",")]
        if selector not in parts:
            continue
        for decl in decls.split(";"):
            if ":" not in decl:
                continue
            name, _, value = decl.partition(":")
            if name.strip() == prop:
                hits.append((value.strip(), at, idx))
    return hits


def _elements():
    """Every element in <body>, each with its ancestor stack.

    A parser rather than regexes over the markup: the first draft of these tests
    matched "the wrapper of #videoSlot" with a regex anchored to the end of the
    text before it, which silently found nothing the moment the wrapper and the
    slot were on separate lines, and "is the licence notice inside a <details>"
    by counting tags — which counted the one in an HTML comment. Both failed
    open, which for a test is worse than failing at all.

    Returns dicts of {tag, id, classes, parent} where parent is the same shape
    or None.
    """
    from html.parser import HTMLParser

    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "area", "col"}

    class Walk(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.found = []
            self.in_body = False

        def handle_starttag(self, tag, attrs):
            if tag == "body":
                self.in_body = True
                return
            if not self.in_body:
                return
            a = dict(attrs)
            node = {"tag": tag, "id": a.get("id"),
                    "classes": (a.get("class") or "").split(),
                    "parent": self.stack[-1] if self.stack else None}
            self.found.append(node)
            if tag not in VOID:
                self.stack.append(node)

        def handle_endtag(self, tag):
            if tag == "body":
                self.in_body = False
                return
            if self.in_body and tag not in VOID and self.stack:
                self.stack.pop()

    w = Walk()
    w.feed(_console_src())
    return w.found


def _by_id(node_id: str):
    hits = [n for n in _elements() if n["id"] == node_id]
    assert len(hits) == 1, "expected exactly one #%s in the markup, found %d" % (node_id, len(hits))
    return hits[0]


def _ancestors(node):
    out, p = [], node["parent"]
    while p is not None:
        out.append(p)
        p = p["parent"]
    return out


def _video_wrapper_class(src: str) -> str:
    """The class of the box that wraps the player.

    Walked out from #videoSlot rather than assumed, so renaming the wrapper
    fails the tests below loudly instead of passing them vacuously.
    """
    parent = _by_id("videoSlot")["parent"]
    assert parent and parent["classes"], (
        "nothing with a class of its own wraps #videoSlot; the player has no box that "
        "could be pinned")
    return parent["classes"][0]


# --------------------------------------------------------------------------- #
# the page fits the window
# --------------------------------------------------------------------------- #

def test_nothing_below_the_full_height_app_makes_the_page_scroll():
    """The document measured 901px against a 900px viewport.

    The console is a `height: 100vh` grid, so the two columns are the only
    things that are supposed to scroll. One pixel more than the viewport gives
    the PAGE a scrollbar as well, on every screen, for content that does not
    exist — and the fix is not `overflow: hidden` on the body, which would hide
    a real overflow just as happily as this imaginary one.

    The cause: the visually-hidden aria-live region is `position: absolute` with
    no inset, so it stayed at its static position — the line after a 100vh app —
    and parked a 1px box at y=900. Anything else left below the app would do
    exactly the same, so this asks the general question rather than naming the
    one element: is every top-level box that follows .app pinned inside the
    viewport?
    """
    src = _console_src()
    css = _css(src)
    body = src.split("<body>", 1)[1].split("</body>", 1)[0]

    # Top-level elements of <body> that are not the app itself. Today that is
    # the live region and nothing else.
    trailing = re.findall(r'^<(\w+)\s+class="([^"]*)"[^>]*>', body, re.M)
    after_app = [(tag, cls) for tag, cls in trailing if "app" not in cls.split()]
    assert after_app, "the live region is no longer a top-level sibling of .app; re-read this test"

    for tag, cls in after_app:
        for name in cls.split():
            positions = [v for v, _at, _i in _declared(css, "." + name, "position")]
            if not positions or positions[-1] != "absolute":
                continue
            pinned = _declared(css, "." + name, "top") or _declared(css, "." + name, "bottom")
            assert pinned, (
                '<%s class="%s"> is absolutely positioned with no top or bottom, so it sits '
                "at its static position after a 100vh .app and makes the document taller "
                "than the window again" % (tag, cls))


# --------------------------------------------------------------------------- #
# the recording stays with the transcript
# --------------------------------------------------------------------------- #

def _column_holding(node_id: str) -> str:
    """Which of the rating view's two columns holds #node_id, by class name."""
    for anc in _ancestors(_by_id(node_id)):
        for name in ("media", "form"):
            if name in anc["classes"]:
                return name
    return ""


def test_the_recording_cannot_be_scrolled_away_from_the_transcript():
    """The one outcome this console exists to deliver.

    A rater watches seven to twelve minutes of conversation and scores 22
    statements on it. Several of those statements are about tone, attention and
    what somebody's face was doing, so reading the transcript instead of
    watching is not a substitute — and before this the two were alternatives:
    the transcript began one pixel above the fold, and scrolling to it took the
    player off screen (0 of 414px visible at the bottom of the column).

    Two arrangements satisfy the requirement and this accepts either:

      * the player lives in a different scroll container from the transcript,
        so moving one cannot move the other; or
      * they share a container and the player is pinned inside it.

    What it will not accept is a player that scrolls with the turns, which is
    the arrangement that was there.
    """
    src = _console_src()
    css = _css(src)

    video_col = _column_holding("videoSlot")
    turns_col = _column_holding("turns")
    assert video_col and turns_col, (video_col, turns_col)

    if video_col != turns_col:
        return  # split panes: neither can scroll the other. Nothing more to ask.

    # Same container, so the container has to be a scroller and the player has
    # to be pinned inside it.
    scrolls = [v for v, at, _i in _declared(css, "." + video_col, "overflow-y") if at is None]
    assert scrolls and scrolls[-1] in ("auto", "scroll"), (
        "." + video_col + " is not a scroll container, so the transcript scrolls the whole "
        "page and takes the recording with it")

    wrap = _video_wrapper_class(src)
    default = [v for v, at, _i in _declared(css, "." + wrap, "position") if at is None]
    assert default and default[-1] == "sticky", (
        "the box around the player is %s in the side-by-side layout, so the recording "
        "leaves the column as soon as the transcript is readable"
        % (default[-1] if default else "unpositioned"))
    tops = [v for v, at, _i in _declared(css, "." + wrap, "top") if at is None]
    assert tops and tops[-1] == "0", (
        "the player is sticky with top:%s; it has to pin to the top of the column or it "
        "still leaves the screen" % (tops[-1] if tops else "unset"))
    # Turns scroll UNDER the pinned player rather than beside it, so the box it
    # is pinned in has to paint something. Without this the transcript reads
    # through the player.
    assert _declared(css, "." + wrap, "background"), (
        "the pinned player has no background, so transcript turns scroll through it")


def test_the_pinned_player_is_released_where_the_columns_stack_and_the_release_wins():
    """Stacked, one scroller holds the recording, the transcript AND all 22
    statements — so a pinned player would follow the rater down two thousand
    pixels of questions and own a third of a short window the whole way. Below
    the stacking width it has to let go.

    The second half of this is the half that actually broke. The release was
    first written into the existing narrow-screen block, which sits ABOVE the
    rule it overrides; equal specificity, so the later declaration won and the
    override did nothing at all. Measured at 1100x700 before the move: the
    player was still pinned 1200px down a 4290px stacked scroll. A test that
    only checked "a max-width rule exists somewhere" would have passed on that,
    which is why this one checks which of the two comes last.
    """
    src = _console_src()
    css = _css(src)
    wrap = _video_wrapper_class(src)

    stacked = [at for _v, at, _i in _declared(css, ".rate", "grid-template-columns")
               if at and "max-width" in at]
    assert stacked, "the columns no longer stack at a narrow width; re-read this test"

    decls = _declared(css, "." + wrap, "position")
    narrow = [(v, at, i) for v, at, i in decls if at and "max-width" in at]
    assert narrow, (
        "nothing releases the pinned player where the columns stack, so it follows the "
        "rater through all 22 statements on a narrow screen")
    assert narrow[-1][0] == "static", narrow

    wide = [(v, at, i) for v, at, i in decls if at is None]
    assert wide, decls
    assert narrow[-1][2] > wide[-1][2], (
        "the narrow-screen release is written BEFORE the rule it overrides. Same "
        "specificity, so the later one wins and the release never applies — which is how "
        "it first shipped, still pinned at 1100x700")


# --------------------------------------------------------------------------- #
# nothing scrolls into a live strip in front of a pinned box
# --------------------------------------------------------------------------- #

def _padding_edge(css: str, selector: str, edge: str) -> str:
    """The value `selector` ends up with for padding-`edge`, in cascade order.

    Reads the shorthand as well as the longhand, because the console sets these
    with the shorthand and a test that only looked for `padding-top` would have
    reported "no padding" on `padding: 16px 20px 40px` — passing on exactly the
    stylesheet the defect below shipped in.
    """
    value, best = "0", -1
    for prop in ("padding", "padding-" + edge):
        for val, at, idx in _declared(css, selector, prop):
            if at is not None or idx < best:
                continue
            if prop == "padding":
                parts = val.split()
                # top/right/bottom/left, with the CSS shorthand's own fill-ins.
                top = parts[0]
                bottom = parts[2] if len(parts) > 2 else parts[0]
                val = top if edge == "top" else bottom
            value, best = val, idx
    return value


def _is_zero(value: str) -> bool:
    return re.fullmatch(r"0(px|em|rem|%)?", value.strip()) is not None


def test_no_column_padding_survives_in_front_of_a_pinned_box():
    """A sticky box pins to its scroll container's CONTENT box, so any block
    padding on that container leaves a strip of live, scrolling page in front of
    the thing that is supposed to be covering it.

    This is not a spacing nit. Measured in Chrome at 1500x900 with the right
    column scrolled to 900, on the stylesheet that had `.form { padding: 16px
    20px 40px }`:

      * 16px above the pinned legend, and `document.elementFromPoint` there
        returned the radio inputs of ESCI item 4. Dispatching a click at 8px
        below the top edge of the column checked "2" and moved the counter to
        "1 of 22 answered" — while that item's own text sat 46px above the top
        of the column, behind the legend, unreadable. An answer set on a
        statement the rater cannot see is a bad row in the very matrix this
        study is the agreement between.
      * 40px below the pinned submit bar, with 18 live radio hits for another
        item.
      * 16px above the pinned player, where the stray line is a transcript turn
        and clicking it seeks the recording out from under the rater.

    So: for every box this stylesheet pins to a column edge, the column may not
    hold padding on that edge. Where the spacing goes instead is not this test's
    business — the console moved it onto the content, and a container that
    reached its sticky boxes some other way would satisfy this just as well.
    """
    src = _console_src()
    css = _css(src)
    nodes = _elements()

    # The scroll containers of the rating view, found by asking which of them
    # actually scrolls rather than by naming them.
    containers = []
    for name in ("media", "form"):
        flow = [v for v, at, _i in _declared(css, "." + name, "overflow-y") if at is None]
        if flow and flow[-1] in ("auto", "scroll"):
            containers.append(name)
    assert containers, "neither column is a scroll container any more; re-read this test"

    # Every class this stylesheet pins, and which inset it pins with.
    pinned = {}
    for prelude, decls, at, _idx in _rules(css):
        if at is not None or not re.search(r"position\s*:\s*sticky", decls):
            continue
        for part in (p.strip() for p in prelude.split(",")):
            if not part.startswith(".") or " " in part:
                continue
            for edge in ("top", "bottom"):
                vals = [v for v, a, _i in _declared(css, part, edge) if a is None]
                if vals and _is_zero(vals[-1]):
                    pinned.setdefault(part[1:], set()).add(edge)
    assert pinned, "nothing in the console is pinned to a column edge; re-read this test"

    checked = 0
    for cls, edges in sorted(pinned.items()):
        holders = {c for n in nodes if cls in n["classes"]
                   for a in _ancestors(n) for c in a["classes"] if c in containers}
        for holder in sorted(holders):
            for edge in sorted(edges):
                pad = _padding_edge(css, "." + holder, edge)
                checked += 1
                assert _is_zero(pad), (
                    ".%s pins to the %s of .%s, but .%s has padding-%s: %s. That is %s of "
                    "live scrolling column in front of a box whose whole job is to cover "
                    "it — content shows through it and, in the right column, answers can "
                    "be clicked there with the statement off screen."
                    % (cls, edge, holder, holder, edge, pad, pad))
    assert checked >= 2, (
        "only %d pinned column edge(s) were checked; the sticky boxes are no longer inside "
        "the columns this test knows about" % checked)


def test_the_player_is_sized_for_a_short_window_as_well_as_a_tall_one():
    """414px of an 854px column was 48% of the rater's working space spent on a
    talking head — and it was a bare `46vh`, so on the 700px window a laptop
    gives you it still took 46% of a column that had less to give. Height is the
    whole budget here: the element is far wider than the picture (a 4:3 webcam is
    pillarboxed inside a ~990px-wide slot), so the only thing a height cap costs
    is face size.

    Measured after the change, in Chrome: 288px at 1500x900, 256px at 1280x800,
    224px at 1101x700 — and at every scroll depth in each, 100% of the player
    still on screen. This asks for the shape that produces that: a cap bounded
    at BOTH ends, so the player cannot grow back into the transcript on a tall
    display or shrink to a thumbnail on a short one.
    """
    css = _css(_console_src())
    caps = [v for v, _at, _i in _declared(css, "video", "max-height")]
    assert caps, "the player has no height cap at all"
    cap = caps[-1]
    assert "clamp(" in cap, (
        "the player's height is `%s`: a bare viewport fraction is the same 46%% of the "
        "column at every window size, which was the problem. It needs a floor and a "
        "ceiling" % cap)
    inner = cap.partition("clamp(")[2].rpartition(")")[0]
    floor, _, rest = inner.partition(",")
    ceiling = rest.rsplit(",", 1)[-1].strip()
    assert floor.strip().endswith("px") and ceiling.endswith("px"), cap
    assert 150 <= int(re.sub(r"\D", "", floor)) <= 240, (
        "the floor is %s: below about 150px a talking head is a thumbnail, and several "
        "ESCI items are about what a face was doing" % floor)
    assert 260 <= int(re.sub(r"\D", "", ceiling)) <= 360, (
        "the ceiling is %s: much above this and the transcript is back below the fold on "
        "the windows raters actually use" % ceiling)


# --------------------------------------------------------------------------- #
# the preamble stops standing in front of the work
# --------------------------------------------------------------------------- #

LAYOUT_HARNESS = r"""/* Opens three encounters in a row in static/rater.html's own script and reports
   what state the two collapsible blocks were left in, what the page stored, and
   where the two columns were scrolled to.

   Storage is real here (a Map, seeded per run) rather than the no-op the banner
   harness uses, because the whole question this asks is what the console
   REMEMBERS between encounters. Elements carry a scrollTop and an `open` so the
   two behaviours under test are observable; everything else is the same thin
   stub, and what it does not model, it does not claim to have exercised. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const stub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const m = html.match(/<script>([\s\S]*)<\/script>/);
assert(m, 'no script block in the console');

const written = {};
const store = new Map(Object.entries(stub.storage || {}));

function el(id) {
  return {
    id, style: {}, dataset: {}, value: '', disabled: false, className: '',
    scrollTop: 0, open: undefined,
    set innerHTML(v) { written[id] = v; }, get innerHTML() { return written[id] || ''; },
    set textContent(v) { written[id + ':text'] = v; }, get textContent() { return written[id + ':text'] || ''; },
    classList: { add() {}, remove() {}, toggle() {} },
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
    fire(ev) { (this.listeners[ev] || []).slice().forEach(f => f({ type: ev })); },
    focus() {}, scrollIntoView() {}, appendChild() {},
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
}

const els = {};
const ctx = {
  console, JSON, Math, Date, Object, Array, String, Number, Boolean,
  parseInt, parseFloat, isNaN, isFinite, URLSearchParams, encodeURIComponent, Promise, Error,
  document: {
    getElementById: (id) => (els[id] = els[id] || el(id)),
    addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    hidden: false, activeElement: null,
  },
  window: { addEventListener() {}, scrollTo() {} },
  location: { search: '?token=rt_' + 'a'.repeat(32), pathname: '/rate' },
  performance: { now: () => Date.now() },
  setInterval() {}, setTimeout: (f) => f(),
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  alert() {}, confirm: () => true,
  CSS: { escape: (s) => s },
  Event: class { constructor(t) { this.type = t; } },
  fetch: async (url) => {
    const u = String(url).split('?')[0];
    let body = null, status = 200;
    if (u.endsWith('/api/rater/me')) body = stub.me;
    else if (u.endsWith('/api/rater/assignments')) body = stub.assignments;
    else if (u.includes('/api/rater/packet/')) { body = stub.packets[u.split('/').pop()]; if (!body) status = 404; }
    else status = 404;
    return { ok: status < 400, status, json: async () => body };
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(m[1], ctx, { filename: 'rater.html' });

/* Always through getElementById, never off `els` directly. `els` only holds
   what the console has already asked for, so reaching into it reports "this
   console has no such element" as a TypeError in the harness — which is a
   crashed test rather than a failed one, and hides the very difference these
   tests exist to measure when they are run against an older console. */
const get = (id) => ctx.document.getElementById(id);

const snap = (label) => {
  const task = get('taskText'), sit = get('situation');
  return {
    label,
    // `null` where the block is not a disclosure at all: "no open state" and
    // "closed" are different facts and the tests read them differently.
    task_open: task.open === undefined ? null : !!task.open,
    situation_open: sit.open === undefined ? null : !!sit.open,
    media_scroll: get('mediaCol').scrollTop,
    form_scroll: get('formCol').scrollTop,
  };
};

(async () => {
  await new Promise(r => setTimeout(r, 0));
  const [A, B, C] = stub.assignments.map(a => a.assignment_id);
  const out = [];

  await ctx.openAssignment(A);
  out.push(snap('first'));

  // The rater reads the scenario, closes it, scrolls both columns down through
  // the encounter, and moves on.
  //
  // Guarded on the card actually being a disclosure. Writing `.open = false` at
  // a console where the card is a plain div would leave the property sitting
  // there and every later snapshot would report a collapsed card that no such
  // console has — the test would then fail against that console for a reason
  // the harness invented.
  if (get('situation').open !== undefined) get('situation').open = false;
  get('mediaCol').scrollTop = 900;
  get('formCol').scrollTop = 1200;

  await ctx.openAssignment(B);
  out.push(snap('second'));

  // ...and now they decide they do want the instructions after all.
  get('taskText').open = true;
  get('taskText').fire('toggle');

  await ctx.openAssignment(C);
  out.push(snap('third'));

  console.log('RESULT ' + JSON.stringify({
    snaps: out,
    storage: Object.fromEntries(store),
    situation_markup: written.situation || '',
  }));
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
"""


def _run_layout(tmp_path, storage=None) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the console harness needs it")
    names = ["as_layout_1", "as_layout_2", "as_layout_3"]
    packets = {a: _packet(a, dict(CENSUS)) for a in names}
    stub = {
        "me": {"rater_id": "rtr_ab12cd34", "name": "R. Okonkwo", "kind": "trained",
               "assignments_pending": len(packets)},
        "assignments": [{"assignment_id": a, "rating_code": p["rating_code"],
                         "status": "pending", "assigned_at": "2026-04-01T14:05:00Z"}
                        for a, p in packets.items()],
        "packets": packets,
        "storage": storage or {},
    }
    stub_path = tmp_path / "layout_stub.json"
    stub_path.write_text(json.dumps(stub), encoding="utf-8")
    harness = tmp_path / "layout_harness.js"
    harness.write_text(LAYOUT_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(CONSOLE), str(stub_path)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hit = re.search(r"^RESULT (.*)$", proc.stdout, re.M)
    assert hit, proc.stdout + proc.stderr
    return json.loads(hit.group(1))


@pytest.fixture(scope="module")
def sitting(tmp_path_factory):
    """Three encounters opened back to back in a browser that has never seen
    this console before."""
    return _run_layout(tmp_path_factory.mktemp("rater_layout"))


# The blocks that stand between the top of the right-hand column and statement
# 1, and what each costs the column: its rendered height plus the gap under it.
#
# These are measurements, taken in Chrome against the demo wave at 1500x900 and
# again at 1101x700 — identical, because the right column is a fixed 440px in
# both and these heights are set by width, not by window height. They are inputs
# the test owns. What the test EXERCISES is the page's own doing: which blocks
# stand above the statements, in what order, and which of them the console
# leaves open for a rater who has opened an encounter before. Those are the two
# things that regress; the pixel heights are here so the answer can be stated in
# the unit the rater experiences.
BLOCK_COST = {
    ("licenceNotice", None): 173,   # 163 tall + 10 of gap
    ("taskText", "open"):    223,   # 213 + 10 — the full instructions
    ("taskText", "shut"):     71,   #  61 + 10 — the one-line summary of them
    ("taskText", None):      295,   # 283 + 12 — not a disclosure at all; how it shipped
    ("scaleNote", None):      82,   #  72 + 10
    ("legend", None):        110,   # 103 + 7
}
FORM_PADDING_TOP = 16
FIRST_STATEMENT_HEIGHT = 79     # the tallest of the 22 at a 440px column
# A 700px-tall window — a 13" laptop with a dock, or the console beside another
# window, which is how researchers and raters actually run it. The app bar is
# 45px, so the scrolling column gets the rest.
COLUMN_ON_A_SHORT_SCREEN = 655


def _preamble_blocks():
    """The direct children of the right column that stand above the statements.

    Direct children only, and in source order. Nesting matters: before this
    round the scale note lived INSIDE the task block, and a flat scan of ids
    would have counted it twice.
    """
    nodes = _elements()
    column = next(n for n in nodes if "form" in n["classes"])
    blocks = []
    for n in nodes:
        if n["parent"] is not column:
            continue
        key = n["id"] or (n["classes"][0] if n["classes"] else n["tag"])
        if key == "items":
            break
        blocks.append((key, n["tag"]))
    return blocks


def _preamble_height(task_open: bool) -> int:
    total = FORM_PADDING_TOP
    for key, tag in _preamble_blocks():
        variant = None
        if tag == "details":
            variant = "open" if task_open else "shut"
        cost = BLOCK_COST.get((key, variant))
        assert cost is not None, (
            "%r is above the statements and has no measured cost in this test. Either it "
            "is new — in which case the preamble has grown and that is the thing to look "
            "at — or it changed shape. Measure it in a browser and put the number here; "
            "do not delete the assertion." % ((key, tag, variant),))
        total += cost
    return total


def test_statement_one_is_on_the_first_screen_of_a_short_window(sitting):
    """The right column's actual job starts 604px down.

    Licence notice, task instructions and legend came to 558px of preamble
    before statement 1 — 71% of an 855px column — and every pixel of it except
    the legend is material a rater has already read: the same licence notice and
    the same instructions on all 27 encounters. On the 700px-tall window a
    laptop gives you that put statement 1 off the first screen: 604 + 79 = 683
    against a 655px column, so the rater scrolled before they could answer
    anything, on every encounter.

    The instructions collapse after the first encounter (see the next two tests
    for exactly when, and for the fact that a first-time rater still meets them),
    which is what buys this back: measured after the change, statement 1 sits at
    442 and is fully on screen in a 655px column with room for the next one.
    """
    second = [s for s in sitting["snaps"] if s["label"] == "second"][0]
    top = _preamble_height(task_open=second["task_open"])
    assert top + FIRST_STATEMENT_HEIGHT <= COLUMN_ON_A_SHORT_SCREEN, (
        "statement 1 runs from %d to %d in a %dpx column, so a rater on a 700px-tall "
        "window has to scroll before they can answer anything. Preamble: %r"
        % (top, top + FIRST_STATEMENT_HEIGHT, COLUMN_ON_A_SHORT_SCREEN, _preamble_blocks()))


def test_a_rater_who_has_never_read_the_instructions_meets_them(sitting):
    """The trade the collapse is allowed to make, and the one it is not.

    Collapsing what a rater has read is the point. Collapsing what they have
    NOT read hands somebody a 22-item instrument with the instructions folded
    away behind a control they have no reason to touch — and a rater who never
    learns they are scoring the participant rather than the counterpart does not
    produce a noisy rating, they produce a confidently wrong one. So the very
    first encounter a browser opens has them open, and it costs what it costs.
    """
    first = [s for s in sitting["snaps"] if s["label"] == "first"][0]
    # `None` is "not a disclosure at all", which satisfies this as completely as
    # "open" does — the requirement is that the instructions are in front of a
    # first-time rater, not that a disclosure exists. This is a constraint the
    # collapse has to live inside, so it holds before the collapse was added
    # too, and it is meant to.
    assert first["task_open"] in (True, None), (
        "the task instructions were folded away on the first encounter this browser has "
        "ever opened; nothing on screen has told this rater what they are scoring")


def test_the_instructions_collapse_once_they_have_been_read_and_the_rater_can_overrule_it(sitting):
    """Read once a sitting, not once an encounter — and the rater has the last
    word in both directions.

    The middle assertion is the one with history. <details> fires `toggle`
    asynchronously, so the console's first attempt to tell its own writes apart
    from the rater's used a flag that was already back to false by the time the
    event arrived; and the HTML parser setting `open` at load time fires one
    more before any encounter exists. Between them, every sitting recorded "the
    rater wants these open" on encounter one and the collapse never happened
    again on that machine — the feature silently off, with the storage key sat
    there looking like a decision somebody made.
    """
    snaps = {s["label"]: s for s in sitting["snaps"]}
    assert snaps["first"]["task_open"] is not None, (
        "the task instructions are not a collapsible block at all, so they stand at full "
        "height in front of statement 1 on all 27 encounters of a rater's queue: %r" % (snaps,))
    assert snaps["first"]["task_open"], snaps
    assert not snaps["second"]["task_open"], (
        "the instructions were still open on the second encounter, so every encounter "
        "after the first still opens behind a screen of text the rater has read: %r"
        % (sitting,))
    assert snaps["third"]["task_open"], (
        "the rater re-opened the instructions and the console closed them again on the "
        "next encounter. An explicit choice has to outlive the encounter: %r" % (sitting,))


def test_the_scenario_card_opens_on_every_encounter(sitting):
    """The other collapsible is not the same kind of thing, and must not be
    treated as one.

    The task instructions are identical on all 27 encounters. The scenario card
    is the briefing THIS participant was holding — new material every time — so
    a card that remembered "closed" would hand the rater the next scenario
    already hidden. Closing it is a decision about the encounter in front of
    them and it lasts exactly that long.
    """
    for s in sitting["snaps"]:
        # As above, `None` — not a collapsible at all — satisfies this. The
        # rule is "the next scenario is on screen", and the harness closed this
        # card on the first encounter precisely to see whether that stuck.
        assert s["situation_open"] in (True, None), (
            "the scenario card opened collapsed on the %s encounter, after the rater "
            "closed it on a previous one — that is a different scenario they have not "
            "read: %r" % (s["label"], sitting))
    # And where it IS a disclosure, renderSituation has to be the thing that
    # reopens it, not the markup: #situation's contents are rebuilt on every
    # encounter but the element itself is not, so the `open` attribute survives
    # unless something puts it back.
    if sitting["snaps"][0]["situation_open"] is not None:
        body = _console_src().split("function renderSituation()", 1)[1].split("\nfunction ", 1)[0]
        assert re.search(r"\$\('situation'\)\.open\s*=\s*true", body), (
            "renderSituation does not reopen the scenario card, so it is one rater's "
            "click away from being hidden for the rest of their queue")


def test_opening_the_next_encounter_starts_both_columns_at_the_top(sitting):
    """The columns are the scrollers, and nothing reset them.

    `window.scrollTo(0, 0)` was all the console did between encounters, and the
    window never scrolls here — the two columns do. So every encounter after the
    first opened wherever the previous one had been left. Measured at 1500x900:
    opening the next assignment landed the rater 638px into a transcript they
    had not read and 900px into a questionnaire they had not answered, past the
    scenario card entirely.
    """
    for s in sitting["snaps"]:
        assert s["media_scroll"] == 0 and s["form_scroll"] == 0, (
            "the %s encounter opened at media=%s form=%s, mid-way through material the "
            "rater has not seen" % (s["label"], s["media_scroll"], s["form_scroll"]))


# --------------------------------------------------------------------------- #
# what a collapsed block is still obliged to do
# --------------------------------------------------------------------------- #

def test_both_collapsibles_are_native_disclosures_a_keyboard_can_reach():
    """A div with a click listener is unreachable by Tab, deaf to Enter and
    Space, and silent to a screen reader — the same defect this audit found on
    the evidence trace's session rows and transcript turns.

    <details>/<summary> is in the tab order, answers Enter and Space, and reports
    its own expanded state, for no script at all. Verified in Chrome: from the
    Queue button, one Tab reaches the scenario card's summary, and a click on
    either summary toggles it and is recorded.
    """
    src = _console_src()
    for block_id in ("situation", "taskText"):
        opening = re.search(r"<(\w+)[^>]*\bid=\"%s\"" % block_id, src)
        assert opening and opening.group(1) == "details", (
            "#%s is a <%s>; a collapsible built out of a div and a click listener cannot "
            "be reached by Tab or operated by Enter" % (block_id, opening and opening.group(1)))
    # And the marker each one hangs its affordance on is a <summary>, not a bare
    # pseudo-element: a summary with no text of its own announces as an unnamed
    # control.
    assert "<summary" in src.split('id="taskText"', 1)[1][:600], (
        "the task disclosure has no <summary>")


def test_the_collapsed_instructions_still_say_what_the_rater_is_doing(sitting):
    """A folded block that reads "Instructions" is a stub, not an instruction.

    Closed, the summary has to carry the whole ask — which participant to rate,
    and against what — because for 26 of the 27 encounters in a rater's queue
    this line IS the instruction on screen.
    """
    src = _console_src()
    task = src.split('id="taskText"', 1)[1]
    summary = task.split("<summary", 1)[1].split("</summary>", 1)[0]
    shut = re.search(r'class="sum-shut"[^>]*>(.*?)</span>', summary, re.S)
    assert shut, "the closed state of the instructions has no line of its own: " + summary[:300]
    text = re.sub(r"<[^>]+>", "", shut.group(1))
    text = " ".join(text.split())
    assert "participant" in text.lower(), text
    assert re.match(r"^(Rate|Score|Judge)\b", text), (
        "the closed instruction does not tell the rater to do anything: %r" % text)
    assert 30 <= len(text) <= 140, (
        "the closed instruction is %d characters; it has to be one line, and it has to be "
        "an instruction: %r" % (len(text), text))


def test_the_licence_notice_is_not_behind_a_click():
    """The proprietary-instrument warning stays on the page, in full.

    It is there because the audit put it there: these are ESCI items reproduced
    for research reference, and the licensing position has to be in front of
    whoever is reading them. Compacting its leading is fine. Folding it into a
    disclosure a rater can leave shut forever is the one thing it must never
    become — and neither is the packet's own copy of the notice, which is the
    instrument document's wording and the version that matters if the two ever
    drift.
    """
    src = _console_src()
    for key, tag in _preamble_blocks():
        if key == "licenceNotice":
            assert tag != "details", "the licence notice has been folded into a disclosure"
            break
    else:
        raise AssertionError("the licence notice is no longer above the statements: %r"
                             % (_preamble_blocks(),))
    # ...and it is not inside somebody else's disclosure either.
    folded = [a for a in _ancestors(_by_id("licenceNotice")) if a["tag"] == "details"]
    assert not folded, (
        "the licence notice sits inside a <details>; a rater can collapse it and never "
        "open it again: %r" % (folded,))
    notice = src.split('id="licenceNotice"', 1)[1].split("</div>", 1)[0].lower()
    for word in ("proprietary", "licens", "boyatzis", "redistribute"):
        assert word in notice, "the licence notice lost %r while being compacted" % word
    assert 'id="packetNotice"' in src, "the packet's own copy of the notice is gone"


def test_the_layout_work_did_not_widen_what_the_rater_can_see():
    """Blinding is the constraint the rest of this round sits inside.

    A rater must not be shown the stage directions, the planted triggers, the
    ESCI tags on them, the actor briefs, the automated score, the participant or
    the session. Rearranging a column is not a reason for any of that to appear,
    and "there was room once the preamble collapsed" is exactly the argument
    that would put it there.
    """
    src = _console_src()
    for leak in ("/evidence", "stage_direction", "trigger_id", "actor_brief",
                 "auto_score", "participant_id", "session_id"):
        assert leak not in src, "the rating console now reaches for %r" % leak
    # packet.construct is the scenario's primary-competency designation and the
    # instrument requires raters to be blind to it. It is in the packet for the
    # server; this page ignores it on purpose.
    assert not re.search(r"packet\.construct\b(?!\` is)", src.split("<script>", 1)[1]), (
        "the packet's construct reached the page")


def test_a_packet_blocked_for_no_items_cannot_be_unblocked_by_a_video_retry():
    """blockedReason holds one reason, and only one of them is retryable.

    openAssignment runs renderVideo then renderItems, so a packet with no items
    settles on 'no-items' — which retryPlayback correctly refuses to lift. But
    videoUnplayable fires LATER and asynchronously, off a media error event, and
    it used to overwrite whatever was there with 'no-playback'. "Try again" then
    lifted that and re-enabled submit, on a packet with no statements to answer.

    Reported by a verifier from a read of the code; it could not be reached in
    the demo wave because rater_packet always emits 22 items. That makes it
    unreachable today and one malformed packet away tomorrow, which is exactly
    the kind of guard worth having before a wave rather than after one.
    """
    src = CONSOLE.read_text(encoding="utf-8")
    fn = src[src.index("function videoUnplayable("):]
    fn = fn[:fn.index("\n}")]
    assert "if (!blockedReason) blockedReason = 'no-playback'" in fn, (
        "videoUnplayable overwrites an existing block, so a stronger reason can "
        "be downgraded to the one reason a retry is allowed to clear")

    # And the retry still only ever lifts the retryable one.
    retry = src[src.index("function retryPlayback("):]
    retry = retry[:retry.index("\n}")]
    assert "blockedReason === 'no-playback'" in retry, (
        "retryPlayback no longer checks which block it is lifting")
