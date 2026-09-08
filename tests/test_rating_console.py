"""Tests for the Phase-2 rating console and the item bank it is fielded with.

The console is HTML and JavaScript, so there is no Python module of mine to
import. What can still be tested, and is worth testing, falls into four groups:

1. **The item bank** (`studies/study1/qualtrics/esci_items.csv`) — it is data,
   it is the file that gets imported into Qualtrics and read by `server/esci.py`
   consumers, and it must agree row for row with the source file it was derived
   from. A drift between the two would put different item text in front of a
   rater than the analysis assumes.
2. **The console's structural invariants** — the promises the page makes that a
   reviewer cannot check by reading it twice: no external dependency, no session
   key, no proprietary item text baked into a public static file, the licensing
   notice present, every element id the script reaches actually in the markup,
   and the endpoints it calls being exactly the ones in the HTTP contract.
3. **The documentation** — that the licensing warning is still in all three
   places it is supposed to be, and that the operational guide still documents
   every route the console uses.
4. **The blinded packet, against the real fixture wave** — the console consumes
   a packet shape, and the fixture is 27 real encounter records. Building the
   packet the console expects out of each of them proves the shape is derivable
   from real data, and that the blinding actually removes what it claims to.

Run from the repo root:

    python -m pytest tests
"""

from __future__ import annotations

import csv
import json
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "static" / "rater.html"
BANK = ROOT / "studies" / "study1" / "qualtrics" / "esci_items.csv"
SOURCE = ROOT / "studies" / "study1" / "qualtrics" / "esci_construct4_items.csv"
INSTRUMENT = ROOT / "studies" / "study1" / "qualtrics" / "rating-instrument.md"
GUIDE = ROOT / "docs" / "RATING.md"
README = ROOT / "README.md"

CONSTRUCTS = ["conflict_management", "influence", "inspirational_leadership", "teamwork"]

# The fixture wave. DATA_DIR wins so the suite can be pointed at any wave; the
# scratchpad path is the one this was developed against. Missing is a skip, not
# a failure — the fixture is not part of the repository.
FIXTURE = Path(os.environ.get("DATA_DIR") or (
    r"C:/Users/benj9/AppData/Local/Temp/claude"
    r"/C--Users-benj9-Downloads-relational-fluency-main--1-"
    r"/4b640cd3-9836-4d35-8114-6f2468c17345/scratchpad/fixture"
))


def read_console() -> str:
    return CONSOLE.read_text(encoding="utf-8")


def read_bank() -> list[dict]:
    with BANK.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def read_source() -> list[dict]:
    with SOURCE.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- #
# 1. the item bank
# --------------------------------------------------------------------------- #

def test_bank_has_all_22_items_once():
    rows = read_bank()
    assert len(rows) == 22
    ids = [r["item_id"] for r in rows]
    assert len(set(ids)) == 22, "duplicate item_id in the bank"


def test_bank_ids_match_the_id_format_the_code_uses():
    # server/esci.py's contract is {id: "ESCI-08", number: 8, ...}: two-digit
    # zero padding, and the number in the id is the ESCI item number, not the
    # row's position. Getting this wrong would silently mis-join every rating.
    for row in read_bank():
        assert re.fullmatch(r"ESCI-\d{2}", row["item_id"]), row["item_id"]
        assert int(row["item_id"].split("-")[1]) == int(row["item_no"])


def test_bank_agrees_with_the_source_file_row_for_row():
    # esci_construct4_items.csv stays the source of truth that server/esci.py
    # loads; esci_items.csv is the export. If the two ever disagree, a rater is
    # being shown text the analysis code has never seen.
    bank, source = read_bank(), read_source()
    assert len(bank) == len(source)
    for b, s in zip(bank, source):
        assert b["item_no"] == s["item_no"]
        assert b["item_text"] == s["item_text"]
        assert b["construct"] == s["competency"]
        assert b["reverse_scored"] == s["reverse_scored"]


def test_bank_constructs_and_counts():
    rows = read_bank()
    by_construct: dict[str, int] = {}
    for r in rows:
        assert r["construct"] in CONSTRUCTS, r["construct"]
        by_construct[r["construct"]] = by_construct.get(r["construct"], 0) + 1
    # The instrument's own counts: 5 / 6 / 5 / 6.
    assert by_construct == {
        "conflict_management": 5, "influence": 6,
        "inspirational_leadership": 5, "teamwork": 6,
    }


def test_bank_reverse_flags_are_exactly_the_three_documented_items():
    reverse = {r["item_no"] for r in read_bank() if r["reverse_scored"] == "TRUE"}
    assert reverse == {"11", "15", "24"}


def test_bank_scale_columns_are_the_esci_scale_with_na_offered():
    for r in read_bank():
        assert r["scale_min"] == "1" and r["scale_max"] == "5"
        assert r["na_allowed"] == "TRUE", "N/A is required on every item"


def test_bank_carries_the_licensing_notice_on_every_row():
    # A repeated column rather than a header comment, because the notice has to
    # survive a load into Qualtrics, a spreadsheet, or a dataframe — none of
    # which would keep a leading '#' line.
    rows = read_bank()
    assert rows, "empty bank"
    for r in rows:
        notice = r["notice"].lower()
        assert "proprietary" in notice
        assert "licens" in notice


def test_bank_is_grouped_in_construct_order():
    # Stable order matters: it is the order server/esci.py's all_items() is
    # expected to hand back, and a reordering would renumber every export.
    seen: list[str] = []
    for r in read_bank():
        if not seen or seen[-1] != r["construct"]:
            seen.append(r["construct"])
    assert seen == CONSTRUCTS


# --------------------------------------------------------------------------- #
# 2. the console
# --------------------------------------------------------------------------- #

def test_console_exists_and_is_a_full_page():
    html = read_console()
    assert html.lstrip().lower().startswith("<!doctype html>")
    for tag in ("<html", "<head", "<body", "</html>"):
        assert tag in html


def test_console_has_no_external_dependency():
    # Every other page here is self-contained and the study runs on a locked-down
    # deployment; a CDN font or script would be a third party inside an IRB
    # session and a page that breaks when it is unreachable.
    html = read_console()
    for pattern in (r'src\s*=\s*["\']https?://', r'href\s*=\s*["\']https?://',
                    r"@import", r"cdn\.", r"fonts\.googleapis", r"unpkg", r"jsdelivr"):
        assert not re.search(pattern, html, re.I), f"external reference: {pattern}"


def test_console_never_touches_the_session_key():
    # Raters must never hold SESSION_KEY. The only credential on this page is the
    # scoped rater token.
    html = read_console()
    assert "SESSION_KEY" not in html
    assert not re.search(r"[?&]key=", html), "session-key query parameter in the rater page"
    assert "params.get('token')" in html


def test_console_only_calls_the_rater_endpoints_in_the_contract():
    html = read_console()
    # Plain string paths, then template-literal paths with their interpolations
    # normalised back to the contract's `{id}` placeholder.
    plain = {m for m in re.findall(r"""api\(\s*'(/[^']*)'""", html) if not m.endswith("/")}
    templated = {re.sub(r"\$\{[^}]*\}", "{id}", m)
                 for m in re.findall(r"api\(\s*`(/[^`]*)`", html)}
    assert plain | templated == {
        "/api/rater/me",
        "/api/rater/assignments",
        "/api/rater/packet/{id}",
        "/api/rater/ratings/{id}",
    }, plain | templated


def test_console_sends_the_token_on_every_request():
    # One helper builds every request, so the token cannot be forgotten on one
    # of them. Assert the helper is the only thing calling fetch.
    html = read_console()
    fetches = re.findall(r"fetch\(", html)
    assert len(fetches) == 1, "more than one fetch site; the token guarantee is per-helper"
    assert "token=${T}" in html


def test_console_does_not_hard_code_the_proprietary_items():
    # The items come down with the packet. Keeping them out of a static file
    # means the bank is not sitting on a public URL, and the randomised order
    # lives in exactly one place.
    html = read_console()
    for row in read_bank():
        assert row["item_text"] not in html, f"item text baked into the console: {row['item_id']}"


def test_console_shows_the_licensing_notice_prominently():
    html = read_console()
    assert 'id="licenceNotice"' in html
    notice_block = html.split('id="licenceNotice"', 1)[1][:900].lower()
    assert "proprietary" in notice_block
    assert "licens" in notice_block
    assert "boyatzis" in notice_block
    # Prominent means before the items, not after them.
    assert html.index('id="licenceNotice"') < html.index('id="items"')


def test_console_offers_na_and_never_preselects_anything():
    html = read_console()
    assert "Not enough information to judge" in html
    # No radio may carry a checked attribute in the markup or the template.
    assert not re.search(r"<input[^>]*type=\"radio\"[^>]*\schecked", html)
    assert 'value="${NA}"' in html or "value=\"${NA}\"" in html


def test_console_sends_na_as_null_not_as_a_number():
    html = read_console()
    assert "v === NA ? null : v" in html


def test_console_refuses_an_incomplete_submission_and_says_what_is_missing():
    html = read_console()
    assert "function missingItems()" in html
    assert "still need an answer" in html
    assert "flagMissing" in html


def test_console_guards_against_double_submission():
    html = read_console()
    assert "if (submitted || inFlight) return;" in html
    assert "inFlight = true;" in html
    # A 409 from the server is an already-recorded rating, which is a success
    # from the rater's side and must not invite a retry.
    assert "409" in html


def test_console_warns_before_leaving_with_unsaved_work():
    html = read_console()
    assert "beforeunload" in html
    assert "e.returnValue" in html


def test_console_makes_careless_rating_cost_a_second_click():
    html = read_console()
    assert "looksStraightLined" in html
    assert "MIN_SECONDS_BEFORE_SUBMIT" in html
    assert "confirmPending" in html
    assert "Submit anyway" in html


def test_console_shows_queue_progress():
    html = read_console()
    assert 'id="queueProgress"' in html
    assert "rated`" in html or "rated'" in html


def test_console_is_keyboard_usable():
    html = read_console()
    assert 'type="radio"' in html, "radios give arrow-key and tab semantics for free"
    assert 'role="radiogroup"' in html
    assert "aria-labelledby" in html
    assert "focus-visible" in html
    assert "keydown" in html


def test_console_handles_an_encounter_with_no_video():
    # Two of the fixture's 27 encounters have no webcam upload; this is a real
    # state, not a defensive branch.
    html = read_console()
    assert "No video for this encounter" in html


def test_console_lays_out_for_a_laptop_and_stacks_when_narrower():
    html = read_console()
    assert "grid-template-columns: 1fr 440px" in html
    assert "@media (max-width: 1100px)" in html


def test_every_element_id_the_script_reaches_exists_in_the_markup():
    html = read_console()
    static_ids = set(re.findall(r'\sid="([A-Za-z0-9_-]+)"', html))
    wanted = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", html))
    wanted |= set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", html))
    # Ids the script itself injects and then reads back.
    injected = {"vid", "nextBtn"}
    missing = wanted - static_ids - injected
    assert not missing, f"script reaches ids that are not in the page: {sorted(missing)}"


def test_console_escapes_every_piece_of_packet_text_it_renders():
    """Packet text is participant speech and scenario prose, and it reaches innerHTML.

    Checked by naming the fields rather than by pattern-matching the file: each
    of these is a string the server puts in the packet, and each one has to be
    wrapped in esc() at the point it is interpolated into markup. A regex over
    "every ${...}" would flag arithmetic and pass a missed field; naming them
    fails loudly when a new field is rendered without going through esc().
    """
    html = read_console()
    assert "const esc =" in html
    for expr in ("esc(text)", "esc(turn.note)", "esc(sit.text",
                 "esc(speakerName(turn))", "esc(it.text || '')",
                 "esc(media.note", "esc(packet.instrument_notice)",
                 "esc(packet.scale_note)", "esc(a.rating_code || a.assignment_id)",
                 "esc(mmss(turn.t))"):
        assert expr in html, f"packet text rendered without escaping: {expr}"


def test_console_never_shows_the_rater_the_construct():
    # The packet carries `construct` — the scenario's primary-competency
    # designation — for the server's and the researcher's benefit. The
    # instrument requires raters to be blind to it, so the page must not render
    # it anywhere.
    html = read_console()
    body = html.split("<script>", 1)[1]
    rendered = re.findall(r"packet\.construct", body)
    for hit in rendered:
        # It may only appear in a comment saying why it is not rendered.
        pass
    for line in body.splitlines():
        if "packet.construct" in line:
            stripped = line.strip()
            assert stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"),                 f"construct reaches the rater: {stripped}"


# --------------------------------------------------------------------------- #
# 3. documentation
# --------------------------------------------------------------------------- #

def test_licensing_warning_is_in_all_three_places():
    for path in (INSTRUMENT, GUIDE, README):
        text = path.read_text(encoding="utf-8").lower()
        assert "proprietary" in text, path
        assert "licens" in text, path


def test_instrument_keeps_the_canonical_licensing_sentence_on_one_line():
    """server/esci.py and server/rater_packet.py quote this sentence verbatim.

    It is the wording that reaches a rater's screen and every export, so the
    instrument document is the one copy the others are checked against — and a
    substring check does not survive a line wrap. Reflowing this paragraph broke
    tests/test_esci.py once already; this fails here first next time.
    """
    doc = INSTRUMENT.read_text(encoding="utf-8").lower()
    assert "confirm licensing/permission before fielding" in doc
    assert "items reproduced for research reference only" in doc


def test_instrument_warning_is_near_the_top_and_not_buried():
    text = INSTRUMENT.read_text(encoding="utf-8")
    assert text.lower().index("proprietary") < 700, "the warning has drifted down the page"


def test_instrument_describes_both_routes():
    text = INSTRUMENT.read_text(encoding="utf-8")
    assert "rater.html" in text
    assert "Qualtrics" in text
    assert "docs/RATING.md" in text


def test_guide_documents_every_route_in_the_http_contract():
    text = GUIDE.read_text(encoding="utf-8")
    for route in ("/rate", "/api/raters", "/api/rater-assignments",
                  "/api/ratings", "/api/reliability"):
        assert route in text, route


def test_guide_covers_the_operational_steps():
    text = GUIDE.read_text(encoding="utf-8").lower()
    for topic in ("register", "token", "assign", "reliability",
                  "icc", "kappa" if "kappa" in text else "κ", "krippendorff"):
        assert topic in text, topic


def test_guide_lists_the_researchers_open_questions():
    text = GUIDE.read_text(encoding="utf-8").lower()
    for topic in ("licens", "payment", "calibrat", "threshold", "route of record"):
        assert topic in text, topic


def test_readme_has_a_phase_2_section_next_to_scoring():
    text = README.read_text(encoding="utf-8")
    assert "## Phase 2" in text
    assert text.index("## Scoring") < text.index("## Phase 2")
    assert "/rate?token=" in text


# --------------------------------------------------------------------------- #
# 4. the blinded packet, against the fixture wave
# --------------------------------------------------------------------------- #

# Everything a rater must not be able to see. `construct` is deliberately NOT
# on this list — server/rater_packet.py puts it in the packet on purpose and the
# console refuses to render it (see the test above) — but the scenario id and
# variant are, because they name the same designation and point at the spec file
# holding every planted beat and its scoring anchors.
FORBIDDEN_PACKET_KEYS = {
    "participant_key", "participant_id", "run_id", "cohort", "encounter_index",
    "steering_log", "stage_direction", "trigger_id", "esci", "spec_fingerprint",
    "instructions_sha256", "score", "scenario", "variant", "agent_id", "voice",
    "encounter_id", "session_id", "provenance",
}


def blind(record: dict) -> dict:
    """The packet shape static/rater.html consumes, built from an encounter record.

    This mirrors what server/rater_packet.build produces, and it is written out
    here rather than imported so that the *console's* half of the boundary is
    stated as executable data. If the packet builder changes shape, the console
    tests that read this go on describing what the page needs, and the
    integration test below is the one that fails.
    """
    names = {a.get("id"): a.get("name") for a in (record.get("cast") or [])}
    turns = []
    for t in record.get("transcript", []):
        role = t.get("role")
        turns.append({
            "t": t.get("t"),
            "role": "participant" if role == "participant" else "agent",
            "speaker": "Participant" if role == "participant"
                       else (names.get(t.get("agent_id")) or "The other speaker"),
            "text": t.get("text") or "",
            "interrupted": bool(t.get("interrupted")),
            "transcript_missing": bool(t.get("transcript_missing")),
            "note": None,
        })
    return {
        "rating_code": "RC-ABCDEFGHIJ",
        "construct": "conflict_management",
        "situation": {"text": "You are a team lead. Late last night a colleague sent…",
                      "assets": [], "people": [{"name": "Mel", "role": "teammate"}],
                      "parts": [{"label": "Hallway run-in", "mode": "1:1", "with": ["Mel"]}]},
        "transcript": turns,
        "duration_s": 421.0,
        "duration_display": "7:01",
        "counts": {
            "participant_turns": sum(1 for t in turns if t["role"] == "participant"),
            "agent_turns": sum(1 for t in turns if t["role"] == "agent"),
            "interrupted_turns": sum(1 for t in turns if t["interrupted"]),
            "untranscribed_turns": sum(1 for t in turns if t["transcript_missing"]),
        },
        "media": {"video_url": None, "video_available": False,
                  "expires_in": None, "note": "No webcam recording…"},
        "scale_note": "Rate the participant, not the other speakers…",
        "instrument_notice": "Proprietary instrument — confirm licensing before fielding.",
        # Added by the rater route, not by rater_packet.build: the console needs
        # the item bank and the assignment it is answering.
        "assignment_id": "as_0123456789ab",
        "status": "pending",
        "items": [{"id": r["item_id"], "number": int(r["item_no"]),
                   "text": r["item_text"], "reverse": r["reverse_scored"] == "TRUE"}
                  for r in read_bank()],
    }


def fixture_records() -> list[Path]:
    sessions = FIXTURE / "sessions"
    if not sessions.is_dir():
        pytest.skip(f"no fixture wave at {FIXTURE}")
    found = sorted(sessions.glob("*/record.json"))
    if not found:
        pytest.skip(f"no encounter records under {sessions}")
    return found


def test_fixture_wave_is_the_expected_size():
    assert len(fixture_records()) == 27


def test_every_fixture_encounter_yields_a_packet_the_console_can_render():
    for path in fixture_records():
        record = json.loads(path.read_text(encoding="utf-8"))
        packet = blind(record)
        # The fields the console dereferences.
        assert isinstance(packet["items"], list) and len(packet["items"]) == 22
        assert isinstance(packet["transcript"], list)
        assert isinstance(packet["situation"], dict)
        assert isinstance(packet["media"], dict)
        assert "video_url" in packet["media"]
        assert set(packet["counts"]) >= {
            "participant_turns", "agent_turns", "interrupted_turns", "untranscribed_turns"}
        assert packet["rating_code"].startswith("RC-")
        for turn in packet["transcript"]:
            assert turn["role"] in ("participant", "agent"), turn["role"]
            assert isinstance(turn["text"], str)
            assert isinstance(turn["speaker"], str) and turn["speaker"]
            assert turn["t"] is None or isinstance(turn["t"], (int, float))


def test_blinded_packet_carries_none_of_the_instrument_internals():
    for path in fixture_records():
        record = json.loads(path.read_text(encoding="utf-8"))
        packet = blind(record)
        blob = json.dumps(packet, ensure_ascii=False)
        for key in FORBIDDEN_PACKET_KEYS:
            assert f'"{key}"' not in blob, f"{key} leaked into the packet for {path.parent.name}"
        # And no literal value from the record's blinded fields.
        for field in ("participant_key", "participant_id", "run_id"):
            value = record.get(field)
            if value:
                assert str(value) not in blob, f"{field} value leaked for {path.parent.name}"
        for direction in record.get("steering_log", []):
            text = direction.get("stage_direction")
            if text:
                assert text not in blob, f"stage direction leaked for {path.parent.name}"


def test_a_speaker_change_is_visible_in_the_blinded_transcript():
    # The console draws a scene break when the counterpart changes, and it does
    # it from the speaker label, because the packet drops agent_id. If the
    # blinding also flattened the labels, a two-character encounter would read
    # as one undifferentiated voice and a rater could not tell who was who.
    multi = 0
    for path in fixture_records():
        record = json.loads(path.read_text(encoding="utf-8"))
        packet = blind(record)
        speakers = {t["speaker"] for t in packet["transcript"] if t["role"] == "agent"}
        if len(speakers) > 1:
            multi += 1
        assert "The other speaker" not in speakers or not record.get("cast"), (
            f"an agent turn lost its cast name in {path.parent.name}"
        )
    assert multi > 0, "no encounter in the wave has more than one counterpart"


def test_the_console_video_url_cannot_come_from_record_json():
    # Every record.json in a real wave has "video": [] — record.json is written
    # at session close and the browser's S3 upload lands afterwards. So the
    # packet's video_url has to be computed from server/video.playback_url (or
    # from the video_uploaded event), never read out of the stored record.
    # This is a live platform defect, and the packet builder must not inherit it.
    empty = 0
    for path in fixture_records():
        record = json.loads(path.read_text(encoding="utf-8"))
        if not record.get("video"):
            empty += 1
    assert empty == len(fixture_records()), (
        "some records now carry video; the packet builder may read it from there"
    )


def test_transcript_gaps_in_the_wave_are_renderable_states_not_crashes():
    """The console renders a truncated or lost agent line as an explicit note.

    Read from events.jsonl, not from record.json — and that is the point. The
    runner writes `interrupted` and `transcript_missing` onto the steering_pair
    event precisely so a rater can tell a truncated delivery from a bad one, and
    the stored record.json in the reference wave (written before
    encounter_record.build learned to copy them) carries neither. A packet built
    from the stored record would show a rater an empty agent line with nothing
    saying why, and they would score a gateway failure as a weak exchange.
    server/rater_packet.build rebuilds the record from events, which is what
    makes these renderable at all.
    """
    stored = flagged = 0
    for path in fixture_records():
        record = json.loads(path.read_text(encoding="utf-8"))
        for turn in record.get("transcript", []):
            if turn.get("interrupted") or turn.get("transcript_missing"):
                stored += 1
        events = path.parent / "events.jsonl"
        for line in events.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") != "steering_pair":
                continue
            actor = event.get("actor") or {}
            if actor.get("interrupted") or actor.get("transcript_missing"):
                flagged += 1
    assert flagged > 0, "the wave no longer carries a truncated or lost agent turn"
    assert stored == 0, (
        "record.json now carries the delivery flags; the packet may read it "
        "directly instead of rebuilding from events"
    )
    html = read_console()
    assert "transcript was lost" in html
    assert "cut off" in html
    assert "turn.note" in html, "the packet's own neutral wording for the flags is not used"


def test_console_warns_once_at_the_top_about_flagged_turns():
    # A marker halfway down a ten-turn transcript is a marker a rater scrolls
    # past, so the packet's counts are surfaced as a banner as well.
    html = read_console()
    assert 'id="turnFlags"' in html
    assert "untranscribed_turns" in html
    assert "interrupted_turns" in html


def test_presigning_a_playback_url_is_a_local_computation():
    # The console plays whatever URL the packet hands it, and that URL is a
    # presigned S3 GET. Presigning is signature arithmetic, not a request: it
    # must work with no network and no valid credentials, which is what makes it
    # testable at all. No AWS call is made here.
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config

    session = boto3.session.Session(
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region_name="us-east-1",
    )
    client = session.client("s3", config=Config(signature_version="s3v4"))
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": "relational-fluency-study-data",
                "Key": "encounters/s_1772460300_44c9a2/webcam.webm"},
        ExpiresIn=3600,
    )
    assert url.startswith("https://")
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Expires=3600" in url
    assert "encounters/s_1772460300_44c9a2/webcam.webm" in url


def test_the_real_packet_builder_produces_what_the_console_reads(monkeypatch):
    """Exercise server/rater_packet.build against the wave, and check the seams.

    The mirror above says what the console needs; this says the builder produces
    it. S3 is unreachable from here and a rating packet must not depend on a
    round trip anyway, so both video calls are replaced: `upload_receipt` with a
    local answer, `playback_url` with a locally-signed URL. Presigning is
    signature arithmetic, not a request — the test above proves that separately
    — so stubbing the receipt lookup is the only thing being faked.
    """
    sessions = FIXTURE / "sessions"
    if not sessions.is_dir():
        pytest.skip(f"no fixture wave at {FIXTURE}")
    rater_packet = pytest.importorskip("server.rater_packet")
    from server import video as video_mod

    monkeypatch.setattr(rater_packet, "SESSIONS_DIR", sessions, raising=False)
    monkeypatch.setattr(video_mod, "upload_receipt", lambda sid: {"key": f"encounters/{sid}/webcam.webm", "bytes": 1024},
                        raising=False)
    monkeypatch.setattr(video_mod, "playback_url",
                        lambda sid, seconds=3600: f"https://example.invalid/{sid}?X-Amz-Signature=deadbeef",
                        raising=False)

    checked = 0
    for path in sorted(sessions.glob("*/record.json")):
        packet = rater_packet.build(path.parent.name)
        assert packet, path.parent.name
        # Everything the console dereferences on a packet.
        assert packet["rating_code"].startswith("RC-")
        assert isinstance(packet["situation"], dict)
        assert isinstance(packet["transcript"], list)
        assert isinstance(packet["media"], dict) and "video_url" in packet["media"]
        assert isinstance(packet["counts"], dict)
        assert packet["instrument_notice"] and "roprietary" in packet["instrument_notice"]
        for turn in packet["transcript"]:
            assert turn["role"] in ("participant", "agent")
            assert isinstance(turn["speaker"], str) and turn["speaker"]
            assert isinstance(turn["text"], str)
        # And nothing the console must never be handed.
        blob = json.dumps(packet, ensure_ascii=False, default=str)
        for key in FORBIDDEN_PACKET_KEYS - {"encounter_id", "session_id"}:
            assert f'"{key}"' not in blob, f"{key} leaked for {path.parent.name}"
        checked += 1
    assert checked == 27


# --------------------------------------------------------------------------- #
# 5. driving the console's own JavaScript
# --------------------------------------------------------------------------- #

# The console is the deliverable, and everything above it reads the file rather
# than runs it. This runs it: the page's script is executed in a Node vm against
# packets that server/rater_packet.py built from the fixture wave, with a DOM
# stub thin enough to be honest about what it is. The one place the stub is not
# thin is the item list — querySelectorAll parses the markup the page actually
# wrote and hands back radios carrying the page's own change listeners, so the
# answers in this test are entered by clicking the page's radios rather than by
# reaching into its state (which is in `let` bindings a vm context cannot touch
# anyway). Skipped where node is not installed; it is not a runtime dependency.

CONSOLE_HARNESS = r"""/* Drives static/rater.html's own script in a Node vm against packets built by
   server/rater_packet.py from the fixture wave.

   The DOM stub is deliberately thin, with one exception: querySelectorAll on
   the item list parses the markup the page actually wrote and hands back radio
   stubs carrying the page's own change listeners. That is what makes this an
   exercise rather than an inspection: answers are entered by clicking the
   page's radios, not by reaching into its state. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const stub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const m = html.match(/<script>([\s\S]*)<\/script>/);
assert(m, 'no script block in the console');

const written = {};
const radioCache = new Map();

function parseRadios(markup) {
  if (radioCache.has(markup)) return radioCache.get(markup);
  const out = [];
  const re = /<input type="radio" name="([^"]+)"\s+value="([^"]+)"/g;
  let hit;
  while ((hit = re.exec(markup))) {
    out.push({
      type: 'radio', name: hit[1], value: hit[2], checked: false, disabled: false,
      _on: [], addEventListener(ev, fn) { if (ev === 'change') this._on.push(fn); },
      dispatchEvent() { this._on.forEach(fn => fn()); }, focus() {},
    });
  }
  radioCache.set(markup, out);
  return out;
}

function el(id) {
  return {
    id, style: {}, dataset: {}, value: '', disabled: false, className: '',
    set innerHTML(v) { written[id] = v; }, get innerHTML() { return written[id] || ''; },
    set textContent(v) { written[id + ':text'] = v; }, get textContent() { return written[id + ':text'] || ''; },
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, focus() {}, scrollIntoView() {}, appendChild() {},
    closest() { return null; },
    querySelectorAll(sel) {
      if (id === 'items' && /input/.test(sel)) return parseRadios(written.items || '');
      return [];
    },
    querySelector(sel) {
      const hit = /input\[value="([^"]+)"\]/.exec(sel);
      if (hit) return parseRadios(written.items || '').find(r => r.value === hit[1]) || null;
      if (/radio/.test(sel)) return parseRadios(written.items || '')[0] || null;
      return null;
    },
  };
}

const els = {};
const calls = [];
const ctx = {
  console, JSON, Math, Date, Object, Array, String, Number, Boolean,
  parseInt, parseFloat, isNaN, URLSearchParams, encodeURIComponent, Promise, Error,
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
  alert(msg) { calls.push(['alert', msg]); },
  confirm: () => true,
  CSS: { escape: (s) => s },
  Event: class { constructor(t) { this.type = t; } },
  fetch: async (url, opts) => {
    calls.push(['fetch', String(url), opts && opts.body]);
    const u = String(url).split('?')[0];
    let body = null, status = 200;
    if (u.endsWith('/api/rater/me')) body = stub.me;
    else if (u.endsWith('/api/rater/assignments')) body = stub.assignments;
    else if (u.includes('/api/rater/packet/')) { body = stub.packets[u.split('/').pop()]; if (!body) status = 404; }
    else if (u.includes('/api/rater/ratings/')) body = { ok: true, submitted_at: 'now' };
    else status = 404;
    return { ok: status < 400, status, json: async () => body };
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(m[1], ctx, { filename: 'rater.html' });   // parses, and runs boot()

const posts = () => calls.filter(c => c[0] === 'fetch' && c[1].includes('/ratings/'));
const answer = (name, value) => {
  const r = parseRadios(written.items).find(x => x.name === name && x.value === value);
  assert(r, 'no radio ' + name + '=' + value);
  r.checked = true;
  r.dispatchEvent();
};

(async () => {
  await new Promise(r => setTimeout(r, 0));

  // --- the queue -----------------------------------------------------------
  assert(written.qlist.includes(stub.assignments[0].rating_code), 'queue lacks the rating code');
  assert(!/s_\d{5,}_/.test(written.qlist), 'a session id reached the queue');
  assert(written['queueProgress:text'].includes('of 3'), 'no queue progress');

  // --- one encounter -------------------------------------------------------
  const A = stub.assignments[0].assignment_id;
  await ctx.openAssignment(A);
  assert(written.situation.includes('participant was told'), 'situation missing');
  assert(written.turns.includes('Participant'), 'transcript missing');
  assert(!/agent_id|stage_direction|trigger_id/.test(written.turns), 'blinding leak in the transcript');
  const items = stub.packets[A].items;
  const radios = parseRadios(written.items);
  assert(radios.length === items.length * 6, 'expected ' + items.length * 6 + ' radios, got ' + radios.length);
  assert(!/checked/.test(written.items), 'something is pre-selected');
  assert(/N\/A/.test(written.items), 'no N/A option');
  assert(written.items.indexOf(items[0].text) >= 0, 'items not rendered in the order served');

  // --- refuses an incomplete submission, and says what is missing -----------
  await ctx.onSubmit();
  assert(/22 statements still need an answer/.test(written['submitMsg:text']),
    'incomplete submit not refused: ' + written['submitMsg:text']);
  assert(posts().length === 0, 'an incomplete rating was posted');

  items.slice(0, 20).forEach(i => answer(i.id, '3'));
  await ctx.onSubmit();
  assert(/2 statements still need an answer/.test(written['submitMsg:text']),
    'partial submit not refused: ' + written['submitMsg:text']);
  assert(/N\/A is a real answer/.test(written['submitMsg:text']), 'the N/A nudge is missing');
  assert(posts().length === 0, 'a partial rating was posted');

  // --- straight-lining costs a second click --------------------------------
  items.slice(20).forEach(i => answer(i.id, '3'));
  await ctx.onSubmit();
  assert(/same answer/.test(written['submitMsg:text']), 'no straight-lining warning');
  assert(posts().length === 0, 'a straight-lined rating went through on the first click');
  await ctx.onSubmit();
  assert(posts().length === 1, 'the second click did not submit');

  const body = JSON.parse(posts()[0][2]);
  assert(Object.keys(body.scores).length === 22, 'not all 22 items in the body');
  assert(Object.values(body.scores).every(v => v === 3), 'scores mangled');
  assert(typeof body.seconds === 'number', 'seconds missing');
  assert('better' in body.open_ended && 'notable' in body.open_ended, 'open_ended shape wrong');

  // --- a second submit on the same assignment does nothing ------------------
  await ctx.onSubmit();
  assert(posts().length === 1, 'a second submit went through');

  // --- N/A travels as null --------------------------------------------------
  const B = stub.assignments[1].assignment_id;
  await ctx.openAssignment(B);
  const bItems = stub.packets[B].items;
  bItems.forEach((i, k) => answer(i.id, k === 0 ? 'na' : String((k % 5) + 1)));
  // Not straight-lined this time, so the other soft gate fires: nothing was
  // played and no time was spent. One warning, then a second click submits.
  await ctx.onSubmit();
  assert(/not played the recording/.test(written['submitMsg:text']),
    'no warning for a rating submitted without watching: ' + written['submitMsg:text']);
  assert(posts().length === 1, 'an unwatched rating went through on the first click');
  await ctx.onSubmit();
  assert(posts().length === 2, 'the varied rating did not submit: ' + written['submitMsg:text']);
  const b2 = JSON.parse(posts()[1][2]);
  assert(b2.scores[bItems[0].id] === null, 'N/A was not sent as null');
  assert(b2.scores[bItems[1].id] === 2, 'a numeric score was mangled');
  assert(Object.values(b2.scores).filter(v => v === null).length === 1, 'more nulls than N/As');

  // the untranscribed turn in this encounter is called out, not left blank
  assert(/transcript was lost|not captured/.test(written.turns), 'a lost line renders as silence');
  assert(/not transcribed/.test(written['turnFlags:text']), 'no banner for the flagged turn');

  // --- an encounter with no video ------------------------------------------
  await ctx.openAssignment(stub.assignments[2].assignment_id);
  assert(/No video for this encounter/.test(written.videoSlot), 'no-video state not rendered');
  assert(/N\/A|Not enough information to judge/.test(written.videoSlot),
    'the no-video state does not point the rater at the N/A option');

  console.log('CONSOLE OK');
})().catch(e => { console.error('FAIL: ' + e.message); process.exit(1); });
"""


def test_the_console_runs(tmp_path, monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the console harness needs it")
    sessions = FIXTURE / "sessions"
    if not sessions.is_dir():
        pytest.skip(f"no fixture wave at {FIXTURE}")
    rater_packet = pytest.importorskip("server.rater_packet")
    from server import video as video_mod

    # Three encounters chosen for what they exercise: an ordinary one, one
    # carrying an untranscribed agent turn, and one whose webcam upload never
    # landed. No AWS call is made — the two video seams are replaced.
    ordinary, untranscribed, novideo = (
        "s_1772460300_44c9a2", "s_1772548516_02952d", "s_1772764657_717245")
    monkeypatch.setattr(rater_packet, "SESSIONS_DIR", sessions, raising=False)
    monkeypatch.setattr(
        video_mod, "upload_receipt",
        lambda sid: None if sid == novideo else {"key": f"encounters/{sid}/webcam.webm", "bytes": 4096},
        raising=False)
    monkeypatch.setattr(
        video_mod, "playback_url",
        lambda sid, seconds=3600: f"https://example.invalid/{sid}.webm?X-Amz-Signature=deadbeef",
        raising=False)

    bank = read_bank()
    packets = {}
    for i, sid in enumerate((ordinary, untranscribed, novideo)):
        packet = rater_packet.build(sid)
        assert packet, sid
        packet["assignment_id"] = f"as_00000000000{i}"
        packet["status"] = "pending"
        items = [{"id": r["item_id"], "number": int(r["item_no"]), "text": r["item_text"],
                  "construct": r["construct"], "reverse": r["reverse_scored"] == "TRUE"}
                 for r in bank]
        # Stand in for the server's per-rater randomisation, so the test also
        # proves the page renders the order it is handed rather than sorting.
        random.Random(7 + i).shuffle(items)
        packet["items"] = items
        packets[packet["assignment_id"]] = packet

    stub = {
        "me": {"rater_id": "rtr_ab12cd34", "name": "R. Okonkwo", "kind": "trained",
               "assignments_pending": 3},
        "assignments": [{"assignment_id": a, "rating_code": p["rating_code"],
                         "status": "pending", "assigned_at": "2026-04-01T14:05:00Z"}
                        for a, p in packets.items()],
        "packets": packets,
    }
    stub_path = tmp_path / "stub.json"
    stub_path.write_text(json.dumps(stub), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(CONSOLE_HARNESS, encoding="utf-8")

    proc = subprocess.run([node, str(harness), str(CONSOLE), str(stub_path)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CONSOLE OK" in proc.stdout, proc.stdout + proc.stderr
