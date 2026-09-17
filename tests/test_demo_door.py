"""The demo door: static/demo.html, and the landing page's way to it.

The researcher has to be able to stand in front of their lab and show the
platform. Before this there was one way to do that — GET /test, which mints a
cohort=internal run and redirects to /v2 — and from the moment of the redirect
nothing on the screen said "demo". The encounter looked exactly like a
participant's, because it IS the participant's page. The marks were a cohort
field in a run file and a `test_` prefix on a participant key: both true, both
invisible to the room, and both invisible to the person driving.

So there is a page in front of that entrance now, and this file is what holds it
to the three things it has to be.

1. NOT A SECOND WAY INTO THE STUDY. The door mints nothing itself; every live
   demo goes through /test, so it is cohort=internal before it has an encounter
   and falls out of every ?cohort=study export. On top of that the door forces a
   `demo-` prefix onto the participant key, because a cohort is a field somebody
   has to think to filter on and a key is printed whether they thought of it or
   not.

2. AN HONEST CONSENT RECORD. There is no Qualtrics response behind a demo and
   there never will be. server/storage.py already has the branch for this —
   CONSENT_SOURCE_INTERNAL, keyed on the run's cohort — and the tests here pin
   that a demo arrival lands in it, carries the internal run id as its
   reference, and never borrows the survey's provenance. The 4403 capture gate
   is pinned unchanged in both directions: a demo record that has not consented
   yet is refused exactly as a participant's would be.

3. NO FAILING IN FRONT OF AN AUDIENCE. The replay lane reads recorded
   encounters and nothing else — no gateway, no bucket, no key, no microphone —
   and the tests below hold it to that by reading what the page can call. The
   live lane says what will break BEFORE the button, which on the model this
   study is configured for includes the one thing the researcher will be asked
   about in that room: nto.gemini-live-2.5-flash acknowledges no mid-session
   session.update, so a live demo shows a conversation with no steering in it.

WHAT IS RED HERE WITHOUT THE CHANGE, AND WHAT IS A PIN. Everything about
static/demo.html and the landing page's door is red before it: the file did not
exist and the nav had no such link. The three server-side tests at the bottom
are pins, not repairs — _consent_provenance and the voice socket already behave
this way, and this file is what stops the demo door being the change that
quietly alters them.

Run from the repo root:

    python -m pytest tests/test_demo_door.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import app as appmod

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "static" / "demo.html"
LANDING = ROOT / "static" / "landing.html"


def _demo() -> str:
    assert DEMO.exists(), (
        f"{DEMO} is missing: there is no demo door, so the only way to show "
        "the platform is GET /test, which says nothing on screen about being a "
        "demo")
    return DEMO.read_text(encoding="utf-8")


def _landing() -> str:
    return LANDING.read_text(encoding="utf-8")


def _script(src: str) -> str:
    """The page's inline script, without the markup around it."""
    i = src.index("<script>")
    return src[i + len("<script>"):src.rindex("</script>")]


#: HTML comments, block comments and line comments, in that order. A line
#: comment's pattern deliberately requires the `//` to be preceded by
#: whitespace or a line start, so that the `//` in a URL survives.
_COMMENTS = re.compile(r"<!--.*?-->|/\*.*?\*/|(?<=^)\s*//[^\n]*|(?<=\s)//[^\n]*",
                       re.S | re.M)


def _code(src: str) -> str:
    """The page with every comment taken out.

    The tests below ask what this page can REACH — which entrances it links to,
    which endpoints it calls, where the researcher key ends up. Asked of the
    whole file those questions are answered by the prose: this page explains at
    length that it does not link to /start and does not stash the key in
    localStorage, and a test reading the raw text sees both strings and calls it
    a leak. Explaining a rule must not be indistinguishable from breaking it, so
    the commentary comes out first and the assertions run on what executes.
    """
    return _COMMENTS.sub(" ", src)


# ---------------------------------------------------------------------------
# 1. It is a demo, and it says so without being asked
# ---------------------------------------------------------------------------

def test_the_demo_band_is_in_the_markup_not_painted_by_script():
    """A page whose script died must still say DEMO.

    The band is the whole claim this page makes to the room. If it were written
    by JavaScript then a page served from a stale cache, a blocked script or a
    thrown exception in any of the four fetches below would render as an
    ordinary-looking console — which is the state the /test entrance was already
    in, and the reason this page exists.
    """
    src = _demo()
    head = src[:src.index("<script>")]
    assert "DEMO MODE" in head
    assert "Nothing started from this page is study data." in head
    # And again around the live encounter, on the screen the lab is actually
    # looking at while it runs.
    assert "DEMO ENCOUNTER" in head
    assert "cohort: internal" in head


def test_the_stage_wraps_the_participant_page_rather_than_leaving_it_bare():
    """The live demo runs inside the demo chrome.

    This is the only lever the door has over what is on screen DURING an
    encounter: static/v2.html shows the participant's page and has no idea it is
    being demonstrated. Framing it is what puts the band, the run id and the
    cohort above the conversation the lab is watching.
    """
    src = _demo()
    frame = re.search(r"<iframe[^>]*id=\"stageFrame\"[^>]*>", src)
    assert frame, "the stage has no frame, so a live demo leaves the demo chrome behind"
    allow = re.search(r"allow=\"([^\"]+)\"", frame.group(0))
    assert allow, "the frame delegates no permissions, so /v2 inside it cannot record"
    granted = {p.strip() for p in allow.group(1).split(";") if p.strip()}
    # A same-origin frame does NOT inherit either of these. Without them the
    # encounter dies at its first getUserMedia, in front of the lab.
    assert {"microphone", "camera"} <= granted
    # And nothing beyond what the encounter needs.
    assert granted <= {"microphone", "camera", "autoplay"}, granted


def test_nothing_the_page_hides_is_given_a_display_by_its_class():
    """`el.hidden = true` has to actually hide it.

    The browser hides a [hidden] element with a UA rule of `display: none`, and
    ANY class rule that sets `display` outranks it. This is not a theoretical
    specificity puzzle: the stage's escape hatch is `display: flex` from its
    class, and the first live demo driven through this page painted "the
    participant page has not loaded in this frame" over a participant page that
    had loaded perfectly — from the moment the stage opened until it closed,
    with `hidden` reading true the whole time.

    So every element this page hides by attribute is checked against the rules
    that style it. A class that sets display must also carry a `[hidden]`
    override, or the hiding is decoration.
    """
    src = _demo()
    style = src[src.index("<style>"):src.index("</style>")]
    body = _script(src)

    hidden_ids = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)\.hidden\s*=", body))
    assert hidden_ids, "nothing on this page is hidden by attribute any more"

    guarded = set(re.findall(r"\.([A-Za-z0-9_-]+)\[hidden\]", style))
    offenders = []
    for el_id in sorted(hidden_ids):
        tag = re.search(r"<[a-z]+[^>]*id=\"%s\"[^>]*>" % re.escape(el_id), src)
        if not tag:
            continue
        classes = re.search(r"class=\"([^\"]*)\"", tag.group(0))
        for cls in (classes.group(1).split() if classes else []):
            if cls in guarded:
                continue
            for rule in re.findall(r"\.%s\s*\{([^}]*)\}" % re.escape(cls), style):
                if re.search(r"(^|[;\s])display\s*:", rule):
                    offenders.append(f"#{el_id} is hidden by attribute but .{cls} sets display")
    assert not offenders, offenders


def test_the_stage_stops_recording_when_the_demo_ends():
    """Closing the stage cuts the frame loose.

    Hiding it would leave a live microphone, a live webcam and an open socket
    running behind a display:none, which is a recording nobody on the screen
    believes is happening.
    """
    body = _script(_demo())
    close = body[body.index("function closeStage()"):]
    close = close[:close.index("\n}")]
    assert "'about:blank'" in close


# ---------------------------------------------------------------------------
# 2. Not a second way into the study
# ---------------------------------------------------------------------------

def test_the_demo_door_offers_no_study_entrance():
    """Nothing here may be a way into the wave.

    The study entrance (/start) mints
    cohort=study runs against a Qualtrics response id. A demo page that linked
    to one of them would be a second, undocumented, unrecruited way into the
    dataset — and the person most likely to click it is somebody the researcher
    just handed the laptop to.
    """
    code = _code(_demo())
    assert "/start" not in code, "the demo door links to the study entrance /start"
    assert "/test?" in code, "the demo door does not use the internal entrance at all"


def test_the_demo_door_mints_no_consent():
    """It writes no consent and claims none.

    POST /api/consent is how a consent record comes into being. The demo path
    reaches it the way a participant does — through /v2, which /test redirects
    to — and this page must not be a second caller with its own idea of what to
    put in the record.
    """
    code = _code(_demo())
    assert "/api/consent" not in code
    assert "consent_given" not in code
    assert "qid=" not in code
    # consent_source is printed on this page, deliberately — the callout tells
    # the room what the record will say. What it may never be is a value this
    # page decides, so it may appear in the markup and never in the script.
    body = _code(_script(_demo()))
    assert "consent_source" not in body
    assert "JSON.stringify" not in body, (
        "the demo door builds a request body; it is supposed to open doors, "
        "not write records")


def test_every_live_demo_is_prefixed_demo_in_the_participant_key():
    """The mark an analyst sees without filtering for it.

    /test builds the run's participant key as test_<name>_<epoch>, and that key
    is printed by the researcher console, /api/runs, the encounter manifest and
    every export. cohort=internal is the mark somebody has to think to filter
    on; this is the one they cannot miss. Forced rather than suggested, and
    present even when the field is left empty.
    """
    body = _script(_demo())
    fn = body[body.index("function demoName()"):]
    fn = fn[:fn.index("\n}")]
    assert "'demo-' +" in fn, "the demo prefix is not forced onto the name"
    assert "'anon'" in fn, "an empty name produces no name at all"


def test_the_landing_page_hides_the_demo_door_from_a_participant():
    """The door is revealed to an operator, and to nobody else.

    This page is what a recruited participant lands on. A visible demo link is
    an invitation to start a run that is not theirs — and the containment (every
    demo is cohort=internal, and /test is key-gated) is what makes that merely
    wasteful rather than harmful, which is not a reason to put the button in
    front of them.
    """
    src = _landing()
    link = re.search(r"<a href=\"/static/demo\.html\"[^>]*>", src)
    assert link, "the landing page has no demo door"
    assert "hidden" in link.group(0), "the demo door ships visible to participants"

    body = _script(src)
    reveal = body[body.index("async function revealDemoDoor()"):]
    reveal = reveal[:reveal.index("\n  }")]
    # Only an explicit false opens it. An unreachable server, a malformed
    # answer and a keyed deployment all have to leave it shut: failing to get
    # an answer is not evidence that nobody is watching.
    assert "session_key_configured === false" in reveal
    assert "if (key)" in reveal


def test_the_landing_page_asks_health_once():
    """One answer per page load, for two readers.

    The rater bypass and the demo door ask the same question. Two fetches are
    two answers that can disagree inside one page load, and the disagreement
    would be between "this server is keyed" and "this server is not" — which is
    the only question either of them is asking.
    """
    body = _script(_landing())
    assert body.count("fetch('/health')") == 1
    assert "function serverHealth()" in body
    # Cached as the promise, so a caller arriving mid-flight waits rather than
    # starting a second request.
    assert "healthPromise = fetch('/health')" in body


# ---------------------------------------------------------------------------
# 3. The replay lane cannot fail live
# ---------------------------------------------------------------------------

def test_the_replay_lane_touches_no_gateway_and_no_bucket():
    """Replay is reads off disk, and that is the whole promise.

    The reason to have this lane is that it has no credentialed dependency to
    fail in front of an audience: it opens recorded encounters. Anything in here
    that presigns an upload, starts a session or asks the gateway for something
    would put the failure it exists to avoid back into it.
    """
    code = _code(_demo())
    for forbidden in ("video-upload-url", "video-uploaded", "amazonaws",
                      "/ws/participant", "/api/launch"):
        assert forbidden not in code, f"the demo door reaches {forbidden}"
    # What it does call, and all it calls.
    called = set(re.findall(r"fetch\('(/[^']+?)'", _code(_script(_demo()))))
    called = {u.split("?")[0] for u in called}
    assert called <= {"/health", "/api/encounters", "/api/runs"}, called


def test_the_replay_lane_is_offered_before_the_live_one():
    """Order is the advice.

    Somebody with one chance to show the platform should be shown the lane that
    cannot break first, not have to scroll past the one that can.
    """
    src = _demo()
    assert src.index("Lane 1 · cannot fail live") < src.index("Lane 2 · the real thing")
    assert src.index("evidenceBtn") < src.index("liveBtn")


def test_the_live_lane_is_switched_off_when_it_would_answer_401():
    """A keyed server and no key is a 401, and the page says so first.

    /test is gated on SESSION_KEY. Pressing a button that opens a blank 401 in
    front of a room is the exact shape of failure this page is for, and /health
    answers the question — session_key_configured — without a credential.
    """
    body = _script(_demo())
    checks = body[body.index("function paintChecks()"):body.index("async function loadHealth()")]
    assert "$('liveBtn').disabled = blocked" in checks
    assert "keyed && !KEY" in checks


def test_the_researcher_key_does_not_stay_in_the_address_bar():
    """This is the one page in the repository whose purpose is to be projected.

    On a deployed server the door needs SESSION_KEY — it gates /test, /evidence
    and /api/encounters alike — so the page is opened with the key in the URL. A
    key in the address bar in front of a room has been handed to the room.

    Nor is it stashed anywhere that outlives the tab: a credential left behind
    on a shared demo machine is worse than one that has to be pasted again.
    """
    body = _code(_script(_demo()))
    assert "history.replaceState" in body
    assert "params.delete('key')" in body
    assert "localStorage" not in body and "sessionStorage" not in body


# ---------------------------------------------------------------------------
# 4. The steering the lab will not see
# ---------------------------------------------------------------------------

#: Model names the page's matcher and server/voice/realtime.py's family_of must
#: agree about. The first is what this study is configured for today; the rest
#: are the members of each family the gateway carries, plus the shapes that must
#: NOT be matched into a family nobody has probed.
_MODEL_NAMES = (
    "nto.gemini-live-2.5-flash",
    "nto.gemini-live-2.5-flash-native-audio",
    "gemini-live-2.5-flash",
    "gpt-realtime-2.1",
    "gpt-realtime-2.1-mini",
    "openai-realtime",
    "nto.gemini-3.1-flash-lite",
    "claude-opus-4",
    "",
    "realtime",
    "gemini-2.5-flash",
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_decides_the_model_family_exactly_as_the_server_does():
    """The JavaScript copy of family_of, pinned to the Python original.

    The page has to know which realtime family REALTIME_MODEL belongs to,
    because that is what decides whether a live demo can show steering at all,
    and /health hands it a model name rather than a family. So the rule is
    written twice, in two languages — which is exactly how server/llm.py's
    duplicated model defaults went stale, and why tests/test_final_preflight.py
    pins those against their source modules.

    A drift here is quiet and expensive: a new gemini-live member the page fails
    to recognise gets the "nobody has checked" card, and a researcher reads that
    as "probably fine" and promises the room live steering on a model that
    silently discards it.
    """
    from server.voice.realtime import family_of

    body = _script(_demo())
    fn = body[body.index("function familyOf(model)"):]
    fn = fn[:fn.index("\n}") + 2]
    script = fn + "\nconsole.log(JSON.stringify(%s.map(familyOf)));" % json.dumps(
        list(_MODEL_NAMES))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == [family_of(m) for m in _MODEL_NAMES]


def test_the_configured_model_is_the_one_with_no_mid_session_steering():
    """The finding the card exists for, read off the table that holds it.

    If this ever stops being true — a new row, a changed default — the card the
    page paints for gemini-live is wrong, and it is wrong in the direction of
    telling a researcher that an encounter is unsteered when it is not.
    """
    from server.llm import provenance
    from server.voice.realtime import capabilities_for, family_of

    model = provenance()["realtime_model"]
    assert family_of(model) == "gemini-live", model
    assert capabilities_for(model).honours_session_update is False


def test_the_gemini_card_says_the_encounter_will_carry_no_steering():
    """Said plainly, and with what the record will show.

    The researcher is going to be asked about steering in that room. The honest
    answer is that the opening brief lands and nothing after it does, that the
    steering panel will still move while it does not, and that the runner marks
    the encounter — one steer_unacked event and a red line on the researcher
    console. A card that merely said "steering may be unreliable" would leave
    them to discover the rest live.
    """
    body = _script(_demo())
    # Sliced on the opening of the condition, not on a closed `)`: the card is
    # shared by both gemini rows now (`fam === 'gemini-live' || fam ===
    # 'gemini-live-native-audio'`), because neither has a measurement saying
    # mid-session steering is honoured.
    card = body[body.index("if (fam === 'gemini-live'"):body.index("} else if (fam === 'gpt-realtime')")]
    assert "steer_unacked" in card
    assert "session.update" in card
    # The two halves that are easy to leave out and impossible to recover from
    # on stage: the brief DOES land, and the panel moves anyway.
    assert "opening brief lands" in card
    assert "steering panel will still move" in card
    # And where to show steering instead, since the platform can still show it.
    assert "evidence trace" in card


def test_an_unknown_model_is_not_reported_as_working():
    """"Nobody has checked" is a third answer, not a default to yes.

    require_capabilities refuses an unknown model loudly for the same reason:
    guessing a family is how a study comes to run on a model whose behaviour
    nobody probed.
    """
    body = _script(_demo())
    card = body[body.index("} else {"):body.index("function paintChecks()")]
    assert "REALTIME_FAMILIES" in card
    assert "unknown is not the same as yes" in card


# ---------------------------------------------------------------------------
# 5. What the server actually records for a demo
#
# Pins, not repairs: server/app.py's /test and server/storage.py's
# _consent_provenance already behave this way. They are here because the demo
# door is built entirely on top of that behaviour, and a change to it would
# turn this page from a demo entrance into an unlabelled study entrance without
# a single line of static/demo.html changing.
# ---------------------------------------------------------------------------

@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def store(tmp_path, monkeypatch, runs_mod):
    from server import storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    storage.init_storage()
    return storage


@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


def _start_demo(client, name="demo-lab"):
    """Arrive the way the demo door arrives, and return (run_id, record_id)."""
    r = client.get("/test", params={"name": name}, follow_redirects=False)
    assert r.status_code == 307, r.text
    q = dict(p.split("=", 1) for p in r.headers["location"].split("?", 1)[1].split("&"))
    return q["run"], q["participant_id"]


def test_a_demo_run_is_internal_before_it_has_an_encounter(client, runs_mod):
    """cohort is decided at the entrance, not at the end.

    A tag applied later would leave a window in which a demo is study data, and
    the window is the whole encounter.
    """
    run_id, _ = _start_demo(client)
    run = runs_mod.get(run_id)
    assert run["cohort"] == "internal"
    assert run["qualtrics_id"] is None
    # The second mark, the one that is printed rather than filtered on.
    assert run["participant_id"].startswith("test_demo-")


def test_a_demo_run_is_absent_from_the_study_export(client, runs_mod):
    """?cohort=study is the analysis set, and a demo is not in it."""
    run_id, _ = _start_demo(client)
    study = client.get("/api/runs", params={"cohort": "study"}).json()
    assert run_id not in [r["run_id"] for r in study]
    internal = client.get("/api/runs", params={"cohort": "internal"}).json()
    assert run_id in [r["run_id"] for r in internal], (
        "the demo is excluded from study data and invisible to the internal "
        "filter too, which is the worst of both: it cannot be found to be "
        "deleted")


def test_a_demo_consent_record_names_the_demo_and_not_the_survey(client, store):
    """The record has to say what actually happened.

    A participant's consent is evidenced by their Qualtrics response id. A demo
    has no Qualtrics response and never will, so the only truthful record is one
    that says so: source internal_test, reference the internal run id. What it
    must never do is carry consent_given alone, or borrow the survey's
    provenance by silence — a record like that reads, to anyone auditing it
    later, as though somebody consented to the study here.
    """
    run_id, pid = _start_demo(client)

    # As minted: identity, no consent. An entry point does not get to assert
    # somebody's agreement on their behalf.
    rec = store.get_participant(pid)
    assert rec["consent_given"] is False

    # The handoff /v2 performs.
    r = client.post("/api/consent", json={
        "code": f"test_demo-lab", "participant_id": pid, "run_id": run_id,
        "consent_given": True, "consent_source": "qualtrics",
    })
    assert r.status_code == 200, r.text

    rec = store.get_participant(pid)
    assert rec["consent_given"] is True
    assert rec["consent_source"] == store.CONSENT_SOURCE_INTERNAL == "internal_test"
    assert rec["consent_reference"] == run_id
    assert rec["consent_reference_kind"] == "internal_run_id"
    # The page asked for "qualtrics" in that POST and was ignored, which is the
    # point: provenance is resolved from the run the server wrote, never from
    # what the browser claims. A client that could name its own source could
    # manufacture a study consent out of a demo.
    assert rec["consent_upstream_verified"] is False
    assert "qualtrics" not in json.dumps(rec).lower()


def test_the_capture_gate_is_no_looser_for_a_demo(client, store, monkeypatch):
    """A demo reaches the microphone through the same consent record, or not at all.

    The demo entrance needs no Qualtrics setup, and it would be an easy and
    invisible mistake to let that become "and no consent record either". It is
    not: the voice socket closes 4403 on a demo record that has not consented,
    exactly as it does on a participant's, and opens once the record exists.
    """
    run_id, pid = _start_demo(client)

    def no_such_scenario(*a, **k):
        raise FileNotFoundError("unknown scenario: conflict")

    monkeypatch.setattr(appmod.registry, "create", no_such_scenario)

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
                f"/ws/participant/voice?scenario=conflict&participant_id={pid}"):
            pass
    assert caught.value.code == 4403

    client.post("/api/consent", json={
        "code": "test_demo-lab", "participant_id": pid, "run_id": run_id,
        "consent_given": True,
    })
    # Positive control: now it opens, so the 4403 above was the consent gate and
    # not a broken fixture.
    with client.websocket_connect(
            f"/ws/participant/voice?scenario=conflict&participant_id={pid}") as ws:
        assert ws.receive_json()["type"] == "error"
