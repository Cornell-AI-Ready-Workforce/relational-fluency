"""Regression tests for the configuration and data core.

Defects that failed silently or published something they should not have, and
that the fixture wave would have carried without a mark:

  * server/llm.py           the gateway key reaching a log line and the public
                            unauthenticated /health via preflight()'s `detail`,
                            and the redactor mangling that same line when the
                            configured key is a short placeholder
  * server/storage.py       record_decline un-consenting a participant who had
                            already consented, irreversibly
  * server/scenarios.py     a participant-supplied ?scenario= escaping the
                            scenarios directory
  * server/steering.py      a gateway reply with no adjust_persona tool call
                            being recorded as "the model saw nothing to change"
  * server/verify_record.py the post-wave completeness check calling an
                            uploaded recording absent, and calling an encounter
                            the watchdog dragged through its beats fully covered

Nothing here opens a socket. The credential seams are exercised by raising the
real exception types the real libraries raise — h11's LocalProtocolError is
produced by running h11's own header validation, and the transport failure is a
genuine httpx.ConnectError — because a hand-rolled stand-in would not carry the
key in its message, which is the entire point of the first group.

Nothing here depends on a machine-specific path either. The consent records the
storage group needs are built in tmp_path through storage's own writers, so the
guard is exercised on the lab's machine and in CI and not only where one
session's scratch fixture happens to exist. A recorded wave, when RF_FIXTURE or
DATA_DIR points at one, is checked as an EXTRA at the end — always via a copy.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest

from server import llm, scenarios, steering, storage, verify_record

REPO_ROOT = Path(__file__).resolve().parent.parent

# A recorded wave is an optional EXTRA, never the thing the guards depend on.
# Round one hard-coded an absolute path into one session's scratchpad and
# skipped when it was absent, so on the lab's machine, in CI, or in that same
# session a day later, the five tests protecting the consent record would have
# become silent skips: a green suite over an unverified guard, which is the
# exact failure mode this audit exists to remove.
#
# The wave itself is resolved once, in tests/conftest.py, and reaches the two
# tests below through the `wave_dir` / `optional_wave` fixtures. This module
# used to read RF_FIXTURE-or-DATA_DIR itself, which was better than a literal
# path but still a third spelling of the question in a suite that had four.

# A key shaped like a real LiteLLM one, wrapped across two lines the way a paste
# out of a terminal or a ticket wraps it. _cfg()'s strip() does not touch the
# interior newline, so this is exactly what reaches the Authorization header.
WRAPPED_KEY = "sk-LIVEKEY-AAAABBBBCCCC\nDDDDEEEEFFFF-TAIL"
FLAT_KEY = "sk-LIVEKEY-AAAABBBBCCCCDDDDEEEEFFFF-TAIL"


def _key_fragments(key: str):
    """Every substring of `key` long enough to be worth finding in a log."""
    return [part for part in key.split() if len(part) >= 6]


def _leaks(blob: str, key: str) -> bool:
    return key in blob or any(frag in blob for frag in _key_fragments(key))


@pytest.fixture()
def wrapped_key(monkeypatch):
    monkeypatch.setitem(llm._FILE, "LITELLM_API_KEY", WRAPPED_KEY)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.gateway_api_key() == WRAPPED_KEY, "strip() must leave the newline"
    return WRAPPED_KEY


@pytest.fixture()
def flat_key(monkeypatch):
    monkeypatch.setitem(llm._FILE, "LITELLM_API_KEY", FLAT_KEY)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return FLAT_KEY


# --- B6: the gateway key must not reach a log line or /health ----------------


def test_a_wrapped_key_is_named_but_never_quoted(wrapped_key, monkeypatch):
    """The failure the operator has to fix, described without the credential.

    This is the case that produced the repo's own "Illegal header value" boot
    warning. preflight() must say what is wrong — a newline in the key — and
    must not hand any part of the value to a string that app.py prints at boot
    and serves from the unauthenticated /health.
    """
    def no_network(*a, **kw):  # pragma: no cover - failing here is the point
        raise AssertionError("preflight must not send a key it already knows is malformed")

    monkeypatch.setattr(httpx, "get", no_network)

    result = llm.preflight()

    assert result["ok"] is False
    assert "newline" in result["detail"]
    assert not _leaks(json.dumps(result), wrapped_key)


def test_h11s_own_complaint_is_redacted_before_it_could_be_served(flat_key):
    """h11 quotes the whole header value; redact_key has to survive that.

    The exception message is generated by h11's real validation rather than
    written by hand, because what makes this dangerous is precisely that the
    library embeds the credential verbatim, and a stand-in would not.
    """
    import h11

    with pytest.raises(Exception) as caught:
        h11.Request(
            method="GET",
            target="/v1/models",
            headers=[("host", "api.ai.it.cornell.edu"),
                     ("authorization", f"Bearer {flat_key}\nx-injected: 1")],
        )
    raw = str(caught.value)
    assert flat_key in raw, "h11 stopped quoting the header; this test needs rewriting"

    assert not _leaks(llm.redact_key(raw), flat_key)


def test_the_escaped_form_of_a_wrapped_key_is_redacted_too(wrapped_key):
    """A newline in the key survives into the message as the characters \\ and n.

    So the raw value never matches, which is why redact_key looks for the
    escaped form and for each line of the key as well.
    """
    message = f"Illegal header value {('Bearer ' + wrapped_key).encode()!r}"
    assert not _leaks(llm.redact_key(message), wrapped_key)


def test_a_gateway_that_echoes_the_key_in_its_401_body_does_not_publish_it(flat_key,
                                                                          monkeypatch):
    """The likelier vector: the gateway's own error body lands in `detail`."""
    body = json.dumps({"error": {"message": f"Invalid proxy key: {flat_key}",
                                 "type": "auth_error"}})
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(401, text=body))

    result = llm.preflight()

    assert result["ok"] is False and result["status"] == 401
    assert not _leaks(json.dumps(result), flat_key)
    assert "<redacted>" in result["detail"]


def test_a_transport_failure_keeps_its_type_and_loses_the_key(flat_key, monkeypatch):
    """The useful half of an exception is its type; the message is not trusted."""
    def boom(*a, **kw):
        raise httpx.ConnectError(f"failed sending 'Bearer {flat_key}' to gateway")

    monkeypatch.setattr(httpx, "get", boom)

    result = llm.preflight()

    assert result["detail"].startswith("ConnectError:")
    assert not _leaks(json.dumps(result), flat_key)


def test_a_healthy_gateway_is_unchanged(flat_key, monkeypatch):
    """The redaction must not cost the ordinary answer its meaning."""
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(200, text="{}"))

    result = llm.preflight()

    assert result["ok"] is True and result["status"] == 200
    assert "detail" not in result


@pytest.mark.parametrize("short", ["x", "sk-", "test", "dummy"])
def test_a_placeholder_key_does_not_shred_the_diagnostic(short, monkeypatch):
    """The redactor substitutes bare substrings, so it needs a length floor.

    With LITELLM_API_KEY=x, "max retries exceeded" came back as
    "ma<redacted> retries e<redacted>ceeded" — the one line an operator reads
    off /health to find out why the gateway is down, destroyed at exactly the
    moment a placeholder key is what they are running. A value this short is
    not a LiteLLM credential, so there is nothing being protected in exchange.
    """
    monkeypatch.setitem(llm._FILE, "LITELLM_API_KEY", short)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    message = ("Connection refused to api.ai.it.cornell.edu: max retries "
               "exceeded; check the dummy test stack")

    assert llm.redact_key(message) == message


def test_the_floor_is_low_enough_that_a_real_key_is_still_caught(flat_key):
    """The floor must not become a hole: the shortest needle a real key
    produces is far above it, and the whole key is still redacted."""
    assert len(flat_key) >= 6
    assert llm.redact_key(f"401 from gateway: bad key {flat_key}") == (
        "401 from gateway: bad key <redacted>"
    )


def test_a_six_character_key_is_still_redacted(monkeypatch):
    """Exactly at the floor, not below it — an off-by-one here leaks."""
    monkeypatch.setitem(llm._FILE, "LITELLM_API_KEY", "abc123")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert "abc123" not in llm.redact_key("gateway said: abc123 is invalid")


# --- B16: a refusal is terminal, and consent is not retroactive --------------


@pytest.fixture()
def participants(tmp_path, monkeypatch):
    """An empty store under tmp_path, wired through storage's own globals.

    Built rather than copied. The records these tests need are two states of
    one small file, and storage.create_participant/record_consent are the only
    writers of it in the app, so minting them here exercises the real shapes
    while keeping the guard verifiable on any machine.
    """
    root = tmp_path / "data"
    monkeypatch.setattr(storage, "DATA_DIR", root)
    monkeypatch.setattr(storage, "SESSIONS_DIR", root / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", root / "participants")
    monkeypatch.setattr(storage, "DB_PATH", root / "index.db")
    storage.init_storage()
    return root / "participants"


def _consented() -> dict:
    """A participant who ticked the box: consent_given plus consent_recorded_at.

    Minted the way /start then POST /api/consent mint it — created pending and
    flipped by record_consent — because the guard keys off both fields and a
    hand-written dict could quietly stop matching what the app writes.
    """
    pid = storage.create_participant("RF-CONSENTED", False, "2026-01-15")
    rec = storage.record_consent(pid, "2026-01-15")
    assert rec and rec["consent_given"] is True and rec.get("consent_recorded_at")
    return rec


def _pending() -> dict:
    """A participant who has been minted by /start and has not yet answered."""
    pid = storage.create_participant("RF-PENDING", False, "2026-01-15")
    rec = storage.get_participant(pid)
    assert rec and rec["consent_given"] is False
    assert "consent_recorded_at" not in rec
    return rec


def test_decline_refuses_a_participant_who_already_consented(participants):
    """The one field an IRB reads must not end up denying a recorded consent.

    A stale second tab, a back-button resubmit or a replayed POST used to flip
    a consented record to declined, and record_consent then refused to flip it
    back — locking the participant out of a study whose audio and webcam were
    already recorded under the consent the record now denied.
    """
    rec = _consented()
    pid = rec["id"]
    before = (participants / f"{pid}.json").read_text(encoding="utf-8")

    assert storage.record_decline(pid, "2026-09-01", run_id="whatever") is None

    after = json.loads((participants / f"{pid}.json").read_text(encoding="utf-8"))
    assert after["consent_given"] is True
    assert "declined" not in after
    assert after["consent_text_version"] == rec["consent_text_version"]
    assert (participants / f"{pid}.json").read_text(encoding="utf-8") == before

    with sqlite3.connect(storage.DB_PATH) as conn:
        row = conn.execute(
            "SELECT consent_given FROM participants WHERE id = ?", (pid,)
        ).fetchone()
    assert row is None or row[0] == 1


def test_a_refused_decline_does_not_lock_the_participant_out(participants):
    """The irreversibility was the injury; check the way back is still open."""
    pid = _consented()["id"]

    storage.record_decline(pid, "2026-09-01")

    assert storage.record_consent(pid, "2026-09-01") is not None
    assert json.loads(
        (participants / f"{pid}.json").read_text(encoding="utf-8")
    )["consent_given"] is True


def test_a_genuine_refusal_is_still_recorded(participants):
    """A refusal is data — the guard must not swallow the case it exists for."""
    pid = _pending()["id"]

    rec = storage.record_decline(pid, "2026-09-01", run_id="r_abc")

    assert rec is not None
    assert rec["declined"] is True and rec["consent_given"] is False
    assert rec["run_id"] == "r_abc"
    on_disk = json.loads((participants / f"{pid}.json").read_text(encoding="utf-8"))
    assert on_disk["declined"] is True


def test_a_refusal_is_still_terminal_in_the_other_direction(participants):
    """The guard added here mirrors record_consent's; neither may weaken."""
    pid = _pending()["id"]
    storage.record_decline(pid, "2026-09-01")

    assert storage.record_consent(pid, "2026-09-01") is None


def test_declining_an_unknown_participant_is_still_a_miss(participants):
    assert storage.record_decline("p_0000000000_ffffff", "2026-09-01") is None


def test_the_guard_holds_over_a_real_recorded_wave(tmp_path, monkeypatch, wave_dir):
    """The same guard against records the app actually wrote, when one is here.

    An extra, not the guarantee: the tests above already cover the behaviour
    everywhere. This one exists because a real wave carries record shapes that
    predate the current writers, and a consented record from an older consent
    version must be just as un-declinable as a freshly minted one. Always a
    COPY — a test must not be able to damage a collection wave.
    """
    if not (wave_dir / "participants").is_dir():
        pytest.skip(f"the wave at {wave_dir} carries no participants/ directory")
    root = tmp_path / "wave"
    shutil.copytree(wave_dir / "participants", root / "participants")
    if (wave_dir / "index.db").exists():
        shutil.copy(wave_dir / "index.db", root / "index.db")
    monkeypatch.setattr(storage, "DATA_DIR", root)
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", root / "participants")
    monkeypatch.setattr(storage, "DB_PATH", root / "index.db")

    consented = [
        rec
        for path in sorted((root / "participants").glob("p_*.json"))
        for rec in [json.loads(path.read_text(encoding="utf-8"))]
        if rec.get("consent_given") or rec.get("consent_recorded_at")
    ]
    if not consented:
        pytest.skip("this wave has no consented participant to try it on")

    for rec in consented:
        pid = rec["id"]
        path = root / "participants" / f"{pid}.json"
        before = path.read_text(encoding="utf-8")
        assert storage.record_decline(pid, "2026-09-01", run_id="stale-tab") is None
        assert path.read_text(encoding="utf-8") == before


# --- B40: a scenario id is a filename fragment, not a path -------------------


@pytest.fixture()
def outside_yaml(tmp_path):
    """A perfectly well-formed scenario sitting outside SCENARIOS_DIR."""
    path = tmp_path / "outside_secret.yaml"
    path.write_text(
        "id: pwned\n"
        "title: Outside The Scenarios Dir\n"
        "intro: injected intro\n"
        "system_prompt: You are an actor whose brief came from outside the repo.\n",
        encoding="utf-8",
    )
    return path


def test_a_traversing_scenario_id_cannot_reach_a_yaml_outside_the_directory(
        outside_yaml, monkeypatch):
    """?scenario= is participant-controlled, so it must not address the disk.

    Unvalidated it loaded any mapping with an id/title/system_prompt as a live
    encounter: an attacker-chosen brief handed to the actor, and an id that is
    not a study scenario stamped onto the recording.
    """
    # Keep the allowed directory and attack target on the fixture's volume.
    # Windows CI puts the checkout on D: and temporary files on C:, so a path
    # relative to the real checkout cannot represent this traversal there.
    scenario_dir = outside_yaml.parent / "scenarios"
    scenario_dir.mkdir()
    monkeypatch.setattr(scenarios, "SCENARIOS_DIR", scenario_dir)
    traversal = os.path.relpath(
        outside_yaml.with_suffix(""), scenario_dir
    ).replace("\\", "/")
    assert ".." in traversal
    target = scenario_dir / f"{traversal}.yaml"
    assert target.resolve() == outside_yaml.resolve()
    assert target.is_file()

    with pytest.raises(FileNotFoundError):
        scenarios.load_scenario(traversal)


@pytest.mark.parametrize("bad", [
    "archive/mundane_chitchat",   # no traversal needed: a retired scenario
    "v3/S1A_taken_credit",        # existing file, wrong shape -> used to KeyError
    "../config/consent",
    "..\\..\\secrets",
    "C:/Windows/win",
    "/etc/passwd",
    "missed_deadlines\x00",
    "",
    "a" * 65,
])
def test_a_scenario_id_that_is_not_a_plain_name_is_a_miss(bad):
    """Every rejection is the same FileNotFoundError an unknown id gets, so the
    endpoint tells a caller nothing about what exists on disk."""
    with pytest.raises(FileNotFoundError):
        scenarios.load_scenario(bad)


def test_every_scenario_the_app_offers_still_loads():
    """The other half of the guard: nothing legitimate may be turned away."""
    offered = [row["id"] for row in scenarios.list_scenarios()]
    assert len(offered) >= 8
    for sid in offered:
        sc = scenarios.load_scenario(sid)
        assert sc.id == sid and sc.cast


# --- B41: a failed review must not read as "no change" ----------------------


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _Client:
    """Minimal stand-in for AsyncAnthropic. Deliberately not a MagicMock:
    _bounded() hands a non-AsyncAnthropic straight back untouched, and a mock's
    auto-generated with_options would swap out the object injected here."""

    def __init__(self, response):
        self._response = response
        self.messages = self

    async def create(self, **kw):
        self.last_kwargs = kw
        return self._response


def _controller(response):
    sc = scenarios.load_scenario("S1A")
    return steering.SteeringController(sc, client=_Client(response)), sc


def _history():
    return [{"speaker": "user", "text": "That's not what I asked for.", "t": 0.0}]


def test_a_reply_without_the_tool_call_is_an_error_not_a_decision():
    """A 200 that ignored tool_choice used to return [], and the caller then
    wrote nothing at all — indistinguishable in events.jsonl from a participant
    who earned no gear shifts, and from auto steering never having run."""
    ctrl, sc = _controller(_Response([_Block("text", text="No changes needed.")]))

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(ctrl.review(_history(), sc.initial_personas(), {}))

    assert "adjust_persona" in str(caught.value)
    assert ctrl.model in str(caught.value)


def test_an_empty_response_is_an_error_too():
    ctrl, sc = _controller(_Response([], stop_reason="max_tokens"))

    with pytest.raises(RuntimeError):
        asyncio.run(ctrl.review(_history(), sc.initial_personas(), {}))


def test_a_differently_named_tool_call_is_not_mistaken_for_a_decision():
    """Belt and braces on the gateway shim: a tool_use block is not enough."""
    ctrl, sc = _controller(_Response([
        _Block("tool_use", name="set_speakers", input={"speakers": []}),
    ]))

    with pytest.raises(RuntimeError):
        asyncio.run(ctrl.review(_history(), sc.initial_personas(), {}))


def test_an_empty_adjustments_list_is_still_an_honest_no_change():
    """"No change" is the expected outcome on most turns and must keep working:
    the point of the raise above is to stop a failure impersonating it."""
    ctrl, sc = _controller(_Response([
        _Block("tool_use", name="adjust_persona", input={"adjustments": []}),
    ]))

    assert asyncio.run(ctrl.review(_history(), sc.initial_personas(), {})) == []


def test_a_real_adjustment_still_comes_back_cleaned():
    aid = scenarios.load_scenario("S1A").cast[0].id
    ctrl, sc = _controller(_Response([
        _Block("tool_use", name="adjust_persona", input={"adjustments": [{
            "agent_id": aid, "knob": "warmth", "level": "low",
            "reason": "The participant dismissed their concern outright.",
        }]}),
    ]))
    personas = sc.initial_personas()

    out = asyncio.run(ctrl.review(_history(), personas, {}))

    assert len(out) == 1
    assert out[0]["agent_id"] == sc.cast[0].id and out[0]["knob"] == "warmth"
    assert out[0]["from_level"] == steering.band_of(
        getattr(personas[sc.cast[0].id], "warmth")
    )


def test_the_raise_reaches_the_record_as_auto_steer_error(tmp_path, monkeypatch):
    """The whole point of raising is that Session.auto_steer writes a trace.

    Checked end to end against a real Session with its data directory pointed
    at a scratch copy, because the defect was never in review() alone — it was
    that the caller had nothing to write down.
    """
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    storage.init_storage()
    from server.session import Session

    sess = Session("S1A")
    sess.auto_steering = True
    sess.steering.client = _Client(_Response([_Block("text", text="ok")]))
    sess.shared_history.extend(_history())

    asyncio.run(sess.auto_steer())

    events = [
        json.loads(line)
        for line in (sess.store.dir / "events.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()
    ]
    errors = [e for e in events if e.get("type") == "auto_steer_error"]
    assert errors, f"a failed review left no trace: {[e.get('type') for e in events]}"
    assert "adjust_persona" in errors[0]["message"]


# --- B42: what the module claims about group delivery must be true ----------


def test_the_module_note_does_not_promise_group_delivery_it_does_not_make():
    """steering.py used to state flatly that a gear change "takes effect on the
    next turn (system prompts are composed fresh per turn)". That holds for 1:1
    only. In a group room realtime_voice_session._steer returns without
    re-briefing anyone, so a shift recorded by set_knob as applied can reach the
    actors turns later, or not within that interaction at all — half the study
    (S3, S4) runs in a room. The delivery fix lives in the voice runner, not in
    this module; what this module owes the reader meanwhile is the truth, and a
    later edit that deletes the caveat without closing the gap should fail here.
    """
    note = steering.__doc__ or ""
    assert "GROUP" in note or "group room" in note
    assert "re-brief" in note or "rebrief" in note
    assert "realtime_voice_session" in note


def test_the_module_note_does_not_promise_immediate_11_delivery_either():
    """The replacement note claimed the 1:1 case was "immediate". It is not.

    realtime_voice_session._steer defers the 1:1 re-brief whenever a reply is
    already in flight — a case its own comment calls routine, since a review can
    take eleven seconds — and writes steer_deferred instead. A reader who
    trusted the old wording would treat every 1:1 knob_set as delivered on the
    next turn, which is precisely the error the note was written to prevent for
    groups. Pinned here by the mechanism's own event name, so a future note
    cannot quietly drop the caveat.
    """
    note = steering.__doc__ or ""
    assert "steer_deferred" in note, (
        "the 1:1 deferral is undocumented again; a reader will read knob_set as "
        "delivered on the next turn"
    )
    assert "immediate" not in note.lower(), (
        "no mode of this controller delivers immediately"
    )
    # The asymmetry is the analyst-facing half: a deferred 1:1 shift leaves an
    # event behind, an undelivered group shift leaves nothing at all.
    assert "asymmetry" in note.lower() or "leaves NOTHING" in note
    source = (REPO_ROOT / "server" / "realtime_voice_session.py").read_text(
        encoding="utf-8")
    assert '"steer_deferred"' in source, (
        "the note describes an event the runner no longer writes"
    )


# --- R7 / R35: the post-wave check must not report a false record -----------

VERIFY_SID = "s_1772460300_44c9a2"
VIDEO_KEY = f"encounters/{VERIFY_SID}/webcam.webm"


def _encounter(tmp_path, *events) -> Path:
    """A session directory carrying the event trail verify() reads.

    Only the fields the checks under test touch; the other checks are expected
    to FAIL on this skeleton, which is fine — each test reads the one line it is
    about out of verify()'s list rather than the overall verdict.
    """
    sdir = tmp_path / "sessions" / VERIFY_SID
    sdir.mkdir(parents=True)
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": 0.0, "type": "session_start",
                             "scenario": "S1A"}) + "\n")
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    return sdir


def _check(sdir: Path, label: str):
    ok, checks = verify_record.verify(sdir)
    match = [c for c in checks if c[1] == label]
    assert match, f"no {label!r} check in {[c[1] for c in checks]}"
    return match[0]


def _upload(status, size, **extra):
    ev = {"t": None, "wall": 1772460800.0, "type": "video_uploaded",
          "key": VIDEO_KEY, "bytes": size, "status": status}
    ev.update(extra)
    return ev


def _fired(trigger_id, index, *, probing):
    return {"t": float(index), "type": "trigger_fired", "trigger_id": trigger_id,
            "interaction": "i1", "segment": 0, "esci": ["esci_1"],
            "probing": probing, "index": index}


def test_a_retried_confirmation_is_read_last_wins_not_first(tmp_path):
    """The PUT landed, the first HEAD got a 503, a retry confirmed it.

    The confirm endpoint now writes an event on every attempt and the page
    retries up to three times, so a "failed" event routinely sits AHEAD of the
    "ok" one. Taking the FIRST event reported a recording that is safely in the
    bucket as NOT UPLOADED — the same false record this tool exists to catch,
    committed by the tool itself, and on the researcher's post-wave check rather
    than somewhere anyone would notice. encounter_record.build already reads it
    last-wins; the two must not disagree about whether a recording exists.
    """
    sdir = _encounter(
        tmp_path,
        _upload("failed", None, error="SlowDown"),
        _upload("ok", 148_221),
    )

    passed, _, detail = _check(sdir, "webcam video in S3")

    assert passed is True
    assert detail == "148221 bytes"
    # And the analysis-facing record agrees, which is the point.
    from server.encounter_record import build
    assert build(sdir)["video"] == [{"key": VIDEO_KEY, "bytes": 148_221}]


def test_a_later_failure_does_not_erase_a_confirmed_upload(tmp_path):
    """Last-wins means last event WITH BYTES, not simply the last event.

    A confirm that succeeded and was then re-POSTed into a transient S3 error
    must not turn a stored recording back into a missing one.
    """
    sdir = _encounter(
        tmp_path,
        _upload("ok", 148_221),
        _upload("failed", 0, error="SlowDown"),
    )

    passed, _, detail = _check(sdir, "webcam video in S3")

    assert passed is True and detail == "148221 bytes"


def test_an_unconfirmed_upload_is_its_own_state_not_a_lost_recording(tmp_path):
    """S3 declining to answer is not S3 saying the object is not there.

    The browser holds a recording that may well have reached the bucket with
    only the confirmation lost. Calling that NOT UPLOADED writes off a
    recoverable encounter; naming the third state sends the researcher to look.
    """
    sdir = _encounter(
        tmp_path,
        _upload("failed", None, error="SlowDown"),
        _upload("failed", None, error="SlowDown"),
    )

    passed, _, detail = _check(sdir, "webcam video in S3")

    assert passed is False, "an unconfirmed upload is still not a verified one"
    assert detail == "CAPTURED, UPLOAD UNCONFIRMED: SlowDown"


def test_s3_answering_that_the_object_is_absent_still_reads_as_lost(tmp_path):
    """The definite negative keeps its meaning; only the ambiguous one moved."""
    sdir = _encounter(tmp_path, _upload("failed", 0, error="not_found"))

    passed, _, detail = _check(sdir, "webcam video in S3")

    assert passed is False
    assert "NOT UPLOADED" in detail and "absent" in detail


def test_no_confirmation_at_all_is_still_plainly_not_uploaded(tmp_path):
    """The honest negative, unchanged: no event, nothing was captured."""
    sdir = _encounter(tmp_path)

    passed, _, detail = _check(sdir, "webcam video in S3")

    assert passed is False and detail == "NOT UPLOADED"


def test_a_probed_beat_is_not_counted_as_a_volunteered_one(tmp_path):
    """Every beat in every spec now carries a probe, so a participant who says
    almost nothing gets walked through the rest by the watchdog, one beat per
    PROBE_AFTER_SECONDS. That used to read "4/4, all": full coverage of an
    encounter in which the participant reached nothing on their own, on the one
    report a researcher runs over a whole wave. Both routes still count as
    delivered stimulus — the beat did happen — but the line has to say which."""
    ids = verify_record._expected_triggers("S1A")
    assert len(ids) == 4, "S1A's beat plan changed; this test needs rewriting"
    sdir = _encounter(
        tmp_path,
        _fired(ids[0], 0, probing=False),
        _fired(ids[1], 1, probing=True),
        _fired(ids[2], 2, probing=True),
        _fired(ids[3], 3, probing=True),
    )

    passed, _, detail = _check(sdir, "planted triggers fired")

    assert passed is True, "all four beats were delivered; coverage is complete"
    assert "1 volunteered" in detail and "3 probed" in detail
    assert detail.startswith("4/4")
    assert ", all" not in detail, (
        "the old wording claimed full participation, not full coverage"
    )


def test_a_fully_volunteered_encounter_says_so(tmp_path):
    """The other half: the split must not smear a genuine encounter either."""
    ids = verify_record._expected_triggers("S1A")
    sdir = _encounter(
        tmp_path, *(_fired(t, i, probing=False) for i, t in enumerate(ids))
    )

    passed, _, detail = _check(sdir, "planted triggers fired")

    assert passed is True
    assert detail == "4/4 (4 volunteered, 0 probed)"


def test_a_retracted_probe_is_not_counted_on_either_side(tmp_path):
    """_net_fired's cancellation still governs both halves of the split.

    A beat that was briefed and never delivered leaves its trigger_fired line
    behind; counting it as coverage — volunteered or probed — would report a
    beat nobody spoke as one the participant faced.
    """
    ids = verify_record._expected_triggers("S1A")
    sdir = _encounter(
        tmp_path,
        _fired(ids[0], 0, probing=False),
        _fired(ids[1], 1, probing=True),
        {"t": 2.0, "type": "trigger_undelivered", "trigger_id": ids[1], "index": 1},
    )

    passed, _, detail = _check(sdir, "planted triggers fired")

    assert passed is False
    assert detail.startswith("1/4 (1 volunteered, 0 probed)")
    assert ids[1] in detail and ids[2] in detail and ids[3] in detail


# --------------------------------------------------------------------------
# The suite's own guards: a guard that does not run is not a guard.
# --------------------------------------------------------------------------

#: The wave-parametrised regression guard for round two's `_enter` identity
#: rewrite (B8 / B14 / B28 / B31 / B34) — the test that proves the runner never
#: ends up naming a character it has no live session for.
IDENTITY_GUARD = "test_every_wave_scenario_holds_the_identity_invariant"


def test_the_wave_parametrised_identity_guard_is_not_an_empty_parametrisation(
        request, optional_wave):
    """The headline identity guard must actually collect cases.

    tests/test_runner_blockers.py parametrises
    `test_every_wave_scenario_holds_the_identity_invariant` — the guard for
    round two's `_enter` identity rewrite, the test that proves the runner
    never names a character it has no live session for — over
    `_fixture_scenarios()`, which reads RF_FIXTURE_DIR and nothing else.

    What an empty parametrisation actually does, checked rather than assumed:
    pytest substitutes ONE placeholder case, ids it `[NOTSET]`, and skips it
    with "got empty parameter set". So it is not literally invisible — but the
    only trace in a bare `pytest` run is a single `s` among hundreds, its
    reason appears only under `-rs`, and it names neither the guard's purpose
    nor the variable that would switch it on. Measured on this tree: 26 real
    cases with a wave, one NOTSET skip without.

    Two halves of the fix. tests/conftest.py resolves the wave from any of the
    three spellings in use and exports it into RF_FIXTURE_DIR, which is the
    only one `_fixture_scenarios()` reads — so setting DATA_DIR or RF_FIXTURE,
    or checking a wave in under tests/data, now makes the guard run. This test
    is the other half: it converts that near-silence into either an explicit
    skip naming the variable, or a failure if a wave IS present and the guard
    still collected nothing — which is the dangerous case, because then the
    suite looks fully exercised and is not.
    """
    collected = [item.nodeid for item in request.session.items]
    if not any("test_runner_blockers" in nodeid for nodeid in collected):
        pytest.skip("tests/test_runner_blockers.py was not collected in this run")
    # An empty parametrisation is not literally zero items: pytest substitutes a
    # single placeholder case whose id is "NOTSET" and marks it skipped with
    # "got empty parameter set". That placeholder is what made this invisible
    # in the first place — it looks like a collected test — so it must not be
    # counted as one here or this guard would pass over the very hole it exists
    # to find.
    guard_cases = [nodeid for nodeid in collected
                   if IDENTITY_GUARD in nodeid and not nodeid.endswith("[NOTSET]")]
    if optional_wave is None:
        pytest.skip(
            f"{IDENTITY_GUARD} is parametrised over a recorded wave and there "
            "is none: set RF_FIXTURE_DIR=<dir with sessions/*/record.json> "
            "(RF_FIXTURE and DATA_DIR also work). Until then the identity "
            "invariant is NOT being checked by this run.")
    assert guard_cases, (
        f"a wave is present at {optional_wave} but {IDENTITY_GUARD} collected "
        "no cases, so the identity invariant is not being checked. Look at "
        "_fixture_scenarios() in tests/test_runner_blockers.py: an empty "
        "parametrisation is invisible in the pytest report."
    )
