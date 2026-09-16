"""The browser half of putting the application back in the video byte path.

`tests/test_video_route.py` covers the server: the route that serves a
recording to a rater and the one that takes a recording from a participant.
This file covers the two pages that talk to them — `static/v2.html`, which
captures the recording, and `static/rater.html`, which plays it — because the
defect they exist to close is a client-side one and no server test can see it.

The defect, stated once: the browser PUT the recording straight to S3 and the
rater GET it straight from S3, so the application never touched the bytes. On a
machine with no AWS credentials the presign fails, every attempt fails with it,
and the recording dies with the page. That is every developer laptop and every
CI run, which is why nobody has ever watched a webcam recording in the rating
console — not the researcher, not the maintainer, not CI.

The harness is the one `tests/test_client_blockers.py` already builds: each
page's inline script is run in a Node vm against a thin DOM, a routed fetch and
a virtual clock, and the assertions are made by driving the page's own
functions and reading what it did to the network. It is imported rather than
copied so that a fix to the stub reaches both files, and so that these pages are
only ever exercised one way.

Run from the repo root:

    python -m pytest tests/test_video_client.py
"""

from __future__ import annotations

import re
from pathlib import Path

# The stub and the runner belong to the sibling module rather than being copied
# here: two divergent copies of a browser stub is how a page comes to pass in
# one file and fail in the other for reasons that have nothing to do with the
# page. `_run` writes DOM_STUB out as stub.js beside each harness, so importing
# it is also what makes `require('./stub.js')` below resolve.
from test_client_blockers import _run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "static" / "v2.html"


# ==========================================================================
# U1 — the participant's fallback upload (static/v2.html)
#
# uploadRecording did presign -> PUT-to-S3 -> confirm, twice, and then gave up.
# When presigning is unavailable it now PUTs the same blob to this server's own
# /api/sessions/{id}/video instead. The judgement in that sentence is "when",
# and most of what follows is about the cases where it must NOT fall back.
# ==========================================================================

def _v2_source() -> str:
    return V2.read_text(encoding="utf-8")


def test_the_page_can_address_this_server_for_the_upload():
    """The fallback URL is built from the same two pieces as the presign URL.

    The upload route applies the presign route's authorisation rules exactly —
    the participant key check and the session-owner check — so a fallback that
    dropped `participant_id`, or that put it in before the key, would answer 403
    for every participant who has one and the recording would be lost for the
    second time in the same chain.
    """
    src = _v2_source()
    urls = src[src.index("function videoUrls("):src.index("function presigningIsUnavailable(")]
    assert "upload: `/api/sessions/${encodeURIComponent(sid)}/video`" in urls, \
        "videoUrls no longer builds the fallback upload URL"
    # All three endpoints get the same key-then-participant tail, from the same
    # two variables, in the same order.
    tails = re.findall(r"\}/[a-z-]+` \+ (keyParamFirst \+ pidQ),", urls)
    assert len(tails) == 3 and len(set(tails)) == 1, \
        "the three upload URLs no longer share one query-string construction"


def test_the_fallback_put_is_bounded_like_the_one_it_replaces():
    """B4's rule reaches the new leg too: a request with no deadline is a
    promise the completion overlay waits on forever. The fallback carries a
    whole recording, so it gets the PUT's bound and not the presign's."""
    src = _v2_source()
    body = src[src.index("async function uploadViaApp("):]
    body = body[:body.index("\n  }") + 4]
    assert "fetchWithDeadline(" in body and "PUT_TIMEOUT_MS" in body, \
        "the fallback upload is an unbounded fetch, or bounded by the wrong deadline"
    assert not re.search(r"(?<!With)(?<!\w)fetch\(", body.replace("fetchWithDeadline(", "")), \
        "a request in the fallback bypasses fetchWithDeadline"


def test_the_fallback_is_decided_per_reason_code_in_one_named_place():
    """Not "the S3 leg failed, try the other one". The exclusions are the whole
    judgement, and they have to be readable as a list rather than inferred from
    a condition inlined into the chain."""
    src = _v2_source()
    assert "function presigningIsUnavailable(" in src
    pred = src[src.index("function presigningIsUnavailable("):]
    pred = pred[:pred.index("\n  }") + 4]
    # A predicate that admitted put_http_* would send a recording S3 refused
    # over a second route that shares the refusal.
    assert "put_http" not in pred, \
        "the fallback predicate now looks at the PUT leg's status"


UPLOAD_FALLBACK_HARNESS = r"""/* static/v2.html's upload chain when the bucket cannot be reached at all.

   Driven, not read: the page's own finishVideoRecording is called with a
   recorder that stops, and what the page then does to the network — which
   routes it calls, how many times, carrying what — is the assertion. */
'use strict';
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');

const PAGE = process.argv[2];

// Substring routes, and the order they are declared in matters: the fallback's
// '/api/sessions/<sid>/video' is a prefix of both other URLs, so it is matched
// on something neither of them contains — the '?' that follows it immediately,
// where the other two have a longer path segment first.
const PRESIGN = '/video-upload-url', CONFIRM = '/video-uploaded';
const APP = '/video?', S3 = 'https://s3.invalid/';
const SID = 's_1772460300_44c9a2';

function armRecorder(b) {
  b.sandbox.__rec = { state: 'recording', onstop: null,
                      stop() { const f = this.onstop; if (f) f(); } };
  b.sandbox.__chunk = { size: 45 * 1024 * 1024 };   // a ten-minute encounter
  vm.runInContext("videoRecorder = __rec; videoChunks = [__chunk];" +
                  "videoMime = 'video/webm;codecs=vp8,opus';" +
                  "sessionId = '" + SID + "'; participantId = 'p_test';", b.ctx);
}

const transcript = (b) =>
  b.dom.document.getElementById('transcript').children.map(c => c.textContent).join(' | ');

async function upload(b, ms) {
  const p = b.ctx.finishVideoRecording();
  assert(p, 'finishVideoRecording returned null with a live recorder');
  let out = null;
  p.then(v => { out = v; });
  await b.clock.advance(ms === undefined ? 600000 : ms);
  assert(out !== null, 'the upload promise never settled — the caller waits forever');
  return out;
}

(async () => {
  // --- no credentials: the recording goes to this server instead -----------
  // The case the whole change exists for. 503 from the presign route is what a
  // machine with no AWS credentials answers on every single run, so before this
  // leg existed every recording made on one died with the page.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(503, { detail: 'recording storage unavailable (NoCredentialsError)' }) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(200, { ok: true, bytes: 45 * 1024 * 1024 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, true,
      'an unreachable bucket still lost the recording: ' + JSON.stringify(out));
    assert.strictEqual(out.via, 'app',
      'the page did not report which path carried it: ' + JSON.stringify(out));
    assert.strictEqual(b.net.countOf(APP), 1, 'the fallback was not used exactly once');
    const url = b.net.urlsOf(APP)[0];
    assert(url.startsWith('/api/sessions/' + SID + '/video'),
      'the fallback did not address this session on this server: ' + url);
    assert(/participant_id=p_test/.test(url),
      'the fallback dropped the participant the route authorises against: ' + url);
    const call = b.net.calls.find(c => c.url.includes(APP));
    assert.strictEqual(call.method, 'PUT', 'the fallback did not PUT the recording');
    assert(!/could not be saved/.test(transcript(b)),
      'a recording that WAS saved told the participant it was lost: ' + transcript(b));
  }

  // --- and it does not then confirm it away --------------------------------
  // The confirm endpoint answers one question, with a HEAD against S3: is the
  // object in the bucket? On this path it deliberately is not, so a confirm
  // would append a later video_uploaded event with status "failed", and every
  // reader of that trail takes the last one as the verdict. The fallback would
  // have saved the recording and made it unrateable in the same breath.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(503, {}) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(200, { ok: true, bytes: 45 * 1024 * 1024 }) },
    ]);
    await upload(b);
    assert.strictEqual(b.net.countOf(CONFIRM), 0,
      'a locally stored recording was confirmed against a bucket that does not have it');
    // Nor by the other way out. Nothing here may report this encounter as a
    // recording that was lost, or as one that never had a camera.
    for (const u of b.net.calls.map(c => c.url).concat(b.beacons)) {
      assert(!/client_error=/.test(u), 'a successful fallback reported a client error: ' + u);
      assert(!/no_camera=/.test(u), 'a successful fallback reported the camera as absent: ' + u);
    }
  }

  // --- a 409 from the one-shot guard is a recording, not a failure ---------
  // The guard refuses a second write only when playable bytes were FOUND for
  // this encounter. Re-sending would overwrite a recording; reporting a failure
  // would block a rateable one.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(503, {}) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(409, { detail: 'video already uploaded' }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, true,
      'an existing recording was reported as a loss: ' + JSON.stringify(out));
    assert.strictEqual(b.net.countOf(APP), 1, 'the blob was re-sent over an existing recording');
    assert.strictEqual(b.net.countOf(CONFIRM), 0, 'a 409 from the app route still ran the confirm');
  }

  // --- a 200 with no url in it is also "no URL was issued" -----------------
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(200, null) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(200, { ok: true, bytes: 45 * 1024 * 1024 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, true, 'a broken presign contract still lost the recording');
    assert.strictEqual(out.via, 'app');
  }

  // --- S3 refused the PUT: NOT a reason to try the app route ---------------
  // The presign worked, so this server has credentials and the bucket
  // answered; 403 is S3's own policy, or a signature that expired. The fallback
  // shares the credentials that produced it and fixes none of it, so trying
  // turns one clear failure into two confusing ones.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(200, { url: S3 + 'obj' }) },
      { match: S3, fn: () => b.net.res(403, {}) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(200, { ok: true, bytes: 4096 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(b.net.countOf(APP), 0,
      'a PUT that S3 refused was re-sent to the app route');
    assert.strictEqual(out.reason, 'put_http_403', 'wrong reason: ' + out.reason);
    assert(/client_error=put_http_403/.test(b.net.urlsOf(CONFIRM)[0]),
      'the confirm no longer carries the S3 refusal: ' + b.net.urlsOf(CONFIRM)[0]);
  }

  // --- a throttled bucket is the same call ---------------------------------
  // 503 from S3 is transient and the two S3 attempts already cover it; storing
  // this one recording somewhere else would divide a wave between two places
  // with nothing recording which encounter went where.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(200, { url: S3 + 'obj' }) },
      { match: S3, fn: () => b.net.res(503, {}) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(200, { ok: true, bytes: 4096 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(b.net.countOf(APP), 0, 'a throttled bucket fell back to the app route');
    assert.strictEqual(out.reason, 'put_http_503', 'wrong reason: ' + out.reason);
  }

  // --- a refusal of THIS participant is not a storage failure --------------
  // 401/403 is check_participant or the session-owner check; 404 is a session
  // this server does not have. The upload route applies both rules identically,
  // so it can only reach the same refusal one request later.
  for (const status of [401, 403, 404]) {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(status, {}) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(200, { ok: true, bytes: 4096 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(b.net.countOf(APP), 0,
      'a ' + status + ' from the presign route was retried against the app route');
    assert.strictEqual(out.reason, 'presign_http_' + status, 'wrong reason: ' + out.reason);
  }

  // --- a server this page cannot reach at all ------------------------------
  // The fallback goes to the same host over the same link. There is nothing to
  // be gained by re-sending tens of megabytes to find that out, and the time is
  // spent by a participant sitting on the completion overlay.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => new Error('connection refused') },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(200, { ok: true, bytes: 4096 }) },
    ]);
    const out = await upload(b);
    assert.strictEqual(b.net.countOf(APP), 0, 'an unreachable server was asked twice more');
    assert.strictEqual(out.reason, 'network', 'wrong reason: ' + out.reason);
  }

  // --- both legs failed: the trail names both ------------------------------
  // "The bucket was unreachable" and "and this server could not take it
  // either" are two findings about a wave, and the second is the one that says
  // the problem is not AWS. Two attempts at the fallback, the same discipline
  // the S3 leg gets.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(503, {}) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => b.net.res(507, {}) },
    ]);
    const out = await upload(b);
    assert.strictEqual(out.ok, false, 'a recording that reached neither store was reported as saved');
    assert.strictEqual(out.reason, 'presign_http_503', 'wrong reason: ' + out.reason);
    assert.strictEqual(out.fallback, 'app_http_507',
      'the fallback leg has no verdict of its own: ' + JSON.stringify(out));
    assert.strictEqual(b.net.countOf(APP), 2, 'the fallback was not retried');
    const url = b.net.urlsOf(CONFIRM)[0];
    assert(/client_error=presign_http_503\.app_http_507/.test(url),
      'the confirm does not name both legs: ' + url);
    assert(/could not be saved/.test(transcript(b)),
      'a recording lost by both routes was not reported to the participant');
  }

  // --- the fallback's deadline is the PUT's, not the presign's -------------
  // It carries the whole recording. Bounded by the 15-second presign deadline
  // it would abort every upload over about 10 MB on an ordinary uplink, which
  // is most of them.
  {
    const b = bootV2(PAGE, '?run=r_1');
    armRecorder(b);
    b.net.route([
      { match: PRESIGN, fn: () => b.net.res(503, {}) },
      { match: CONFIRM, fn: () => b.net.res(200, { ok: false, bytes: 0 }) },
      { match: APP, fn: () => 'hang' },
    ]);
    const p = b.ctx.finishVideoRecording();
    let out = null; p.then(v => { out = v; });
    await b.clock.advance(120000);
    assert.strictEqual(b.net.countOf(APP), 1,
      'the fallback gave up well before its deadline: ' + b.net.countOf(APP) + ' attempts by 120s');
    assert.strictEqual(out, null, 'the chain settled while the fallback was still in flight');
    await b.clock.advance(600000);
    assert(out && out.ok === false, 'a hung fallback never settled');
    assert.strictEqual(out.fallback, 'app_timeout',
      'wrong reason for a hung fallback: ' + JSON.stringify(out));
    assert.strictEqual(b.net.countOf(APP), 2, 'the hung fallback was not retried');
  }

  // --- an encounter that never had a camera still reports an absence -------
  // The camera-absent / recording-lost split is what decides whether a rater
  // may score an encounter from the transcript at all. A fallback that ran when
  // there were no bytes, or that turned an absence into a storage failure,
  // would undo it.
  {
    const b = bootV2(PAGE, '?run=r_1');
    b.net.route([]);
    vm.runInContext("videoRecorder = null; videoChunks = [];" +
                    "sessionId = '" + SID + "'; participantId = 'p_test';" +
                    "cameraFailReason = 'NotAllowedError';", b.ctx);
    assert.strictEqual(b.ctx.finishVideoRecording(), null,
      'an encounter with no recorder returned an upload to wait on');
    await b.clock.advance(10000);
    assert.strictEqual(b.net.calls.length, 0, 'an encounter with no camera made a request');
    assert(b.beacons.some(u => /no_camera=NotAllowedError/.test(u)),
      'the camera absence was not reported as an absence: ' + JSON.stringify(b.beacons));
    for (const u of b.beacons) {
      assert(!/client_error=/.test(u), 'an absent camera was reported as a lost recording: ' + u);
    }
  }

  console.log('FALLBACK OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def test_a_machine_with_no_credentials_still_keeps_the_recording(tmp_path):
    """The whole point, driven through the page: with the presign route
    answering the 503 a credential-free machine answers on every run, the
    recording reaches this server rather than dying with the page — and the
    cases where falling back would be wrong still fail exactly as they did."""
    assert "FALLBACK OK" in _run(tmp_path, UPLOAD_FALLBACK_HARNESS, V2)


# ==========================================================================
# C1 — the rating console's player (static/rater.html)
#
# The console used to point a <video> at a presigned S3 GET the packet carried.
# That one decision produced four of the audit's findings, and this section is
# mostly about their disappearance: the URL now comes from this application's
# own route, so it does not expire, it is not a storage credential, and it
# resolves on a machine that has never heard of AWS. What is left of the old
# subsystem — a pre-emptive re-mint, a 20-second gap, a cap of six, and a retry
# button that existed to recover from the cap — is nothing.
#
# The second half is the scrubber. `packet.duration_s` is computed from the
# event trail at session close and was being spent on a text label, while the
# media element reported `duration: Infinity` and the browser drew a position
# bar to match. A rater who cannot scrub cannot rate efficiently.
# ==========================================================================

RATER = ROOT / "static" / "rater.html"


def _rater_source() -> str:
    return RATER.read_text(encoding="utf-8")


def test_no_part_of_the_expiry_subsystem_is_left_in_the_console():
    """Every name the re-minting machinery went by, asserted absent.

    Named rather than pattern-matched, because a half-removed subsystem is
    worse than the whole one: a timer that still fires against a function that
    no longer exists throws inside a listener and takes the player's other
    handlers down with it. If one of these comes back, it comes back with a
    reason, and this test is where the reason has to be written down.
    """
    src = _rater_source()
    for name in ("armExpiryRefresh", "refreshVideoUrl", "videoUrlRefreshed",
                 "videoRefreshes", "lastVideoRefreshAt", "videoRefreshTimer",
                 "VIDEO_REFRESH_MAX", "VIDEO_REFRESH_MIN_GAP_MS", "playRetry"):
        assert name not in src, f"the re-mint subsystem still has a {name}"
    # `expires_in` is the packet field the whole thing hung off. Nothing in the
    # console may read it any more, or the mechanism has somewhere to grow back.
    assert "expires_in" not in src, "the console still reads the link's lifetime"


def test_the_console_says_what_the_true_duration_does_not_buy():
    """The comment is the deliverable here, not decoration.

    A correct `duration` fixes the timeline and nothing else: a cue-less WebM
    still has no index, so the browser estimates a byte offset and plays from
    the nearest keyframe at or before it. The next person to read this file will
    otherwise take an accurate timeline for accurate seeking, and file the
    approximation as a bug in this code rather than as the container's.
    """
    src = _rater_source()
    assert "keyframe" in src, "nothing warns that scrubbing lands approximately"
    assert "remux" in src, "the fix that would make it exact is not named"


RATER_PLAYER_HARNESS = r"""/* static/rater.html's player: where it gets its bytes, what it does when they
   do not arrive, and the timeline it draws over them. */
'use strict';
const fs = require('fs'), assert = require('assert');
const { makeContext, vm } = require('./stub.js');

const PAGE = process.argv[2];
const SRC = fs.readFileSync(PAGE, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];

const ITEMS = [1, 2, 3].map(i => ({ id: 'i' + i, text: 'item ' + i }));
// What the packet used to have to carry: a presigned S3 GET with an hour on it.
const SIGNED = { video_url: 'https://study.s3.invalid/encounters/s_1/webcam.webm?X-Amz-Signature=dead',
                 video_available: true, video_status: 'ok', expires_in: 3600 };
// The same recording behind a link that ages out fast, so a sitting long enough
// to outlive two of them fits on the virtual clock.
const SHORT = Object.assign({}, SIGNED, { expires_in: 400 });
// What a deployment with no AWS credentials can say instead: the bytes exist,
// and there is no link because nothing could sign one.
const UNSIGNED = { video_url: null, video_available: true, video_status: 'ok', note: null };

function boot() {
  // A token the page's own format check rejects, so boot() gates without
  // touching the network and the packet under test is the only thing opened.
  const b = makeContext({
    location: { search: '?token=not-a-token', href: 'http://t/rate', reload() {} },
    scrollTo() {},
    CSS: { escape: (s) => s },
  });
  // Tied to the virtual clock rather than pinned at zero: the scrubber's settle
  // window is measured with performance.now, and a frozen clock would make the
  // window either always open or always shut.
  b.sandbox.performance = { now: () => b.clock.now() };
  b.net.route([{ match: '/api/rater/', fn: () => b.net.res(200, {}) }]);
  vm.runInContext(SRC, b.ctx, { filename: 'rater.html' });
  return b;
}

const $ = (b, id) => b.dom.document.getElementById(id);
// The stub records listeners rather than dispatching them, so a test fires by
// hand — which is also how a media event with no user gesture behind it works.
const fire = (el, ev, detail) => (el.listeners[ev] || []).slice().forEach(f => f(detail || { type: ev }));

const packetOf = (media, extra) => Object.assign(
  { assignment_id: 'a_1', status: 'assigned', items: ITEMS,
    transcript: [{ role: 'participant', text: 'hello', t: 12 }], media },
  extra || {});

async function open(packet) {
  const b = boot();
  // Re-routing clears the recorded calls, so every count below is "since this
  // assignment was opened" and the packet fetch itself is the first of them.
  b.net.route([{ match: '/api/rater/packet/', fn: () => b.net.res(200, packet) }]);
  await b.ctx.openAssignment(packet.assignment_id);
  await b.clock.flush();
  return b;
}

(async () => {
  // --- the bytes come from this application, not from the bucket -----------
  {
    const b = await open(packetOf(SIGNED));
    const src = $(b, 'vid').src;
    assert(/^\/api\/rater\/video\/a_1\?token=not-a-token$/.test(src),
      'the player is still pointed at whatever URL the packet carried: ' + src);
    assert(!/X-Amz|s3\.invalid/.test(src),
      'a storage credential is in the page and in the browser history: ' + src);
  }

  // --- and a packet that could not sign anything is still playable ---------
  // This is the credential-free laptop, and the `unsigned` state it produced.
  // The bytes are in the bucket; only the signature was missing, and nothing
  // needs a signature any more.
  {
    const b = await open(packetOf(UNSIGNED));
    assert(/<video/.test($(b, 'videoSlot').innerHTML),
      'a recording with no presigned link was reported as unplayable: ' + $(b, 'videoSlot').innerHTML);
    assert.strictEqual($(b, 'submitBtn').disabled, false,
      'an encounter whose bytes exist was blocked because nothing could sign a link for them');
    assert(/^\/api\/rater\/video\/a_1\?/.test($(b, 'vid').src),
      'the player had no source to fall back on: ' + $(b, 'vid').src);
  }

  // --- a recording that will not play blocks at once, and costs no request -
  // The re-mint this used to try first was for an expired link. There is no
  // expiry, so the first error is the answer: block, and say so.
  {
    const b = await open(packetOf(SIGNED));
    const v = $(b, 'vid');
    v.error = { code: 4 };                 // MEDIA_ERR_SRC_NOT_SUPPORTED
    fire(v, 'error');
    await b.clock.flush();
    assert.strictEqual($(b, 'submitBtn').disabled, true,
      'an undecodable recording was rated around: this produces a gold label made without ' +
      'the artefact and indistinguishable from a good one');
    assert(/cannot be played in this browser/i.test($(b, 'videoSlot').innerHTML),
      'the rater was shown a black box with no explanation: ' + $(b, 'videoSlot').innerHTML);
    assert(/Do not rate/i.test($(b, 'submitMsg').textContent),
      'a rater silently blocked from submitting is worse than one told what happened: ' +
      $(b, 'submitMsg').textContent);
    assert.strictEqual(b.net.countOf('/api/rater/packet/'), 1,
      'the player still re-fetches the packet hunting for a fresh link');
  }

  // --- and the way back out of that block is local -------------------------
  {
    const b = await open(packetOf(SIGNED));
    const v = $(b, 'vid');
    v.error = { code: 2 };                 // MEDIA_ERR_NETWORK: the transfer failed
    fire(v, 'error');
    await b.clock.flush();
    assert.strictEqual($(b, 'submitBtn').disabled, true, 'a failed transfer was rated around');
    assert(/Try again/.test($(b, 'videoSlot').innerHTML),
      'the rater is told not to rate this encounter and given no way to test that claim: ' +
      $(b, 'videoSlot').innerHTML);
    fire($(b, 'videoRetry'), 'click');
    await b.clock.flush();
    assert.strictEqual($(b, 'submitBtn').disabled, false,
      'a recording that plays on the second attempt stayed blocked for the rest of the sitting');
    assert(/<video/.test($(b, 'videoSlot').innerHTML), 'the player was never rebuilt');
    assert.strictEqual(b.net.countOf('/api/rater/packet/'), 1,
      'Try again is still a re-mint: it spends a request on a URL that cannot go stale');
  }

  // --- three hours on one encounter, and nothing re-mints anything ---------
  // The packet still carries expires_in and the console must not care. A timer
  // armed off it is the whole deleted subsystem growing back.
  {
    const b = await open(packetOf(SHORT));
    await b.clock.advance(3 * 60 * 60 * 1000);
    assert.strictEqual(b.net.countOf('/api/rater/packet/'), 1,
      'something is still swapping the playback URL on a timer: ' +
      JSON.stringify(b.net.urlsOf('/api/rater/packet/')));
    assert.strictEqual($(b, 'submitBtn').disabled, false,
      'a sitting long enough to outlive two presigned links blocked the rating');
  }

  // --- the timeline is the packet's, and it is right before the file loads --
  {
    const b = await open(packetOf(SIGNED, { duration_s: 421 }));
    assert.strictEqual($(b, 'vidTimeline').style.display, 'flex',
      'no timeline was drawn from a duration the packet already knew');
    assert.strictEqual($(b, 'vidScrub').max, '421',
      'the scrubber was scaled to something other than the encounter: ' + $(b, 'vidScrub').max);
    assert.strictEqual($(b, 'vidClock').textContent, '0:00 / 7:01',
      'the clock does not show the encounter length: ' + $(b, 'vidClock').textContent);

    // The engine now says what every recording in this study says, and it must
    // not win: Infinity is the absence of an answer, not a better one.
    const v = $(b, 'vid');
    v.duration = Infinity;
    fire(v, 'loadedmetadata');
    assert.strictEqual($(b, 'vidScrub').max, '421',
      "the engine's Infinity overwrote the length the packet was sure of");
    // And the probe still ran, which is the finding this round set out to
    // check: knowing the duration in JS does not make the ENGINE's currentTime
    // assignable, so the seek-past-the-end trick is still the only thing that
    // does, packet.duration_s or not.
    assert(v.currentTime > 1e100,
      'nothing asked the engine to resolve its own duration, so seeking cannot work: ' + v.currentTime);

    v.duration = 421;
    fire(v, 'durationchange');
    await b.clock.advance(1000);
    v.currentTime = 60;
    fire(v, 'timeupdate');
    assert.strictEqual($(b, 'vidClock').textContent, '1:00 / 7:01',
      'the clock does not follow the player: ' + $(b, 'vidClock').textContent);
    assert.strictEqual($(b, 'vidScrub').value, '60', 'the thumb does not follow the player');
  }

  // --- a packet with no duration falls back to the engine ------------------
  // Nothing is drawn against an unknown length: a slider whose end means
  // nothing is worse than the control bar the browser already draws.
  {
    const b = await open(packetOf(SIGNED));
    assert.strictEqual($(b, 'vidTimeline').style.display, 'none',
      'a timeline was drawn with no length to draw it against');
    const v = $(b, 'vid');
    v.duration = 100;
    fire(v, 'durationchange');
    assert.strictEqual($(b, 'vidTimeline').style.display, 'flex',
      'the engine worked the duration out and nothing picked it up');
    assert.strictEqual($(b, 'vidScrub').max, '100', 'the fallback duration was not used');
  }

  // --- dragging the scrubber seeks, and the thumb is not fought ------------
  {
    const b = await open(packetOf(SIGNED, { duration_s: 300 }));
    await b.clock.advance(1000);           // off zero, so the settle window is measurable
    const v = $(b, 'vid'), scrub = $(b, 'vidScrub');
    scrub.value = '120';
    fire(scrub, 'input');
    assert.strictEqual(v.currentTime, 120,
      'the scrubber is a decoration: moving it did not move the player');
    // The seek lands on the keyframe before the moment asked for. Writing that
    // back at once drags the thumb out from under the rater's finger.
    v.currentTime = 118;
    fire(v, 'timeupdate');
    assert.strictEqual(scrub.value, '120', 'the thumb was dragged backwards mid-drag');
    await b.clock.advance(600);
    fire(v, 'timeupdate');
    assert.strictEqual(scrub.value, '118', 'the thumb never resynced with the player');
  }

  // --- a file the engine cannot seek is not left with a live scrubber ------
  {
    const b = await open(packetOf(SIGNED, { duration_s: 421 }));
    const v = $(b, 'vid');
    v.duration = Infinity;
    fire(v, 'loadedmetadata');              // and no durationchange ever comes
    await b.clock.advance(9000);
    assert.strictEqual($(b, 'vidScrub').disabled, true,
      'a dead affordance was left in place: the thumb moves and the player does not');
    assert(/does not\s+work reliably|no duration index/i.test($(b, 'seekHint').textContent),
      'the console still promises click-to-seek on a file that cannot honour it: ' +
      $(b, 'seekHint').textContent);
    // The clock is still true, so it stays.
    assert.strictEqual($(b, 'vidClock').textContent, '0:00 / 7:01',
      'the encounter length was withdrawn along with the seek: ' + $(b, 'vidClock').textContent);
  }

  console.log('PLAYER OK');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def test_the_rating_console_plays_from_this_server_and_scrubs_by_the_packet(tmp_path):
    """The console's whole video surface, driven rather than read.

    Two claims in one harness because they are one change: the player's source
    is an application URL that cannot expire — so the re-mint, the rate limit,
    the cap and their retry button are gone and one failure is reported as a
    failure — and the timeline is drawn from `packet.duration_s`, which is right
    before the first byte arrives and stays right when the engine says
    `Infinity`.
    """
    assert "PLAYER OK" in _run(tmp_path, RATER_PLAYER_HARNESS, RATER)
