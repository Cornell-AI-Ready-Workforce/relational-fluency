"""Tests for server/esci.py, the Phase 2 item bank.

The bank is the instrument. If it loads short, loads in the wrong order, or gets
the direction of the three reverse items wrong, every gold label collected under
it is wrong in a way that no downstream check would notice, so the tests here go
after those three failures specifically and then after the edge cases the rating
console will actually hand `validate`.

The crosswalk tests re-derive the slug table from scenarios/v3/*.yaml rather than
restating it, so a spec that invents a new trigger tag fails here instead of
silently dropping out of any analysis that joins triggers to items.
"""

from __future__ import annotations

import csv
import glob
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import esci  # noqa: E402


# --- Loading -----------------------------------------------------------------

def test_loads_every_row_of_the_csv():
    with esci.ITEMS_CSV.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("item_no") or "").strip()]
    assert len(esci.ITEMS) == len(rows) == 22
    assert [i["number"] for i in esci.ITEMS] == [int(r["item_no"]) for r in rows]
    assert [i["text"] for i in esci.ITEMS] == [r["item_text"].strip() for r in rows]


def test_item_shape_matches_the_contract():
    for it in esci.ITEMS:
        assert set(it) == {"id", "number", "text", "construct", "reverse"}
        assert it["id"] == f"ESCI-{it['number']:02d}"
        assert it["construct"] in esci.CONSTRUCTS
        assert isinstance(it["reverse"], bool)
        assert it["text"]


def test_construct_counts_match_the_instrument():
    # rating-instrument.md: 5 conflict management, 6 influence, 5 inspirational
    # leadership, 6 teamwork.
    expected = {
        "conflict_management": 5,
        "influence": 6,
        "inspirational_leadership": 5,
        "teamwork": 6,
    }
    assert {c: len(esci.items_for(c)) for c in esci.CONSTRUCTS} == expected
    assert sum(expected.values()) == len(esci.ITEMS)


def test_all_items_order_is_stable_and_copies_are_handed_out():
    first, second = esci.all_items(), esci.all_items()
    assert [i["id"] for i in first] == [i["id"] for i in second]
    first[0]["text"] = "vandalised"
    first[0]["extra"] = True
    assert esci.all_items()[0]["text"] != "vandalised"
    assert "extra" not in esci.all_items()[0]
    # items_for hands out copies too
    got = esci.items_for("teamwork")
    got[0]["reverse"] = not got[0]["reverse"]
    assert esci.items_for("teamwork")[0]["reverse"] is esci.item("ESCI-11")["reverse"]


def test_items_for_rejects_an_unknown_construct():
    # Returning [] would render an empty rating block and look like a UI bug.
    with pytest.raises(ValueError):
        esci.items_for("teamork")
    with pytest.raises(ValueError):
        esci.items_for("empathy")


def test_scale_constants():
    assert esci.SCALE_MIN == 1
    assert esci.SCALE_MAX == 5
    assert esci.NA is None
    assert set(esci.SCALE_LABELS) == {1, 2, 3, 4, 5}


# --- item() lookup -----------------------------------------------------------

def test_item_by_canonical_id():
    it = esci.item("ESCI-08")
    assert it["number"] == 8
    assert it["construct"] == "conflict_management"
    assert it["reverse"] is False


@pytest.mark.parametrize("form", ["ESCI-08", "esci-08", "ESCI-8", "esci_8",
                                  " ESCI-08 ", "8", 8])
def test_item_accepts_the_forms_callers_actually_hold(form):
    assert esci.item(form)["id"] == "ESCI-08"


@pytest.mark.parametrize("bad", ["ESCI-99", "ESCI-", "", "   ", None, 99, 0,
                                 -8, "eight", True, False, 3.5, ["ESCI-08"]])
def test_item_returns_none_for_anything_that_is_not_an_item(bad):
    assert esci.item(bad) is None


def test_bool_is_not_an_item_number():
    # True == 1 in Python, and item 1 does not exist, but item numbers that do
    # exist would be reachable this way in another bank. Reject the type.
    assert esci.item(True) is None


# --- Reverse scoring, the thing most worth getting right ---------------------

def test_exactly_three_items_are_reverse_scored():
    assert esci.REVERSE_ITEMS == ["ESCI-15", "ESCI-24", "ESCI-11"]
    assert {esci.item(i)["number"] for i in esci.REVERSE_ITEMS} == {11, 15, 24}


def test_reverse_items_are_the_negatively_worded_ones():
    assert esci.item("ESCI-15")["text"] == "Allows conflict to fester"
    assert esci.item("ESCI-24")["text"] == "Does not inspire followers"
    assert esci.item("ESCI-11")["text"] == "Does not cooperate with others"


def test_never_allowing_conflict_to_fester_is_top_marks():
    # The brief's own example: a raw 1 on "Allows conflict to fester" is a 5 on
    # the construct. If this ever flips, three items invert silently.
    assert esci.score_value("ESCI-15", 1) == 5
    assert esci.score_value("ESCI-15", 5) == 1
    assert esci.score_value("ESCI-24", 2) == 4
    assert esci.score_value("ESCI-11", 4) == 2


def test_reverse_scoring_is_its_own_inverse_and_fixes_the_midpoint():
    for iid in esci.REVERSE_ITEMS:
        assert esci.score_value(iid, 3) == 3
        for raw in range(1, 6):
            assert esci.score_value(iid, esci.score_value(iid, raw)) == raw


def test_forward_items_are_untouched():
    for it in esci.ITEMS:
        if it["reverse"]:
            continue
        for raw in range(1, 6):
            assert esci.score_value(it["id"], raw) == raw


def test_na_survives_scoring_as_null_on_both_polarities():
    # N/A must never become a number: 6 - None would be a crash, and a reverse
    # item that turned N/A into 5 would manufacture evidence.
    assert esci.score_value("ESCI-15", None) is None
    assert esci.score_value("ESCI-08", None) is None


@pytest.mark.parametrize("raw,expect", [("4", 4), (" 4 ", 4), (4.0, 4)])
def test_score_value_accepts_the_qualtrics_export_forms(raw, expect):
    assert esci.score_value("ESCI-08", raw) == expect
    assert esci.score_value("ESCI-15", raw) == 6 - expect


@pytest.mark.parametrize("raw", [0, 6, -1, 99, 4.5, "4.5", "", "  ", "four",
                                 True, False, [], {}, object()])
def test_score_value_rejects_anything_off_the_scale(raw):
    with pytest.raises(ValueError):
        esci.score_value("ESCI-08", raw)


def test_score_value_raises_on_an_unknown_item_rather_than_returning_none():
    # None already means N/A. A typo'd id that scored as N/A would be
    # indistinguishable from an honest "cannot tell".
    with pytest.raises(ValueError):
        esci.score_value("ESCI-99", 3)
    with pytest.raises(ValueError):
        esci.score_value("", None)


def test_scored_reverse_codes_a_whole_submission_and_canonicalises_keys():
    out = esci.scored({"ESCI-08": 2, "15": 1, "ESCI-24": None, 11: 5})
    assert out == {"ESCI-08": 2, "ESCI-15": 5, "ESCI-24": None, "ESCI-11": 1}


def test_scored_raises_on_an_unknown_key():
    with pytest.raises(ValueError):
        esci.scored({"ESCI-08": 3, "ESCI-99": 3})


def test_scored_of_empty_is_empty():
    assert esci.scored({}) == {}
    assert esci.scored(None) == {}


# --- validate ----------------------------------------------------------------

def _full(value=3):
    return {it["id"]: value for it in esci.ITEMS}


def test_a_complete_submission_validates():
    assert esci.validate(_full(3)) == []


def test_all_na_validates():
    # A rater who genuinely could not judge anything is submitting a legitimate
    # (if useless) response; that is a rater-quality question, not a schema one.
    assert esci.validate(_full(None)) == []


def test_every_point_on_the_scale_validates():
    for v in range(esci.SCALE_MIN, esci.SCALE_MAX + 1):
        assert esci.validate(_full(v)) == []


def test_out_of_range_scores_are_rejected():
    for bad in (0, 6, -1, 42):
        scores = _full(3)
        scores["ESCI-20"] = bad
        problems = esci.validate(scores)
        assert len(problems) == 1
        assert "ESCI-20" in problems[0]


@pytest.mark.parametrize("bad", [4.5, "high", "", True, False, [3], {"v": 3}])
def test_non_scale_values_are_rejected(bad):
    scores = _full(3)
    scores["ESCI-20"] = bad
    assert any("ESCI-20" in p for p in esci.validate(scores))


def test_string_digits_are_accepted_because_qualtrics_sends_them():
    scores = {it["id"]: "4" for it in esci.ITEMS}
    assert esci.validate(scores) == []


def test_unknown_item_id_is_rejected():
    scores = _full(3)
    scores["ESCI-99"] = 3
    problems = esci.validate(scores)
    assert any("ESCI-99" in p for p in problems)


def test_missing_items_are_rejected_and_named():
    scores = _full(3)
    del scores["ESCI-56"]
    del scores["ESCI-03"]
    problems = esci.validate(scores)
    assert len(problems) == 1
    assert "ESCI-03" in problems[0] and "ESCI-56" in problems[0]
    assert "missing 2 of 22" in problems[0]


def test_empty_submission_is_rejected():
    problems = esci.validate({})
    assert problems and "missing 22 of 22" in problems[0]


def test_a_non_dict_is_rejected_without_exploding():
    for bad in (None, [], "scores", 3, object()):
        problems = esci.validate(bad)
        assert len(problems) == 1
        assert "object" in problems[0]


def test_the_same_item_named_twice_is_a_duplicate_not_an_overwrite():
    # "8" and "ESCI-08" both resolve; collapsing them would silently discard one
    # of two contradictory answers from the same rater.
    scores = _full(3)
    scores["8"] = 5
    problems = esci.validate(scores)
    assert len(problems) == 1
    assert "ESCI-08 rated more than once" in problems[0]


def test_validate_reports_every_problem_at_once():
    # The caller is an HTTP handler talking to a rater looking at the form.
    scores = _full(3)
    del scores["ESCI-46"]
    scores["ESCI-20"] = 9
    scores["ESCI-77"] = 3
    problems = esci.validate(scores)
    assert len(problems) == 3
    joined = " | ".join(problems)
    assert "ESCI-20" in joined and "ESCI-77" in joined and "ESCI-46" in joined


def test_validated_submissions_always_score():
    scores = _full(3)
    scores["ESCI-15"] = None
    scores["ESCI-08"] = "1"
    assert esci.validate(scores) == []
    out = esci.scored(scores)
    assert len(out) == 22
    assert out["ESCI-15"] is None
    assert out["ESCI-08"] == 1


# --- The proprietary-items notice --------------------------------------------

def test_notice_names_the_instrument_and_the_licensing_condition():
    n = esci.NOTICE.lower()
    assert "proprietary" in n
    assert "licens" in n
    assert "research reference" in n


def test_notice_matches_the_instrument_document():
    doc = (ROOT / "studies" / "study1" / "qualtrics" /
           "rating-instrument.md").read_text(encoding="utf-8").lower()
    assert "proprietary instrument" in doc
    assert "confirm licensing/permission before fielding" in doc


def test_item_bank_always_carries_the_notice():
    # The notice has to arrive attached to the items, not in a README. Every
    # shape this function can produce carries it.
    for kwargs in ({}, {"construct": "influence"}, {"include_text": False},
                   {"construct": "teamwork", "include_text": False}):
        bank = esci.item_bank(**kwargs)
        assert bank["notice"] == esci.NOTICE
        assert bank["items"]


def test_item_bank_full_shape():
    bank = esci.item_bank()
    assert len(bank["items"]) == 22
    assert [c["key"] for c in bank["constructs"]] == esci.CONSTRUCTS
    assert bank["scale"] == {
        "min": 1, "max": 5, "labels": esci.SCALE_LABELS,
        "na_label": esci.NA_LABEL, "na_value": None,
    }
    row = bank["items"][0]
    assert set(row) == {"id", "number", "construct", "reverse", "slug", "text"}
    assert row["id"] == "ESCI-08"
    assert row["slug"] == "resolve_not_fester"


def test_item_bank_can_withhold_the_wording():
    bank = esci.item_bank(include_text=False)
    assert all("text" not in r for r in bank["items"])
    # ...and still says where the wording came from.
    assert bank["notice"] == esci.NOTICE


def test_item_bank_for_one_construct():
    bank = esci.item_bank("conflict_management")
    assert [r["id"] for r in bank["items"]] == \
        ["ESCI-08", "ESCI-14", "ESCI-15", "ESCI-26", "ESCI-46"]
    assert [c["key"] for c in bank["constructs"]] == ["conflict_management"]


def test_item_bank_rejects_an_unknown_construct():
    with pytest.raises(ValueError):
        esci.item_bank("nonsense")


# --- The crosswalk to the scenario specs' trigger tags -----------------------

def _spec_slugs():
    """{slug: declared text} across every v3 spec, plus the slugs actually used."""
    declared, used = {}, set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "esci" and isinstance(v, list):
                    used.update(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for path in sorted(glob.glob(str(ROOT / "scenarios" / "v3" / "*.yaml"))):
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
        for slug, text in (spec.get("esci_items") or {}).items():
            declared[slug] = text
        walk(spec)
    return declared, used


def test_crosswalk_is_a_bijection():
    assert len(esci.SLUG_TO_ITEM) == len(esci.ITEM_TO_SLUG) == 22
    assert set(esci.SLUG_TO_ITEM.values()) == {i["id"] for i in esci.ITEMS}


def test_crosswalk_covers_every_slug_the_specs_declare_or_use():
    declared, used = _spec_slugs()
    assert declared, "no v3 specs declared any esci_items"
    assert used, "no v3 trigger carried an esci tag"
    assert used <= set(declared), f"trigger tags with no declaration: {used - set(declared)}"
    unmapped = set(declared) - set(esci.SLUG_TO_ITEM)
    assert not unmapped, (
        "scenarios/v3 uses ESCI slugs server/esci.py cannot map: "
        f"{sorted(unmapped)} — extend SLUG_TO_ITEM and say so in its comment"
    )
    assert set(esci.SLUG_TO_ITEM) - set(declared) == set(), (
        "SLUG_TO_ITEM carries slugs no spec declares: "
        f"{sorted(set(esci.SLUG_TO_ITEM) - set(declared))}"
    )


def test_each_slug_maps_to_the_item_with_that_exact_text():
    # The specs' declared text is the CSV text verbatim; reverse items add
    # " (R)". This is what makes the crosswalk checkable rather than asserted.
    declared, _ = _spec_slugs()
    for slug, text in declared.items():
        it = esci.item_for_slug(slug)
        assert it is not None, slug
        expected = it["text"] + (" (R)" if it["reverse"] else "")
        assert text == expected, f"{slug}: spec says {text!r}, bank says {expected!r}"


def test_slug_r_suffix_agrees_with_the_reverse_flag():
    for slug, iid in esci.SLUG_TO_ITEM.items():
        assert slug.endswith("_r") == esci.item(iid)["reverse"], slug


def test_slug_for_and_item_for_slug_round_trip():
    for slug, iid in esci.SLUG_TO_ITEM.items():
        assert esci.slug_for(iid) == slug
        assert esci.slug_for(esci.item(iid)["number"]) == slug
        assert esci.item_for_slug(slug)["id"] == iid
    assert esci.slug_for("ESCI-99") is None
    assert esci.item_for_slug("no_such_slug") is None
    assert esci.item_for_slug(None) is None
    assert esci.item_for_slug(" fester_r ") is not None


def test_items_for_slugs_resolves_a_trigger_tag_list():
    got = esci.items_for_slugs(["de_escalate", "talk_openly"])
    assert [i["id"] for i in got] == ["ESCI-14", "ESCI-26"]


def test_items_for_slugs_drops_unknowns_and_dedupes():
    # A recorded encounter must stay readable when the specs have moved on.
    got = esci.items_for_slugs(["de_escalate", "gone_away", "de_escalate"])
    assert [i["id"] for i in got] == ["ESCI-14"]
    assert esci.items_for_slugs([]) == []
    assert esci.items_for_slugs(None) == []


def test_construct_of_a_slug_matches_the_spec_that_declares_it():
    for path in sorted(glob.glob(str(ROOT / "scenarios" / "v3" / "*.yaml"))):
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
        construct = spec["construct"]
        for slug in (spec.get("esci_items") or {}):
            it = esci.item_for_slug(slug)
            assert it["construct"] == construct, (
                f"{Path(path).name} declares {slug} but it belongs to "
                f"{it['construct']}, not {construct}"
            )


# --- Against the fixture wave ------------------------------------------------

# The wave arrives through tests/conftest.py's `wave_sessions` fixture, which
# skips with instructions when there is not one. The module-level skipif this
# replaces asked whether `<DATA_DIR>/sessions` *existed* — but server.storage
# creates that directory the moment it is imported, so pointing DATA_DIR at a
# fresh temp directory (which is what a first local run does) got past the
# guard with zero records and then died on `assert seen` below: a missing
# fixture reported as a defect. conftest calls a directory a wave only when it
# actually holds a record, so reaching the assertion now means the wave is
# present and the assertion is about the wave's contents, which is what it is
# for.


def test_every_esci_tag_in_a_recorded_wave_resolves(wave_sessions):
    """The crosswalk against real recorded encounters, not just the specs.

    Each entry in a record's `steering_log` carries the fired trigger's `esci`
    slugs. If any of them fail to resolve, a join from encounters to rating items
    silently loses beats, which is the exact failure the crosswalk exists to
    prevent — and it would only show up as a suspiciously thin evidence trail.
    """
    import json
    seen, unresolved = set(), set()
    per_construct = {}
    for rec in sorted(wave_sessions.glob("*/record.json")):
        doc = json.loads(rec.read_text(encoding="utf-8"))
        for entry in doc.get("steering_log", []):
            for slug in (entry.get("esci") or []):
                seen.add(slug)
                found = esci.item_for_slug(slug)
                if found is None:
                    unresolved.add(slug)
                else:
                    per_construct.setdefault(found["construct"], set()).add(found["id"])
    assert seen, "no esci tags found in the wave"
    assert not unresolved, f"unresolvable slugs in recorded data: {sorted(unresolved)}"
    # A wave that covers all four constructs should exercise all four blocks of
    # the instrument; anything less means an encounter type never got rated.
    assert set(per_construct) == set(esci.CONSTRUCTS), sorted(per_construct)


# --- CLI ---------------------------------------------------------------------

def test_cli_prints_the_notice_and_the_bank():
    out = subprocess.run([sys.executable, "-m", "server.esci"],
                         cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert "proprietary" in out.stdout.lower()
    assert "ESCI-15" in out.stdout
    assert "items with no scenario slug: none" in out.stdout


def test_cli_json_is_the_item_bank():
    import json
    out = subprocess.run([sys.executable, "-m", "server.esci", "--json"],
                         cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    bank = json.loads(out.stdout)
    assert bank["notice"] == esci.NOTICE
    assert len(bank["items"]) == 22


# --- Loader failure modes ----------------------------------------------------
#
# The loader raises at import rather than degrading, because a half-loaded bank
# would let `validate` accept a short submission that looks like every other
# submission in the dataset. Exercised here against temp copies of the CSV.

def _write(tmp_path, body):
    p = tmp_path / "items.csv"
    p.write_text(body, encoding="utf-8", newline="")
    return p


HEADER = "item_no,competency,item_text,reverse_scored\r\n"


def test_loader_accepts_a_well_formed_file(tmp_path):
    p = _write(tmp_path, HEADER + "8,conflict_management,Tries,FALSE\r\n\r\n")
    items = esci._load(p)
    assert items == [{"id": "ESCI-08", "number": 8, "text": "Tries",
                      "construct": "conflict_management", "reverse": False}]


def test_loader_rejects_a_missing_file(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        esci._load(tmp_path / "nope.csv")


def test_loader_rejects_missing_columns(tmp_path):
    p = _write(tmp_path, "item_no,competency\r\n8,teamwork\r\n")
    with pytest.raises(RuntimeError, match="missing column"):
        esci._load(p)


def test_loader_rejects_a_duplicate_item_number(tmp_path):
    p = _write(tmp_path, HEADER + "8,teamwork,A,FALSE\r\n8,teamwork,B,FALSE\r\n")
    with pytest.raises(RuntimeError, match="already defined"):
        esci._load(p)


def test_loader_rejects_an_unknown_construct(tmp_path):
    # Adding the Construct 3 block means extending CONSTRUCTS deliberately.
    p = _write(tmp_path, HEADER + "9,empathy,Reads the room,FALSE\r\n")
    with pytest.raises(RuntimeError, match="not.*one of"):
        esci._load(p)


def test_loader_rejects_an_unparseable_reverse_flag(tmp_path):
    p = _write(tmp_path, HEADER + "8,teamwork,A,maybe\r\n")
    with pytest.raises(RuntimeError, match="not a boolean"):
        esci._load(p)


def test_loader_rejects_empty_item_text(tmp_path):
    p = _write(tmp_path, HEADER + "8,teamwork,,FALSE\r\n")
    with pytest.raises(RuntimeError, match="item_text is empty"):
        esci._load(p)


def test_loader_rejects_a_non_numeric_item_no(tmp_path):
    p = _write(tmp_path, HEADER + "eight,teamwork,A,FALSE\r\n")
    with pytest.raises(RuntimeError, match="not a number"):
        esci._load(p)


def test_loader_rejects_an_empty_bank(tmp_path):
    with pytest.raises(RuntimeError, match="no items"):
        esci._load(_write(tmp_path, HEADER))


def test_loader_reads_the_reverse_flag_in_the_spellings_a_researcher_may_use(tmp_path):
    p = _write(tmp_path, HEADER +
               "8,teamwork,A,TRUE\r\n9,teamwork,B,true\r\n10,teamwork,C,yes\r\n"
               "11,teamwork,D,1\r\n12,teamwork,E,\r\n")
    assert [i["reverse"] for i in esci._load(p)] == [True, True, True, True, False]


def test_loader_tolerates_a_utf8_bom(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_bytes(b"\xef\xbb\xbf" + (HEADER + "8,teamwork,A,FALSE\r\n").encode())
    assert esci._load(p)[0]["id"] == "ESCI-08"
