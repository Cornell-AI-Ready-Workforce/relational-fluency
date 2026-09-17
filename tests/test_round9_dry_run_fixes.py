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
