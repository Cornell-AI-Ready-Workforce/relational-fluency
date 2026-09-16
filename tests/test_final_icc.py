"""The largest-complete-block search behind every ICC in the report.

ICC(2,k) is the reliability figure this study is built to report, and it is
computed on a restriction of the wave: the largest block of encounters that one
fixed set of raters all scored. Finding that block is the whole difficulty,
because a wave allocated by ``raters.assign`` is not fully crossed -- with a
pool of five scoring three at a time, no encounter is scored by all five, and no
encounter is scored by any four either. Every subset above a certain size is
complete on nothing, so the answer is only reachable by a search that can walk
across a plateau of zeroes rather than one that takes improving steps.

These tests pin three things:

  * the block found is the optimum, checked against brute force over every
    rater subset, at every pool size the study could plausibly run;
  * it is the same block whatever order the data arrives in;
  * when there really is no block, the report says so with a reason that is a
    true statement about the data in front of it. That second half matters as
    much as the first: a researcher who reads "fewer than two raters with any
    data" over a wave with five raters and 78 ratings goes looking for lost
    ratings, and the bug is not in their data.

Run from the repo root:

    python -m pytest tests/test_final_icc.py
"""

from __future__ import annotations

import csv
import itertools
import random
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    # pytest puts tests/ on sys.path, not the repo root.
    sys.path.insert(0, str(REPO_ROOT))

from server import reliability as rel  # noqa: E402

ITEMS_CSV = REPO_ROOT / "studies" / "study1" / "qualtrics" / "esci_construct4_items.csv"


# --------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def items():
    out = []
    with ITEMS_CSV.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            n = int(row["item_no"])
            out.append({
                "id": f"ESCI-{n:02d}",
                "number": n,
                "text": row["item_text"],
                "construct": row["competency"],
                "reverse": row["reverse_scored"].strip().upper() == "TRUE",
            })
    return out


def _rating(session_id, rater_id, scores):
    return {
        "session_id": session_id,
        "rater_id": rater_id,
        "scores": scores,
        "open_ended": {"better": "", "notable": ""},
        "seconds": 420,
    }


def _rotating_wave(n_encounters, pool, per_encounter):
    """(encounter_id, raters) pairs in the shape raters.assign produces.

    A rotation rather than a shuffle, for the same reason ``raters.assign``
    refuses to shuffle: consecutive encounters share raters, so the overlap
    graph is connected and there is agreement to measure. It is also the
    structure that produces the plateau -- every subset larger than
    ``per_encounter`` is complete on zero encounters.
    """
    return [(f"s_{n}", sorted(pool[(n + k) % len(pool)] for k in range(per_encounter)))
            for n in range(n_encounters)]


def _cells(n_encounters, pool, per_encounter, seed=7, na_rate=0.0):
    """One construct-shaped ``{encounter: {rater: score}}`` map."""
    rng = random.Random(seed)
    out = {}
    for sid, panel in _rotating_wave(n_encounters, pool, per_encounter):
        truth = rng.randint(1, 5)
        cell = {}
        for r in panel:
            if rng.random() < na_rate:
                cell[r] = None
            else:
                cell[r] = min(5, max(1, truth + rng.choice([-1, 0, 0, 0, 1])))
        out[sid] = cell
    return out


def _brute_force_best(rows_by_subject, pool):
    """Every rater subset, exhaustively: the biggest block ICC could use.

    Deliberately the stupidest possible implementation. It is the ground truth
    the search is checked against, so it must be obviously right rather than
    fast.
    """
    best = (0, None, None)
    for size in range(2, len(pool) + 1):
        for subset in itertools.combinations(sorted(pool), size):
            subs = [s for s in sorted(rows_by_subject)
                    if all(rows_by_subject[s].get(r) is not None for r in subset)]
            if len(subs) >= 2 and size * len(subs) > best[0]:
                best = (size * len(subs), list(subset), subs)
    return best


def _sharing_pairs(rows_by_subject, pool):
    """How many encounters each rater pair both scored."""
    return {
        (a, b): sum(1 for s in rows_by_subject
                    if rows_by_subject[s].get(a) is not None
                    and rows_by_subject[s].get(b) is not None)
        for a, b in itertools.combinations(sorted(pool), 2)
    }


# --------------------------------------------------------------------------
# the search finds the block that is there
# --------------------------------------------------------------------------

class TestBlockSearchFindsWhatExists:

    def test_the_studys_own_design_produces_a_computable_icc(self, items):
        """26 encounters, a pool of 5, 3 raters each -- the design as written.

        This is the case the report exists for, and the one that returned null
        for every construct and all 22 items: no encounter is scored by all
        five raters and none by any four, so the block search started and
        finished on zero.
        """
        pool = [f"rt_{c}" for c in "abcde"]
        rng = random.Random(7)
        rows = []
        for sid, panel in _rotating_wave(26, pool, 3):
            truth = {i["id"]: rng.randint(1, 5) for i in items}
            for r in panel:
                scores = {}
                for i in items:
                    if rng.random() < 0.045:
                        scores[i["id"]] = None
                    else:
                        scores[i["id"]] = min(5, max(
                            1, truth[i["id"]] + rng.choice([-1, 0, 0, 0, 1])))
                rows.append(_rating(sid, r, scores))

        rep = rel.report(None, ratings=rows, items=items)
        assert rep["n_raters"] == 5 and rep["n_encounters"] == 26
        assert len(rows) == 78
        assert rep["design"]["fully_crossed"] is False

        for name, block in rep["constructs"].items():
            ic = block["icc"]
            assert ic["computable"] is True, (name, ic["reason"])
            assert ic["icc_2_1"] is not None and ic["icc_2_k"] is not None
            # A block, and it names itself: at least two raters over at least
            # two of the 26 encounters, and the restriction is stated.
            assert len(ic["raters_used"]) >= 2
            assert ic["subjects_used"] >= 2
            assert ic["subjects_available"] == 26
            assert set(ic["raters_used"]) <= set(pool)
        # And the same for the items, which are what the panel actually scored.
        computable = [b for b in rep["items"].values() if b["icc"]["computable"]]
        assert len(computable) == 22

    @pytest.mark.parametrize("n_pool", [2, 3, 4, 5, 6, 7, 8])
    def test_every_pool_size_reaches_the_brute_force_optimum(self, n_pool):
        """Checked against exhaustive enumeration, not against itself."""
        pool = [f"rt_{i}" for i in range(n_pool)]
        per = min(3, n_pool)
        rows = _cells(26, pool, per, na_rate=0.045)

        raters_used, subjects_used, exhaustive = rel._largest_complete_block(
            rows, sorted(pool))
        best_area, best_raters, best_subjects = _brute_force_best(rows, pool)

        assert exhaustive is True
        assert best_area > 0, "the fixture itself has no computable block"
        assert len(raters_used) * len(subjects_used) == best_area
        assert len(subjects_used) == len(best_subjects)
        # The block is complete: every named rater scored every named encounter.
        for s in subjects_used:
            for r in raters_used:
                assert rows[s].get(r) is not None
        assert rel.icc(rel._matrix(rows, subjects_used, raters_used))["computable"]

    def test_the_zero_plateau_is_crossed(self):
        """The minimal shape of the bug, with no randomness in it.

        Five raters, three per encounter. Every 5-subset and every 4-subset is
        complete on zero encounters, so a search that stops at the first
        non-improving drop never reaches the 2 x 10 block sitting underneath.
        """
        pool = [f"rt_{i}" for i in range(5)]
        rows = _cells(25, pool, 3)

        for size in (5, 4):
            for subset in itertools.combinations(pool, size):
                assert not [s for s in rows
                            if all(rows[s].get(r) is not None for r in subset)], (
                    f"fixture broken: a {size}-rater subset is complete")

        raters_used, subjects_used, _ = rel._largest_complete_block(rows, pool)
        assert len(raters_used) == 2
        assert len(subjects_used) == 10
        assert rel.icc(rel._matrix(rows, subjects_used, raters_used))["computable"]

    def test_a_wider_block_never_wins_by_being_uncomputable(self):
        """Area alone is the wrong objective, and this is where it shows.

        All four raters scored encounter one, so the 4 x 1 block has an area of
        four and the 2 x 2 block underneath it has an area of four as well --
        and ICC needs two encounters, so only one of them is worth anything. A
        search maximising cells with no floor under it can report the wider one
        and then compute nothing.
        """
        rows = {
            "s_0": {"rt_a": 4, "rt_b": 2, "rt_c": 5, "rt_d": 1},
            "s_1": {"rt_a": 3, "rt_b": 5},
        }
        raters_used, subjects_used, _ = rel._largest_complete_block(
            rows, ["rt_a", "rt_b", "rt_c", "rt_d"])
        assert raters_used == ["rt_a", "rt_b"]
        assert subjects_used == ["s_0", "s_1"]
        assert rel.icc(rel._matrix(rows, subjects_used, raters_used))["computable"]

    def test_the_block_does_not_depend_on_the_order_the_wave_arrived_in(self):
        """Two runs of the same wave have to be citable as the same analysis."""
        pool = [f"rt_{i}" for i in range(6)]
        rows = _cells(30, pool, 3, seed=19, na_rate=0.05)
        shuffled_rows = dict(sorted(rows.items(), key=lambda kv: kv[0][::-1]))
        shuffled_pool = list(reversed(pool))

        first = rel._largest_complete_block(rows, pool)
        second = rel._largest_complete_block(shuffled_rows, shuffled_pool)
        assert first == second
        area, _, _ = _brute_force_best(rows, pool)
        assert len(first[0]) * len(first[1]) == area


# --------------------------------------------------------------------------
# when there is no block, the refusal has to be true
# --------------------------------------------------------------------------

class TestRefusalsSaySomethingTrue:

    def _no_overlap_wave(self, items):
        """Six raters, two per encounter, every pair used exactly once.

        No pair shares two encounters, so no complete block of two encounters
        exists for any subset and ICC genuinely cannot be computed. Krippendorff
        still can be, which is the point of computing it alongside.
        """
        pool = [f"rt_{i}" for i in range(6)]
        rng = random.Random(3)
        rows = []
        for n, (a, b) in enumerate(itertools.combinations(pool, 2)):
            truth = {i["id"]: rng.randint(1, 5) for i in items}
            for r in (a, b):
                rows.append(_rating(f"s_{n}", r, {
                    i["id"]: min(5, max(1, truth[i["id"]] + rng.choice([-1, 0, 1])))
                    for i in items}))
        return pool, rows

    def test_the_reason_does_not_claim_the_raters_are_missing(self, items):
        pool, rows = self._no_overlap_wave(items)
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["n_raters"] == 6 and rep["n_encounters"] == 15

        for name, block in rep["constructs"].items():
            ic = block["icc"]
            assert ic["computable"] is False
            reason = ic["reason"]
            # The false statement this used to make. Six raters submitted.
            assert "fewer than two raters with any data" not in reason, reason
            # What it says instead is checkable against the wave above.
            assert "all 6 raters" in reason, reason
            assert "0 of 15" in reason, reason
            assert "no complete rater-by-encounter block" in reason, reason
            # Alpha is computable on the same data and the reason points at it.
            assert block["krippendorff_alpha"] is not None
            assert "Krippendorff" in reason

    def test_that_refusal_is_a_true_statement_about_the_data(self, items):
        """Verify the claim independently rather than trusting the search."""
        pool, rows = self._no_overlap_wave(items)
        rep = rel.report(None, ratings=rows, items=items)
        cells = {}
        for row in rows:
            cells.setdefault(row["session_id"], {})[row["rater_id"]] = 3.0
        shared = _sharing_pairs(cells, pool)
        assert max(shared.values()) == 1, shared
        assert _brute_force_best(cells, pool)[0] == 0
        assert not rep["constructs"]["influence"]["icc"]["computable"]

    def test_subjects_used_counts_encounters_the_named_panel_all_scored(self,
                                                                        items):
        """The number next to ``raters_used`` has to mean one thing.

        Where a block is found it is the block's height. Where none is, the
        report still has to name a panel, and the honest count beside it is how
        many encounters that panel all scored -- zero -- not the size of the
        table it was handed. "All 6 raters, 15 of 15 encounters, not
        computable" is the same species of false statement as the reason string
        this whole fix is about: it describes a wave that had every rating and
        failed anyway, and nothing in the data says that.
        """
        pool, rows = self._no_overlap_wave(items)
        rep = rel.report(None, ratings=rows, items=items)
        ic = rep["constructs"]["influence"]["icc"]
        assert ic["computable"] is False
        assert ic["raters_used"] == sorted(pool)
        assert ic["subjects_used"] == 0
        assert ic["subjects_available"] == 15

        # Independently: no encounter was scored by all six of the named pool.
        cells = {}
        for row in rows:
            cells.setdefault(row["session_id"], {})[row["rater_id"]] = 3.0
        assert not [s for s in cells
                    if all(cells[s].get(r) is not None for r in pool)]

        # And the same field means the same thing where a block IS found.
        for name, block in rel.report(
                None,
                ratings=[_rating(f"s_{n}", r, {i["id"]: 1 + (n % 5) for i in items})
                         for n in range(6) for r in ("rt_a", "rt_b")],
                items=items)["constructs"].items():
            bic = block["icc"]
            assert bic["subjects_used"] == 6, name
            assert bic["raters_used"] == ["rt_a", "rt_b"]

        # The case the wave above cannot reach, because every rater in it
        # scored something: a NAMED rater who contributed nothing to THIS row.
        # That is the systematically-N/A item the module docstring predicts for
        # ESCI 3 and 49 in S2's dyadic setting, and it is the shape where a
        # count taken off the ICC's own listwise total goes wrong -- icc()
        # drops rt_b's empty column, so its "n" is the number of encounters
        # rt_a alone scored, and reporting that next to a two-rater panel says
        # "both raters, 5 of 5 encounters, not computable" over a row the two
        # of them never once scored together.
        na_rows = []
        for n in range(5):
            full = {i["id"]: 4 for i in items}
            partial = dict(full, **{"ESCI-03": None})
            na_rows.append(_rating(f"s_{n}", "rt_a", full))
            na_rows.append(_rating(f"s_{n}", "rt_b", partial))
        na_ic = rel.report(None, ratings=na_rows, items=items)["items"]["ESCI-03"]["icc"]
        assert na_ic["computable"] is False
        assert na_ic["raters_used"] == ["rt_a", "rt_b"]
        assert na_ic["subjects_available"] == 5
        assert na_ic["subjects_used"] == 0, na_ic["subjects_used"]

        # Independently: rt_b judged item 3 on nothing, so the named panel
        # shares no encounter on this row at all.
        item3 = {f"s_{n}": {"rt_a": 4, "rt_b": None} for n in range(5)}
        assert not [s for s in item3
                    if all(item3[s].get(r) is not None for r in ("rt_a", "rt_b"))]

    def test_the_crossing_warning_does_not_promise_a_block_that_is_not_there(
            self, items):
        """report() warns about the design; it must not overpromise the remedy.

        The warning tells a researcher what was done about a pool that is not
        fully crossed. Stated flatly -- "each ICC below was computed on the
        largest complete block" -- it is false for this wave, where no ICC was
        computed at all, and it sends the reader looking through the tables for
        a block that does not exist.
        """
        pool, rows = self._no_overlap_wave(items)
        rep = rel.report(None, ratings=rows, items=items)
        crossing = [w for w in rep["warnings"] if "not fully crossed" in w]
        assert len(crossing) == 1, rep["warnings"]
        w = crossing[0]
        assert "where no such block exists" in w, w
        assert "the ICC is not computable" in w, w
        # The unconditional promise, which no construct in this wave kept.
        assert "block and reports which raters" not in w, w
        assert not any(b["icc"]["computable"] for b in rep["constructs"].values())

    def test_the_warning_a_researcher_reads_carries_the_true_reason(self, items):
        pool, rows = self._no_overlap_wave(items)
        rep = rel.report(None, ratings=rows, items=items)
        stalled = [w for w in rep["warnings"] if "ICC not computable" in w]
        assert stalled, rep["warnings"]
        for w in stalled:
            assert "fewer than two raters with any data" not in w, w

    def test_a_row_nobody_rated_says_that_rather_than_counting_raters(self):
        """An empty table has no raters in it; that is not a fact about a wave.

        report() hands this shape to icc() for an item no submitted rating
        carried at all.
        """
        r = rel.icc([])
        assert r["computable"] is False
        assert r["n"] == 0 and r["k"] == 0
        assert "no rows" in r["reason"]
        assert "fewer than two raters" not in r["reason"]

    def test_an_item_every_rater_marked_na_is_described_by_its_own_row(self, items):
        """The reason has to be true of the item, not of the wave around it."""
        def scores_for(_sid):
            out = {i["id"]: 4 for i in items}
            out["ESCI-03"] = None
            return out

        rows = [_rating(f"s_{n}", r, scores_for(f"s_{n}"))
                for n in range(6) for r in ("rt_a", "rt_b")]
        rep = rel.report(None, ratings=rows, items=items)
        ic = rep["items"]["ESCI-03"]["icc"]
        assert ic["computable"] is False
        assert rep["items"]["ESCI-03"]["na_rate"] == pytest.approx(1.0)
        # Zero raters produced a score on THIS item, which is what it says, and
        # the wave's own two raters are still reported next to it.
        assert "fewer than two raters with any data (0)" in ic["reason"]
        assert ic["raters_used"] == ["rt_a", "rt_b"]
        # ...and the count beside them is the encounters those two both scored
        # ON THIS ITEM, which is none. A table with no ratings left in it is
        # still six rows tall, and printing that height here would read as
        # "both raters, 6 of 6 encounters" over an item nobody judged.
        assert ic["subjects_used"] == 0, ic["subjects_used"]
        assert ic["subjects_available"] == 6

    def test_a_single_rater_wave_still_blames_the_rater_count(self, items):
        rows = [_rating(f"s_{n}", "rt_a", {i["id"]: 3 for i in items})
                for n in range(5)]
        rep = rel.report(None, ratings=rows, items=items)
        ic = rep["constructs"]["teamwork"]["icc"]
        assert ic["computable"] is False
        assert "fewer than two raters with any data (1)" in ic["reason"]
        # One rater cannot be rescued by a sub-panel and is not told they might.
        assert "No sub-panel" not in ic["reason"]


# --------------------------------------------------------------------------
# the search terminates on any input
# --------------------------------------------------------------------------

class TestSearchTermination:

    def test_a_capped_search_still_returns_a_usable_block(self, monkeypatch):
        """The cap bounds the work; it must not cost the answer.

        Pairwise overlap is what a not-fully-crossed wave gets its ICC from and
        the first round of the enumeration produces all of it, so a cap that
        trips immediately afterwards still lands on a real block.
        """
        monkeypatch.setattr(rel, "_BLOCK_SEARCH_MAX_CANDIDATES", 1)
        pool = [f"rt_{i}" for i in range(5)]
        rows = _cells(26, pool, 3, na_rate=0.045)

        raters_used, subjects_used, exhaustive = rel._largest_complete_block(
            rows, pool)
        assert exhaustive is False
        assert len(raters_used) >= 2 and len(subjects_used) >= 2
        assert rel.icc(rel._matrix(rows, subjects_used, raters_used))["computable"]

    def test_a_capped_search_does_not_claim_no_block_exists(self, items,
                                                            monkeypatch):
        """A search that stopped early may not report an absence as a fact."""
        monkeypatch.setattr(rel, "_BLOCK_SEARCH_MAX_CANDIDATES", 1)
        pool = [f"rt_{i}" for i in range(6)]
        rng = random.Random(3)
        rows = []
        for n, (a, b) in enumerate(itertools.combinations(pool, 2)):
            for r in (a, b):
                rows.append(_rating(f"s_{n}", r,
                                    {i["id"]: rng.randint(1, 5) for i in items}))
        rep = rel.report(None, ratings=rows, items=items)
        reason = rep["constructs"]["influence"]["icc"]["reason"]
        assert "No sub-panel rescues it" not in reason, reason
        assert "0 of 15" in reason


# --------------------------------------------------------------------------
# subjects_used is a count of the data, never of the table it was handed
# --------------------------------------------------------------------------

class TestSubjectsUsedNeverOverstatesTheOverlap:
    """One meaning for ``subjects_used``, on every row of every wave shape.

    ``raters_used`` and ``subjects_used`` are printed together and read
    together: "these raters, this many encounters". The only reading that
    makes the pair true is "encounters that ALL of those raters scored". Any
    other number turns the reliability report into the thing this module
    exists to prevent -- a confident false statement about the data -- and the
    dangerous direction is upward, because a reader who is shown "5 of 5"
    stops looking for the missingness that is actually there.
    """

    def _wave(self, items, seed, n_encounters, pool, per_encounter, na_rate):
        """Ratings plus the per-item cell map they should produce.

        The map is rebuilt here from the same randomness rather than read back
        out of ``report``, so the assertion checks the report against the wave
        and not against itself.
        """
        rng = random.Random(seed)
        rows = []
        cells = {i["id"]: {} for i in items}
        for n in range(n_encounters):
            sid = f"s_{n}"
            for r in sorted(rng.sample(pool, per_encounter)):
                scores = {}
                for i in items:
                    v = None if rng.random() < na_rate else rng.randint(1, 5)
                    scores[i["id"]] = v
                    cells[i["id"]].setdefault(sid, {})[r] = v
                rows.append(_rating(sid, r, scores))
        return rows, cells

    @pytest.mark.parametrize("seed,n_encounters,n_pool,per_encounter,na_rate", [
        (11, 5, 2, 2, 0.5),     # two raters, half the cells N/A
        (12, 9, 3, 2, 0.25),    # the rotation that leaves no full panel
        (13, 14, 5, 3, 0.35),   # the study's own allocation shape, with holes
        (14, 7, 4, 4, 0.6),     # fully crossed on paper, mostly N/A in fact
        (15, 6, 3, 1, 0.1),     # one rater per encounter: nothing is shared
    ])
    def test_it_equals_the_encounters_the_named_raters_all_scored(
            self, items, seed, n_encounters, n_pool, per_encounter, na_rate):
        pool = [f"rt_{i}" for i in range(n_pool)]
        rows, cells = self._wave(
            items, seed, n_encounters, pool, per_encounter, na_rate)
        rep = rel.report(None, ratings=rows, items=items)

        for iid, block in rep["items"].items():
            ic = block["icc"]
            cell = cells[iid]
            shared = [s for s in cell
                      if all(cell[s].get(r) is not None
                             for r in ic["raters_used"])]
            assert ic["subjects_used"] == len(shared), (
                iid, ic["raters_used"], ic["subjects_used"], len(shared))
            assert ic["subjects_used"] <= ic["subjects_available"]

    def test_a_rater_who_scored_nothing_does_not_inflate_the_count(self, items):
        """The verifier's own reproduction, kept as a named regression.

        Two raters, five encounters, rt_b marks item 3 "not enough information
        to judge" every single time -- exactly what the instrument predicts for
        that item in a dyadic setting. ICC drops rt_b's empty column and
        reports n = 5 for the one column left standing; published beside a
        two-name panel that reads "5 of 5", and the truth is 0.
        """
        rows = []
        for n in range(5):
            full = {i["id"]: 3 for i in items}
            rows.append(_rating(f"s_{n}", "rt_a", full))
            rows.append(_rating(f"s_{n}", "rt_b", dict(full, **{"ESCI-03": None})))
        rep = rel.report(None, ratings=rows, items=items)
        ic = rep["items"]["ESCI-03"]["icc"]
        assert ic["raters_used"] == ["rt_a", "rt_b"]
        assert ic["subjects_used"] == 0
        assert ic["subjects_available"] == 5
        assert ic["computable"] is False
        # The rest of the wave is untouched: rt_b answered every other item, so
        # a row that really does have a complete block still reports one.
        other = [b["icc"] for iid, b in rep["items"].items() if iid != "ESCI-03"]
        assert other and all(o["subjects_used"] == 5 for o in other)


# --------------------------------------------------------------------------
# the report may not promise more than the search delivered
# --------------------------------------------------------------------------

class TestTheCrossingWarningMatchesTheSearchThatRan:
    """``report`` tells the reader what was done about a pool that is not
    fully crossed. The enumeration is exact until it hits its candidate cap
    and near-optimal afterwards -- measured 192 against an optimum of 196 on a
    14-rater leave-one-out design. "The largest complete block that EXISTS in
    its row" is a guarantee, and a statistics module that overstates its own
    guarantee has made the same species of false statement as a row reporting
    encounters nobody scored.
    """

    def _rotating_rows(self, items, n_encounters=26, n_pool=5, per_encounter=3):
        rng = random.Random(5)
        rows = []
        for sid, panel in _rotating_wave(n_encounters,
                                         [f"rt_{i}" for i in range(n_pool)],
                                         per_encounter):
            truth = {i["id"]: rng.randint(1, 5) for i in items}
            for r in panel:
                rows.append(_rating(sid, r, {
                    i["id"]: min(5, max(1, truth[i["id"]] + rng.choice([-1, 0, 1])))
                    for i in items}))
        return rows

    def _crossing_warning(self, rep):
        found = [w for w in rep["warnings"] if "not fully crossed" in w]
        assert len(found) == 1, rep["warnings"]
        return found[0]

    def test_the_guarantee_is_stated_when_the_search_ran_to_completion(self, items):
        rep = rel.report(None, ratings=self._rotating_rows(items), items=items)
        w = self._crossing_warning(rep)
        assert "that exists in its row" in w, w
        # Not hedged when there is nothing to hedge: a reader told the answer
        # might be beatable goes hunting for a better block by hand.
        assert "the search found" not in w, w
        assert any(b["icc"]["computable"] for b in rep["constructs"].values())

    def test_the_guarantee_is_withdrawn_when_the_search_was_capped(
            self, items, monkeypatch):
        monkeypatch.setattr(rel, "_BLOCK_SEARCH_MAX_CANDIDATES", 1)
        rep = rel.report(None, ratings=self._rotating_rows(items), items=items)
        w = self._crossing_warning(rep)
        assert "that exists in its row" not in w, w
        assert "the search found" in w, w
        assert "not provably the largest" in w, w
        # The hedge may not size the gap smaller than the search can
        # guarantee. "a slightly larger one may be there" shipped here once,
        # and the measured shortfall is not slight: a pool of 15 with 80
        # encounters returns 9 raters x 23 encounters against a provably
        # exhaustive 6 x 44, which nearly doubles the usable n. The existing
        # assertions above are substring matches and all three still passed
        # with "slightly" in place, so the magnitude is asserted directly.
        for minimiser in ("slightly", "marginally", "a little", "a bit",
                          "barely", "somewhat", "negligibl", "trivially"):
            assert minimiser not in w.lower(), (minimiser, w)
        assert "20% of" in w, w
        # The remedy is still described; only the promise about it is softened.
        assert "raters_used/subjects_used" in w, w
        assert "Krippendorff" in w, w

    def test_the_capped_flag_reaches_every_row_that_was_searched(
            self, items, monkeypatch):
        """report() cannot word that warning without the flag on the rows."""
        monkeypatch.setattr(rel, "_BLOCK_SEARCH_MAX_CANDIDATES", 1)
        rep = rel.report(None, ratings=self._rotating_rows(items), items=items)
        blocks = list(rep["constructs"].values()) + list(rep["items"].values())
        assert blocks
        assert all(b["icc"]["block_search_exhaustive"] is False for b in blocks)

        monkeypatch.undo()
        clean = rel.report(None, ratings=self._rotating_rows(items), items=items)
        clean_blocks = (list(clean["constructs"].values())
                        + list(clean["items"].values()))
        assert all(b["icc"]["block_search_exhaustive"] is True
                   for b in clean_blocks)

    def test_a_design_that_caps_the_search_at_the_shipped_limit(self, items):
        """The overstated guarantee is reachable without touching the cap.

        Fourteen raters, 56 encounters, every encounter scored by all but one
        of them -- a leave-one-out rotation, which is a design a real lab could
        run. Every subset of the pool is the rater set of some intersection, so
        the closure walks toward 2**14 panels and stops on the cap. The block
        it returns is 8 raters x 24 encounters = 192; the optimum is
        7 x 28 = 196. The number is fine. Calling it "the largest that exists"
        is not.

        The assertion is on the AGREEMENT between the flag and the wording
        rather than on which branch is taken, so raising the cap (which would
        make the search exhaustive here, and the strong wording true again)
        keeps the test honest instead of breaking it.
        """
        pool = [f"rt_{i:02d}" for i in range(14)]
        rows = []
        for n in range(56):
            left_out = pool[n % 14]
            for r in pool:
                if r == left_out:
                    continue
                rows.append(_rating(f"s_{n:03d}", r,
                                    {i["id"]: 1 + ((n + int(r[3:])) % 5)
                                     for i in items}))
        rep = rel.report(None, ratings=rows, items=items)
        w = self._crossing_warning(rep)
        blocks = list(rep["constructs"].values()) + list(rep["items"].values())
        exhaustive = all(b["icc"]["block_search_exhaustive"] for b in blocks)

        assert ("that exists in its row" in w) is exhaustive, (exhaustive, w)
        assert ("the search found" in w) is not exhaustive, (exhaustive, w)

        # And, at the cap this module ships with, that design really does stop
        # the search early -- so the wording above is the hedged one.
        if rel._BLOCK_SEARCH_MAX_CANDIDATES == 5000:
            assert exhaustive is False
            ic = rep["constructs"]["teamwork"]["icc"]
            assert len(ic["raters_used"]) * ic["subjects_used"] == 192
            # The optimum the capped search did not reach, by construction: a
            # panel of k raters is complete on the 56 - 4k encounters that left
            # none of them out, so the area is k * (56 - 4k), maximal at k = 7.
            assert max(k * (56 - 4 * k) for k in range(2, 14)) == 196


# --------------------------------------------------------------------------
# the search has to finish while a researcher is still looking at the page
# --------------------------------------------------------------------------

class TestTheBlockSearchStaysWithinBudget:

    def test_a_randomly_allocated_pool_of_20_does_not_own_the_endpoint(self):
        """GET /api/reliability is uncached and this search runs once per row.

        A rotation keeps the number of distinct rater-sets down to the pool
        size, but an allocator that picks panels freely gives almost every
        encounter its own set, and the enumeration then produces tens of
        thousands of candidate panels. Scoring each of those by walking every
        distinct rater-set in the wave is a product of two large numbers --
        measured 9.5 million mask tests for one row of a 20-rater,
        600-encounter wave, and ~250 million for a whole report, which is 20s
        of wall clock on an endpoint nothing caches.

        The budget is expressed in multiples of a busy-loop probe rather than
        in seconds, so a slow CI box moves both sides of the comparison. On the
        development machine one probe is ~0.19s; this search cost ~9 probes
        before the fix and ~1.3 after.
        """
        rng = random.Random(19)
        pool = [f"rt_{i}" for i in range(20)]
        rows = {f"s_{n}": {r: 3 for r in rng.sample(pool, 8)}
                for n in range(1000)}

        # Both sides are timed three times, interleaved, and the MINIMUM of each
        # is compared. Timing them once each reddened the full suite on about
        # one run in three: the true cost is ~1.3 probes against a budget of 4,
        # and it still failed at 4.1, because the two intervals were measured at
        # different moments and a scheduler hiccup landing in the search but not
        # the probe inflates the ratio threefold. Interleaving means both
        # measurements meet the same machine load, and the minimum is the right
        # estimator for the reason microbenchmarks use it: noise only ever adds
        # time, so the fastest observation is the least contaminated. A
        # regression big enough to matter here is a factor of several and
        # survives the minimum untouched.
        #
        # Worth the extra second of runtime. A test that fails for reasons
        # unrelated to the product teaches people to re-run it until it passes,
        # and that habit is how a real regression gets waved through later.
        probe = search = float("inf")
        raters_used: tuple = ()
        subjects_used: tuple = ()
        for _ in range(3):
            t0 = time.perf_counter()
            acc = 0
            for i in range(2_000_000):
                acc += i & 3
            probe = min(probe, time.perf_counter() - t0)
            assert acc

            t0 = time.perf_counter()
            raters_used, subjects_used, exhaustive = rel._largest_complete_block(
                rows, pool)
            search = min(search, time.perf_counter() - t0)

        assert probe > 0

        # It still has to find a real block; a fast wrong answer is no answer.
        assert len(raters_used) >= 2 and len(subjects_used) >= 2
        assert all(all(rows[s].get(r) is not None for r in raters_used)
                   for s in subjects_used)
        assert search < 4 * probe, (
            f"block search took {search:.2f}s against a {probe:.2f}s probe "
            f"({search / probe:.1f} probes); the budget is 4")
