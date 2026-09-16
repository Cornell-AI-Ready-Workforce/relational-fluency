"""Tests for server.reliability.

The point of this file is the first three test classes: every estimator is
asserted against a published worked example, and two of them are additionally
hand-computed here in full so a reader can check the arithmetic without leaving
the file. A statistics module nobody has checked against a known answer is
worse than none, because the study's gating decision -- whether the gold labels
are reliable enough to model -- depends on it.

Published sources used as ground truth:

  * ICC  -- Shrout, P. E. & Fleiss, J. L. (1979), Psychological Bulletin 86(2),
    420-428, Table 1 (6 targets x 4 judges). Reported: ICC(2,1) = .29,
    ICC(2,k) = .62, with ANOVA mean squares BMS 11.24, JMS 32.49, EMS 1.02.
  * Weighted kappa -- Cohen, J. (1968), Psychological Bulletin 70(4), 213-220.
    With exactly two categories every weighting scheme collapses to the
    unweighted kappa, which gives an independently computable target.
  * Krippendorff's alpha -- Krippendorff, K. (2011), "Computing Krippendorff's
    Alpha-Reliability", the canonical 3-observer x 15-unit reliability data
    matrix. Reported: alpha_nominal = .691, alpha_ordinal = .807,
    alpha_interval = .811, over 26 pairable values.
  * The QWK/ICC correspondence -- Fleiss, J. L. & Cohen, J. (1973),
    Educational and Psychological Measurement 33(3), 613-619: quadratic
    weighted kappa and the intraclass correlation are equivalent for two
    raters. This one cross-checks two independently written functions in this
    module against each other, which no single published constant can.

Run from the repo root:

    python -m pytest tests
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    # pytest puts tests/ on sys.path, not the repo root, so a plain
    # `pytest tests/test_reliability.py` would not find the server package.
    sys.path.insert(0, str(REPO_ROOT))

from server import reliability as rel  # noqa: E402

ITEMS_CSV = REPO_ROOT / "studies" / "study1" / "qualtrics" / "esci_construct4_items.csv"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _items_from_csv():
    """The item bank in the shape server.esci.all_items() returns.

    Read from the CSV that esci itself treats as the source of truth, so these
    tests exercise report() against the real 22 items with their real
    reverse-scoring flags whether or not server/esci.py is on the path yet.
    """
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


@pytest.fixture(scope="module")
def items():
    return _items_from_csv()


def _rating(session_id, rater_id, scores, seconds=420):
    return {
        "assignment_id": f"as_{abs(hash((session_id, rater_id))) % (16 ** 12):012x}",
        "session_id": session_id,
        "rater_id": rater_id,
        "scores": scores,
        "open_ended": {"better": "", "notable": ""},
        "seconds": seconds,
    }


def _flat(items_, value):
    """Every item scored `value` (or None)."""
    return {i["id"]: value for i in items_}


# The Shrout & Fleiss (1979) Table 1 data, 6 targets rated by 4 judges.
SHROUT_FLEISS = [
    [9, 2, 5, 8],
    [6, 1, 3, 2],
    [8, 4, 6, 8],
    [7, 1, 2, 6],
    [10, 5, 6, 9],
    [6, 2, 4, 7],
]

# Krippendorff's canonical reliability data, transposed to this module's
# orientation (rows = units, columns = observers). Observers A, B, C down the
# columns; "." in the paper is None here.
_N = None
KRIPPENDORFF_OBSERVERS = [
    [_N, _N, _N, _N, _N, 3, 4, 1, 2, 1, 1, 3, 3, _N, 3],
    [1, _N, 2, 1, 3, 3, 4, 3, _N, _N, _N, _N, _N, _N, _N],
    [_N, _N, 2, 1, 3, 4, 4, _N, 2, 1, 1, 3, 3, _N, 4],
]
KRIPPENDORFF_UNITS = [list(col) for col in zip(*KRIPPENDORFF_OBSERVERS)]


# --------------------------------------------------------------------------
# ICC against published and hand-computed answers
# --------------------------------------------------------------------------

class TestICCAgainstKnownAnswers:

    def test_matches_shrout_and_fleiss_1979_table_1(self):
        """The textbook case. ICC(2,1) = .29 and ICC(2,k) = .62."""
        r = rel.icc(SHROUT_FLEISS)
        assert r["computable"] is True
        assert r["n"] == 6 and r["k"] == 4
        assert r["icc_2_1"] == pytest.approx(0.290, abs=0.001)
        assert r["icc_2_k"] == pytest.approx(0.620, abs=0.001)

    def test_reproduces_the_published_anova_table(self):
        """BMS 11.24, JMS 32.49, EMS 1.02 -- the intermediate quantities.

        Asserting the mean squares as well as the coefficient matters: two
        different bugs (a wrong partition and a wrong final formula) can cancel
        and still land on .29 for one dataset.
        """
        r = rel.icc(SHROUT_FLEISS)
        assert r["ms_subjects"] == pytest.approx(11.24, abs=0.01)
        assert r["ms_raters"] == pytest.approx(32.49, abs=0.01)
        assert r["ms_error"] == pytest.approx(1.02, abs=0.01)
        assert (r["df_subjects"], r["df_raters"], r["df_error"]) == (5, 3, 15)

    def test_hand_computed_absolute_agreement(self):
        """Four subjects, three raters, arithmetic done by hand below.

            S1:  1  2  3      row means:  2, 5, 8, 11
            S2:  4  5  6      col means:  5.5, 6.5, 7.5
            S3:  7  8  9      grand mean: 78/12 = 6.5
            S4: 10 11 12

            SSR = k * sum (rowmean - grand)^2
                = 3 * (20.25 + 2.25 + 2.25 + 20.25) = 3 * 45 = 135, df 3
            SSC = n * sum (colmean - grand)^2
                = 4 * (1 + 0 + 1) = 8,                            df 2
            SST = sum (x - 6.5)^2 = 2 * (30.25 + 20.25 + 12.25
                                         + 6.25 + 2.25 + 0.25) = 143
            SSE = 143 - 135 - 8 = 0,                              df 6

            MSR = 45, MSC = 4, MSE = 0

            ICC(2,1) = (45 - 0) / (45 + 2*0 + 3*(4 - 0)/4) = 45/48 = 0.9375
            ICC(2,k) = (45 - 0) / (45 + (4 - 0)/4)         = 45/46 = 0.97826...

        This case also demonstrates *why* the absolute-agreement form was
        chosen. Rater 3 is exactly one point more generous than rater 1 on
        every subject, so the raters agree perfectly on the ordering and a
        consistency ICC(3,1) would be 1.0. Absolute agreement is 0.9375,
        because a gold label that feeds a model is a number and the raters do
        not agree on the number.
        """
        r = rel.icc([[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]])
        assert r["ms_subjects"] == pytest.approx(45.0)
        assert r["ms_raters"] == pytest.approx(4.0)
        assert r["ms_error"] == pytest.approx(0.0, abs=1e-9)
        assert r["icc_2_1"] == pytest.approx(45 / 48)
        assert r["icc_2_k"] == pytest.approx(45 / 46)
        assert r["icc_2_1"] < 1.0  # perfect consistency, imperfect agreement

    def test_identical_rater_means_make_agreement_equal_consistency(self):
        """When MSC == MSE the absolute-agreement term vanishes.

        ICC(2,1) then reduces to ICC(3,1) = (MSR - MSE)/(MSR + (k-1)MSE),
        which is computed here by hand from the module's own mean squares.
        """
        matrix = [[1, 2], [2, 1], [4, 5], [5, 4]]  # rater means both 3.0
        r = rel.icc(matrix)
        msr, msc, mse = r["ms_subjects"], r["ms_raters"], r["ms_error"]
        assert msc == pytest.approx(0.0, abs=1e-12)
        consistency = (msr - mse) / (msr + (r["k"] - 1) * mse)
        # MSC is 0, not equal to MSE, so the two forms differ a little; the
        # point is that they are close, not that the term is absent.
        assert r["icc_2_1"] == pytest.approx(consistency, abs=0.05)


class TestICCDegenerateCases:

    def test_single_rater(self):
        r = rel.icc([[1], [2], [3]])
        assert r["computable"] is False
        assert "fewer than two raters" in r["reason"]
        assert r["icc_2_1"] is None and r["icc_2_k"] is None

    def test_single_subject(self):
        r = rel.icc([[1, 2, 3]])
        assert r["computable"] is False
        assert "fewer than two subjects" in r["reason"]

    def test_empty_matrix(self):
        r = rel.icc([])
        assert r["computable"] is False
        assert r["n"] == 0 and r["k"] == 0

    def test_zero_variance_says_so_rather_than_returning_a_number(self):
        """A panel that always says 3 agrees perfectly and measures nothing."""
        r = rel.icc([[3, 3, 3]] * 8)
        assert r["computable"] is False
        assert r["icc_2_1"] is None
        assert "no variance" in r["reason"]
        assert "discriminates nothing" in r["reason"]

    def test_column_every_rater_left_blank_is_dropped_not_counted(self):
        """An item every rater marked N/A -- here, one rater who scored none.

        The empty column is removed rather than turning every subject into an
        incomplete case, which would report zero subjects instead of a working
        two-rater ICC.
        """
        matrix = [[1, 2, None], [2, 3, None], [4, 4, None], [5, 5, None]]
        r = rel.icc(matrix)
        assert r["computable"] is True
        assert r["k"] == 2
        assert r["n_raters_given"] == 3 and r["n_raters_dropped"] == 1
        assert r["n"] == 4

    def test_whole_matrix_na(self):
        r = rel.icc([[None, None], [None, None]])
        assert r["computable"] is False
        assert "fewer than two raters" in r["reason"]

    def test_listwise_deletion_is_counted(self):
        matrix = [[1, 2], [2, None], [4, 4], [5, 5], [None, 3]]
        r = rel.icc(matrix)
        assert r["n_subjects_given"] == 5
        assert r["n"] == 3
        assert r["n_subjects_dropped"] == 2

    def test_too_few_complete_subjects_after_deletion(self):
        matrix = [[1, None], [None, 2], [3, 3]]
        r = rel.icc(matrix)
        assert r["computable"] is False
        assert "complete subject-by-rater table" in r["reason"]
        assert r["n"] == 1

    def test_negative_icc_is_reported_not_clamped(self):
        """Raters disagree more within a subject than between subjects."""
        matrix = [[1, 5], [5, 1], [1, 5], [5, 1]]
        r = rel.icc(matrix)
        assert r["computable"] is True
        assert r["icc_2_1"] < 0
        assert "negative ICC" in r["reason"]

    def test_ragged_matrix_raises(self):
        with pytest.raises(ValueError, match="ragged"):
            rel.icc([[1, 2], [3]])

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError, match="non-numeric"):
            rel.icc([["a", 2], [3, 4]])

    def test_boolean_is_not_a_rating(self):
        with pytest.raises(ValueError, match="boolean"):
            rel.icc([[True, 2], [3, 4]])


# --------------------------------------------------------------------------
# quadratic weighted kappa
# --------------------------------------------------------------------------

class TestQuadraticWeightedKappa:

    def test_hand_computed(self):
        """Six subjects, k = 5, weights w_ij = (i-j)^2 / 16.

            a = [1, 1, 1, 2, 2, 2]
            b = [1, 1, 2, 2, 3, 3]

            pair weights: 0, 0, 1/16, 0, 1/16, 1/16
            observed disagreement = (3/16) / 6 = 3/96

            marginals  a: 1 -> 1/2, 2 -> 1/2
                       b: 1 -> 1/3, 2 -> 1/3, 3 -> 1/3
            expected   = 1/2 * 1/3 * (w12 + w13 + w21 + w23)
                       = 1/6 * (1/16 + 4/16 + 1/16 + 1/16) = 7/96

            kappa = 1 - (3/96)/(7/96) = 1 - 3/7 = 4/7 = 0.571428...
        """
        k = rel.quadratic_weighted_kappa([1, 1, 1, 2, 2, 2], [1, 1, 2, 2, 3, 3])
        assert k == pytest.approx(4 / 7)

    def test_two_categories_equals_cohens_unweighted_kappa(self):
        """Cohen (1968): with two categories all weightings coincide.

            confusion  (1,1)=3  (1,2)=1  (2,1)=1  (2,2)=5,  n = 10
            po = 8/10 = 0.80
            pe = 0.4*0.4 + 0.6*0.6 = 0.52
            kappa = (0.80 - 0.52) / (1 - 0.52) = 0.28/0.48 = 0.583333...

        An independent target: the unweighted formula shares no code with the
        weighted one under test.
        """
        a = [1, 1, 1, 1, 2, 2, 2, 2, 2, 2]
        b = [1, 1, 1, 2, 1, 2, 2, 2, 2, 2]
        assert rel.quadratic_weighted_kappa(a, b) == pytest.approx(0.28 / 0.48)

    def test_equivalent_to_icc_for_two_raters(self):
        """Fleiss & Cohen (1973). Cross-checks kappa against the ICC code.

        The equivalence is asymptotic, so the agreement tightens with n; at
        n = 240 the two independently written implementations land within
        0.005 of each other. If either formula were wrong they would not.
        """
        import random
        rng = random.Random(7)
        matrix = []
        for _ in range(240):
            true = rng.gauss(3, 1)
            matrix.append([min(5, max(1, round(true + rng.gauss(0, 0.7))))
                           for _ in range(2)])
        q = rel.quadratic_weighted_kappa([r[0] for r in matrix],
                                         [r[1] for r in matrix])
        i = rel.icc(matrix)["icc_2_1"]
        assert q == pytest.approx(i, abs=0.005)

    def test_perfect_agreement_with_variance_is_one(self):
        assert rel.quadratic_weighted_kappa([1, 2, 3, 4, 5],
                                            [1, 2, 3, 4, 5]) == pytest.approx(1.0)

    def test_distance_is_penalised_quadratically(self):
        """A 1-vs-5 disagreement must cost more than a 3-vs-4 one."""
        near = rel.quadratic_weighted_kappa([1, 2, 3, 4, 5], [1, 2, 4, 4, 5])
        far = rel.quadratic_weighted_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 1])
        assert near > far

    def test_na_deleted_pairwise(self):
        """Administration note 3: N/A items are excluded pairwise.

        Dropping the two N/A subjects leaves exactly the perfect-agreement
        vector, so kappa is 1.0 rather than being dragged down by a gap.
        """
        a = [1, 2, None, 4, 5, 3]
        b = [1, 2, 3, None, 5, 3]
        d = rel.quadratic_weighted_kappa_detail(a, b)
        assert d["n"] == 4
        assert d["kappa"] == pytest.approx(1.0)

    def test_constant_ratings_are_undefined_not_perfect(self):
        """Both raters pressed 3 every time. That is not reliability."""
        d = rel.quadratic_weighted_kappa_detail([3] * 10, [3] * 10)
        assert d["computable"] is False
        assert math.isnan(d["kappa"])
        assert "straight-lining" in d["reason"]
        assert math.isnan(rel.quadratic_weighted_kappa([3] * 10, [3] * 10))

    def test_below_chance_agreement_is_negative(self):
        k = rel.quadratic_weighted_kappa([1, 5, 1, 5, 1, 5], [5, 1, 5, 1, 5, 1])
        assert k < 0

    def test_fewer_than_two_shared_subjects(self):
        d = rel.quadratic_weighted_kappa_detail([1, None], [None, 2])
        assert d["computable"] is False
        assert d["n"] == 0
        assert "at least two" in d["reason"]

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="equal-length"):
            rel.quadratic_weighted_kappa([1, 2, 3], [1, 2])

    def test_out_of_range_rating_raises(self):
        with pytest.raises(ValueError, match="outside 1..5"):
            rel.quadratic_weighted_kappa([1, 2, 6], [1, 2, 3])
        with pytest.raises(ValueError, match="outside 1..5"):
            rel.quadratic_weighted_kappa([1, 2, 0], [1, 2, 3])

    def test_non_integer_rating_raises(self):
        with pytest.raises(ValueError, match="not an integer category"):
            rel.quadratic_weighted_kappa([1, 2, 3.5], [1, 2, 3])

    def test_scale_size_only_bounds_the_valid_range(self):
        """`k` does not change the coefficient, which is not obvious.

        The (k-1)^2 normaliser is a constant factor in both the observed and
        the expected weighted disagreement, so it cancels in their ratio; and
        the expected term is built from the marginals actually observed, so
        categories nobody used contribute nothing either. Rating the same data
        on a 5-point and a 3-point scale therefore gives the same kappa. What
        `k` does do is decide which ratings are in range at all.
        """
        a, b = [1, 1, 2, 2, 3, 3], [1, 2, 2, 3, 3, 1]
        assert rel.quadratic_weighted_kappa(a, b, k=5) == pytest.approx(
            rel.quadratic_weighted_kappa(a, b, k=3))
        with pytest.raises(ValueError, match="outside 1..3"):
            rel.quadratic_weighted_kappa([1, 4], [1, 2], k=3)

    def test_k_below_two_raises(self):
        with pytest.raises(ValueError, match="at least 2"):
            rel.quadratic_weighted_kappa([1, 1], [1, 1], k=1)


# --------------------------------------------------------------------------
# Krippendorff's alpha
# --------------------------------------------------------------------------

class TestKrippendorffAlpha:

    def test_matches_the_canonical_worked_example(self):
        """Krippendorff (2011): .691 nominal, .807 ordinal, .811 interval.

        Three observers, fifteen units, two thirds of the cells missing --
        which is the whole reason this estimator is in the module. All three
        levels are checked because the difference between them is exactly the
        delta-squared function, the part most likely to be got wrong.
        """
        assert rel.krippendorff_alpha(KRIPPENDORFF_UNITS, "nominal") == pytest.approx(0.691, abs=0.001)
        assert rel.krippendorff_alpha(KRIPPENDORFF_UNITS, "ordinal") == pytest.approx(0.807, abs=0.001)
        assert rel.krippendorff_alpha(KRIPPENDORFF_UNITS, "interval") == pytest.approx(0.811, abs=0.001)

    def test_reproduces_the_published_pairable_counts(self):
        """26 pairable values over 12 usable units; 3 units are unpairable.

        Units 1, 2 and 14 carry fewer than two ratings. Observed nominal
        disagreement is 6/26, the published intermediate value.
        """
        d = rel.krippendorff_alpha_detail(KRIPPENDORFF_UNITS, "nominal")
        assert d["n_pairable_values"] == 26
        assert d["n_units_used"] == 12
        assert d["n_units_dropped"] == 3
        assert d["observed_disagreement"] == pytest.approx(6 / 26)

    def test_ordinal_and_interval_differ(self):
        """The ordinal metric is doing work, not aliasing the interval one."""
        o = rel.krippendorff_alpha(KRIPPENDORFF_UNITS, "ordinal")
        i = rel.krippendorff_alpha(KRIPPENDORFF_UNITS, "interval")
        assert o != i

    def test_default_level_is_ordinal(self):
        assert rel.krippendorff_alpha(KRIPPENDORFF_UNITS) == pytest.approx(
            rel.krippendorff_alpha(KRIPPENDORFF_UNITS, "ordinal"))

    def test_perfect_agreement_is_one(self):
        assert rel.krippendorff_alpha([[1, 1], [3, 3], [5, 5], [2, 2]]) == pytest.approx(1.0)

    def test_tolerates_missing_data_where_icc_cannot(self):
        """The case the module exists for: N/A everywhere, but pairable.

        Every subject is missing one of the three raters, so ICC has zero
        complete subjects and declines to answer, while alpha uses all 12
        pairable values.
        """
        m = [
            [1, 2, None], [2, None, 2], [None, 4, 4], [5, 5, None],
            [1, None, 1], [3, 3, None],
        ]
        assert rel.icc(m)["computable"] is False
        d = rel.krippendorff_alpha_detail(m)
        assert d["computable"] is True
        assert d["n_pairable_values"] == 12
        assert d["alpha"] > 0.5

    def test_units_with_one_rating_are_dropped(self):
        m = [[1, 1], [5, None], [5, 5], [None, None], [3, 3]]
        d = rel.krippendorff_alpha_detail(m)
        assert d["n_units_used"] == 3
        assert d["n_units_dropped"] == 2

    def test_every_rating_identical_is_undefined(self):
        d = rel.krippendorff_alpha_detail([[4, 4]] * 10)
        assert d["computable"] is False
        assert math.isnan(d["alpha"])
        assert "discrimination is nil" in d["reason"]

    def test_no_pairable_units(self):
        d = rel.krippendorff_alpha_detail([[1, None], [None, 2], [3, None]])
        assert d["computable"] is False
        assert d["n_units_used"] == 0
        assert "pairable" in d["reason"]

    def test_a_single_pairable_unit_is_refused_not_reported_as_zero(self):
        """One unit forces alpha to exactly 0 whatever the raters said.

        With a single unit the coincidence matrix and the product of its own
        marginals are built from the same values, so Do == De identically. An
        item that 25 of 26 raters marked "not enough information to judge"
        would otherwise be published as alpha = 0.000, which reads as "the
        raters disagreed" when the truth is "almost nobody rated it". The two
        cases below have opposite data and both used to return 0.0.
        """
        agree = [[4, 4]] + [[None, None]] * 25
        disagree = [[1, 5]] + [[None, None]] * 25
        for m in (agree, disagree):
            d = rel.krippendorff_alpha_detail(m)
            assert d["n_units_used"] == 1
            assert d["computable"] is False
            assert math.isnan(d["alpha"])
            assert "at least two pairable units" in d["reason"]

    def test_two_pairable_units_is_enough(self):
        d = rel.krippendorff_alpha_detail([[1, 1], [5, 5]] + [[None, None]] * 10)
        assert d["n_units_used"] == 2
        assert d["computable"] is True
        assert d["alpha"] == pytest.approx(1.0)

    def test_empty_matrix(self):
        d = rel.krippendorff_alpha_detail([])
        assert d["computable"] is False
        assert math.isnan(d["alpha"])

    def test_independent_raters_land_near_zero(self):
        """Systematic disagreement, i.e. no reliability at all."""
        m = [[1, 5], [5, 1], [2, 4], [4, 2], [1, 5], [5, 1]]
        assert rel.krippendorff_alpha(m, "interval") < 0.0

    def test_unknown_level_raises_even_on_degenerate_data(self):
        with pytest.raises(ValueError, match="unknown level"):
            rel.krippendorff_alpha([[1, 2], [3, 4]], "nomnal")
        with pytest.raises(ValueError, match="unknown level"):
            rel.krippendorff_alpha([[1, None]], "ratio")


# --------------------------------------------------------------------------
# report over a wave
# --------------------------------------------------------------------------

def _wave(items_, encounters, raters, scorer):
    """Build ratings for a crossed encounters x raters wave."""
    rows = []
    for s in encounters:
        for r in raters:
            rows.append(_rating(s, r, scorer(s, r)))
    return rows


class TestReport:

    def test_scores_every_construct_and_every_item(self, items):
        encounters = [f"s_{i}" for i in range(6)]
        raters = ["rt_a", "rt_b", "rt_c"]

        def scorer(s, r):
            base = 1 + (int(s.split("_")[1]) % 5)
            nudge = {"rt_a": 0, "rt_b": 0, "rt_c": 1}[r]
            return {i["id"]: min(5, base + nudge) for i in items}

        rep = rel.report("study", ratings=_wave(items, encounters, raters, scorer),
                         items=items)
        assert rep["n_ratings"] == 18
        assert rep["n_encounters"] == 6
        assert rep["n_raters"] == 3
        assert set(rep["constructs"]) == {
            "conflict_management", "influence",
            "inspirational_leadership", "teamwork"}
        assert len(rep["items"]) == 22
        assert rep["design"]["fully_crossed"] is True
        block = rep["constructs"]["teamwork"]
        assert block["n_encounters"] == 6 and block["n_raters"] == 3
        assert block["icc"]["computable"] is True
        assert block["krippendorff_alpha"] is not None

    def test_carries_the_proprietary_item_notice(self, items):
        rep = rel.report(None, ratings=[], items=items)
        notice = rep["item_source_notice"].lower()
        assert "proprietary" in notice
        assert "licensing" in notice
        assert "research reference" in notice

    def test_the_notice_is_esci_s_own_not_a_second_copy(self):
        """One warning, one wording, wherever the items go.

        Two copies of a licensing warning eventually say two different things,
        and the weaker one is what ends up in front of somebody.
        """
        esci = pytest.importorskip("server.esci")
        assert rel.ITEM_SOURCE_NOTICE == esci.NOTICE

    def test_reverse_scored_items_are_recoded(self, items):
        """Items 11, 15 and 24 are reverse-keyed.

        A rater who answers 5 ("Consistently allows conflict to fester") is
        scored 1. If the recoding were skipped the reverse items would be the
        only ones disagreeing with their own construct, so the check is that
        their construct mean matches the forward items' mean exactly.
        """
        forward = {i["id"]: 4 for i in items if not i["reverse"]}
        reverse = {i["id"]: 2 for i in items if i["reverse"]}  # 6 - 2 = 4
        scores = {**forward, **reverse}
        rows = [_rating(f"s_{n}", r, scores)
                for n in range(4) for r in ("rt_a", "rt_b")]
        rep = rel.report(None, ratings=rows, items=items)
        for iid, block in rep["items"].items():
            assert block["mean"] == pytest.approx(4.0), iid
        for name, block in rep["constructs"].items():
            assert block["mean"] == pytest.approx(4.0), name

    def test_construct_scale_is_the_mean_of_its_answered_items(self, items):
        """N/A items drop out of the scale rather than zeroing it."""
        cm = [i for i in items if i["construct"] == "conflict_management"]
        scores = {i["id"]: None for i in items}
        # 8 -> 5, 14 -> 3, 15 (reverse) -> 5 => 1. The other two are N/A.
        scores["ESCI-08"], scores["ESCI-14"], scores["ESCI-15"] = 5, 3, 5
        assert len(cm) == 5
        rows = [_rating(f"s_{n}", r, scores)
                for n in range(3) for r in ("rt_a", "rt_b")]
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["constructs"]["conflict_management"]["mean"] == pytest.approx(
            (5 + 3 + 1) / 3)

    def test_counts_na_and_warns_about_systematically_na_items(self, items):
        """The instrument predicts this for items 3 and 49 in S2."""
        def scorer(s, r):
            out = {i["id"]: 3 for i in items}
            out["ESCI-03"] = None
            out["ESCI-49"] = None
            return out

        rows = _wave(items, [f"s_{i}" for i in range(5)], ["rt_a", "rt_b"], scorer)
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["items"]["ESCI-03"]["na_rate"] == pytest.approx(1.0)
        assert rep["items"]["ESCI-08"]["na_rate"] == pytest.approx(0.0)
        assert rep["missing"]["item_cells_na"] == 20  # 2 items x 5 x 2
        assert any("ESCI-03" in w and "not enough information" in w
                   for w in rep["warnings"])
        # An item nobody could judge has no reliability, and says so.
        assert rep["items"]["ESCI-03"]["icc"]["computable"] is False
        assert rep["items"]["ESCI-03"]["krippendorff_alpha"] is None

    def test_construct_rows_carry_the_item_missingness_too(self, items):
        """A construct's own na_rate is not the number a reader wants.

        The scale is formed from any one answered item, so its na_rate is ~0
        even when most of the items behind it were N/A. The construct row
        therefore also carries the underlying item missingness, and the table
        prints that one.
        """
        def scorer(s, r):
            out = {i["id"]: 3 for i in items}
            for iid in ("ESCI-03", "ESCI-49", "ESCI-20"):
                out[iid] = None
            return out

        rows = _wave(items, [f"s_{i}" for i in range(4)], ["rt_a", "rt_b"], scorer)
        rep = rel.report(None, ratings=rows, items=items)
        infl = rep["constructs"]["influence"]
        assert infl["na_rate"] == pytest.approx(0.0)   # the scale always formed
        assert infl["item_na_rate"] == pytest.approx(3 / 6)  # 3 of 6 items N/A
        assert infl["item_cells_na"] == 24  # 3 items x 4 encounters x 2 raters
        assert rep["constructs"]["teamwork"]["item_na_rate"] == pytest.approx(0.0)

    def test_detects_a_pool_that_is_not_fully_crossed(self, items):
        """raters.assign draws per_encounter raters from a larger pool.

        Encounters rotate through a pool of four, three at a time, so no rater
        scored everything. ICC(2,1)'s fixed-panel assumption is violated; the
        report has to say so and say what it computed instead.
        """
        pool = ["rt_a", "rt_b", "rt_c", "rt_d"]
        rows = []
        for n in range(8):
            trio = [pool[(n + k) % 4] for k in range(3)]
            for r in trio:
                rows.append(_rating(f"s_{n}", r, {i["id"]: 2 + (n % 3) for i in items}))
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["design"]["fully_crossed"] is False
        assert rep["design"]["raters_per_encounter"]["max"] == 3
        assert any("not fully crossed" in w for w in rep["warnings"])
        assert any("Krippendorff" in w for w in rep["warnings"])
        block = rep["constructs"]["teamwork"]
        # Whatever it managed to compute, it names the restriction.
        assert set(block["icc"]["raters_used"]) <= set(pool)
        assert block["icc"]["subjects_available"] == 8
        assert block["icc"]["subjects_used"] <= 8

    def test_largest_complete_block_beats_naive_listwise_deletion(self, items):
        """Two raters did everything; a third did one encounter.

        Listwise deletion over all three would leave one complete subject and
        no ICC. Dropping the third rater leaves a full 6 x 2 table.
        """
        rows = []
        for n in range(6):
            for r in ("rt_a", "rt_b"):
                rows.append(_rating(f"s_{n}", r, {i["id"]: 1 + (n % 5) for i in items}))
        rows.append(_rating("s_0", "rt_c", {i["id"]: 3 for i in items}))
        rep = rel.report(None, ratings=rows, items=items)
        ic = rep["constructs"]["influence"]["icc"]
        assert ic["computable"] is True
        assert ic["raters_used"] == ["rt_a", "rt_b"]
        assert ic["subjects_used"] == 6

    def test_thin_and_duplicate_and_unknown_are_all_reported(self, items):
        scores = {i["id"]: 3 for i in items}
        rows = [
            _rating("s_0", "rt_a", scores),
            _rating("s_0", "rt_b", scores),
            _rating("s_0", "rt_b", scores),        # duplicate submission
            _rating("s_1", "rt_a", scores),        # only one rater
            _rating("s_2", "rt_a", {**scores, "ESCI-99": 4}),  # unknown item
            _rating("s_2", "rt_b", scores),
        ]
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["design"]["encounters_with_fewer_than_2_raters"] == ["s_1"]
        assert rep["design"]["duplicate_submissions"] == ["rt_b@s_0"]
        assert any("fewer than two ratings" in w for w in rep["warnings"])
        assert any("duplicate submissions" in w for w in rep["warnings"])
        assert any("ESCI-99" in w for w in rep["warnings"])

    def test_single_rater_wave_warns_and_computes_nothing(self, items):
        rows = [_rating(f"s_{n}", "rt_a", {i["id"]: 3 for i in items})
                for n in range(5)]
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["n_raters"] == 1
        assert any("no agreement statistic is defined" in w for w in rep["warnings"])
        assert rep["constructs"]["teamwork"]["icc"]["computable"] is False

    def test_rows_missing_identifiers_are_skipped_loudly(self, items):
        rows = [{"scores": {i["id"]: 3 for i in items}}]
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["n_encounters"] == 0
        assert any("no session_id/rater_id" in w for w in rep["warnings"])

    def test_encounter_id_is_accepted_as_well_as_session_id(self, items):
        scores = {i["id"]: 3 for i in items}
        rows = [{"encounter_id": "s_0", "rater_id": "rt_a", "scores": scores},
                {"encounter_id": "s_0", "rater_id": "rt_b", "scores": scores}]
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["n_encounters"] == 1

    def test_no_ratings(self, items):
        rep = rel.report("study", ratings=[], items=items)
        assert rep["n_ratings"] == 0
        assert rep["constructs"] == {} and rep["items"] == {}
        assert any("no ratings" in w for w in rep["warnings"])

    def test_no_item_bank(self):
        rep = rel.report(None, ratings=[], items=[])
        assert any("no item bank" in w for w in rep["warnings"])

    def test_construct_rows_decline_kappa_with_a_reason(self, items):
        rows = _wave(items, ["s_0", "s_1", "s_2"], ["rt_a", "rt_b"],
                     lambda s, r: {i["id"]: 3 if r == "rt_a" else 4 for i in items})
        rep = rel.report(None, ratings=rows, items=items)
        qwk = rep["constructs"]["teamwork"]["qwk"]
        assert qwk["mean"] is None
        assert "mean over items" in qwk["reason"]
        # Items do get kappa.
        assert rep["items"]["ESCI-12"]["qwk"]["n_pairs"] == 1

    def test_report_is_strict_json_even_when_cells_are_undefined(self, items):
        """No bare NaN anywhere, including in the uncomputable cells.

        The report is served as-is from GET /api/reliability. json.dumps
        writes NaN as a bare `NaN` token, which is not JSON, and a browser's
        JSON.parse rejects the whole document over it -- so one unrateable
        item would take down the researcher console's whole reliability view.
        This wave is built so that some cells cannot be computed: item 3 is
        N/A everywhere and one encounter has a single rater.
        """
        rows = []
        for n in range(4):
            for r in ("rt_a", "rt_b"):
                if n == 3 and r == "rt_b":
                    continue  # leaves an encounter with one rater
                scores = {i["id"]: 1 + ((n + len(r)) % 5) for i in items}
                scores["ESCI-03"] = None
                rows.append(_rating(f"s_{n}", r, scores))
        rep = rel.report(None, ratings=rows, items=items)
        assert rep["items"]["ESCI-03"]["alpha_detail"]["computable"] is False
        assert rep["items"]["ESCI-03"]["alpha_detail"]["alpha"] is None

        text = json.dumps(rep, default=str)
        assert "NaN" not in text and "Infinity" not in text
        assert rel.ITEM_SOURCE_NOTICE in text
        # Round-trips through a parser that rejects the JavaScript extensions,
        # which is what the browser will do with it.
        json.loads(text, parse_constant=_reject_constant)


def _reject_constant(name):
    raise AssertionError(f"report contained the non-JSON token {name!r}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class TestCLI:

    def test_json_mode_emits_a_parseable_report(self, capsys, monkeypatch):
        monkeypatch.setattr(rel, "report", lambda *a, **k: {
            "cohort": "study", "generated_at": "now",
            "item_source_notice": rel.ITEM_SOURCE_NOTICE,
            "sources": {"ratings": "x", "items": "y"},
            "n_ratings": 0, "n_encounters": 0, "n_raters": 0,
            "design": {}, "missing": {}, "constructs": {},
            "items": {"ESCI-08": {}}, "warnings": [],
        })
        assert rel.main(["--cohort", "study", "--json"]) == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["cohort"] == "study"

    def test_table_mode_prints_the_notice_above_the_items(self, capsys, items):
        rows = _wave(items, ["s_0", "s_1", "s_2"], ["rt_a", "rt_b"],
                     lambda s, r: {i["id"]: 1 + (int(s[-1]) % 5) for i in items})
        rel._print_report(rel.report(None, ratings=rows, items=items))
        out = capsys.readouterr().out
        assert rel.ITEM_SOURCE_NOTICE in out
        # The notice heads the item table rather than trailing it.
        assert out.index(rel.ITEM_SOURCE_NOTICE) < out.index("ESCI-08")
        assert "conflict_management" in out

    def test_exits_nonzero_when_there_is_nothing_to_report(self, capsys, monkeypatch):
        monkeypatch.setattr(rel, "report", lambda *a, **k: {
            "cohort": None, "generated_at": "now",
            "item_source_notice": rel.ITEM_SOURCE_NOTICE,
            "sources": {"ratings": "unavailable", "items": "unavailable"},
            "n_ratings": 0, "n_encounters": 0, "n_raters": 0,
            "design": {}, "missing": {}, "constructs": {}, "items": {},
            "warnings": ["no item bank"],
        })
        assert rel.main([]) == 1
        assert "no item bank" in capsys.readouterr().out


# --------------------------------------------------------------------------
# against the real item bank and a real collection wave
# --------------------------------------------------------------------------

class TestAgainstTheRealThing:

    def test_item_bank_is_the_instrument_the_report_expects(self, items):
        """22 items, 4 constructs, 3 reverse-keyed (11, 15, 24)."""
        assert len(items) == 22
        assert sorted({i["construct"] for i in items}) == [
            "conflict_management", "influence",
            "inspirational_leadership", "teamwork"]
        assert sorted(i["number"] for i in items if i["reverse"]) == [11, 15, 24]

    def test_report_over_a_real_collection_wave(self, items, wave_encounters):
        """Rate every recorded encounter in the wave and gate it.

        The fixture wave has no ratings on disk (Phase 2 has not been run), so
        the ratings are synthesised here; the encounter ids, their count and
        their distribution are the real ones, which is what exercises the
        report's aggregation, its missing-data accounting and its warnings at
        realistic scale.

        The wave comes from tests/conftest.py, which skips when there is not
        one. The old guard was `skipif DATA_DIR unset` plus a `sessions/`
        existence check, and those two do not cover the same ground:
        server.storage creates `DATA_DIR/sessions` on import, so DATA_DIR
        pointed at a fresh temp directory — an ordinary first local run — got
        past both and then failed on `assert encounters`. A missing wave and
        an empty one mean the same thing and now skip the same way.
        """
        import random
        encounters = wave_encounters
        # The assertions below are about behaviour "at realistic scale" — an
        # incomplete design with four raters, a computable ICC per construct.
        # A handful of encounters cannot produce that, so a small wave is a
        # skip with its size named, not a failure about the wave's size.
        if len(encounters) < 8:
            pytest.skip(f"the wave has only {len(encounters)} encounters; this "
                        "checks the report at collection scale (8 or more)")

        rng = random.Random(11)
        pool = ["rt_alice", "rt_bo", "rt_cai", "rt_dee"]
        rows = []
        for n, sid in enumerate(encounters):
            trio = [pool[(n + k) % len(pool)] for k in range(3)]
            truth = {i["id"]: rng.randint(1, 5) for i in items}
            for r in trio:
                scores = {}
                for i in items:
                    # Items 3 and 49 are the ones the instrument expects to be
                    # systematically unjudgeable in a dyadic scenario.
                    if i["number"] in (3, 49) and rng.random() < 0.8:
                        scores[i["id"]] = None
                    elif rng.random() < 0.05:
                        scores[i["id"]] = None
                    else:
                        scores[i["id"]] = min(5, max(
                            1, truth[i["id"]] + rng.choice([-1, 0, 0, 0, 1])))
                rows.append(_rating(sid, r, scores))

        rep = rel.report("study", ratings=rows, items=items)
        assert rep["n_encounters"] == len(encounters)
        assert rep["n_raters"] == 4
        assert len(rep["items"]) == 22
        assert rep["design"]["fully_crossed"] is False
        # Every construct produced a usable ICC and a usable alpha at this
        # scale, which is the shape of answer the Phase 2 gate needs.
        for name, block in rep["constructs"].items():
            assert block["icc"]["computable"] is True, (name, block["icc"]["reason"])
            assert block["krippendorff_alpha"] is not None, name
            assert -1.0 <= block["icc"]["icc_2_1"] <= 1.0
        # The predicted systematic-N/A items were flagged.
        assert rep["items"]["ESCI-03"]["na_rate"] > 0.5
        assert any("ESCI-03" in w for w in rep["warnings"])
        json.dumps(rep, default=str)
