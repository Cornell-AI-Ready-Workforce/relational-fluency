"""Inter-rater reliability for the Phase 2 gold labels.

Phase 2 turns recorded encounters into gold labels: 2-3 independent raters score
each encounter on the 22 ESCI Relationship Management items, and reliability is
computed per construct BEFORE anything is modelled. This module is that gate.
If the numbers here are wrong the study proceeds on labels nobody has checked,
so every formula below names its source and every one of them is asserted
against a published worked example in tests/test_reliability.py.

Three estimators, because they answer three different questions:

  * ``icc`` -- ICC(2,1) and ICC(2,k), two-way random effects, absolute
    agreement. The form for "the same raters scored every encounter and I care
    whether they land on the same *number*, not merely the same ordering".
    ICC(2,k) is what the rating instrument's own reliability plan asks for,
    because the gold label is the mean over k raters, not one rater's score.
  * ``quadratic_weighted_kappa`` -- pairwise agreement on the 1..5 ordinal
    scale, penalising a 1-vs-5 disagreement 16 times as hard as a 3-vs-4. Per
    item, per rater pair. It is the number that tells you *which two raters*
    are drifting, which an ICC over the whole panel cannot.
  * ``krippendorff_alpha`` -- the one that tolerates missing data, which is why
    it is here at all. The instrument requires a "not enough information to
    judge" option (rating-instrument.md, Scale), stored as null; item 3 and
    item 49 are expected to be systematically N/A in S2's dyadic setting. An
    estimator that needs a complete matrix would silently throw those cells
    away. Alpha does not.

Pure Python on purpose. requirements.txt does not carry numpy or scipy and must
not start: the deploy image is pinned per study wave and a statistics import is
not worth a re-pin. Everything here is arithmetic over lists.

What is deliberately NOT computed: confidence intervals around the ICCs. The
McGraw & Wong interval needs quantiles of the F distribution, and a CI that has
not been checked against a published answer is exactly the kind of number this
module exists to avoid. The ANOVA mean squares, the F ratio and the degrees of
freedom are all reported instead, so a researcher can read the interval off a
table or out of R, and knows precisely which numbers to feed it.

Matrix orientation, used by all three estimators and stated once here: a matrix
is a list of rows, one row per SUBJECT (an encounter, or an encounter-item
cell), each row a list of one value per RATER, in the same rater order in every
row. ``None`` means the rater did not supply a value -- either they marked "not
enough information to judge" or the encounter was never assigned to them. The
two are different facts and ``report`` counts them separately, but to the
estimators both are simply absent.

    python -m server.reliability [--cohort study] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# The scale bounds live in server.esci, which owns the item bank. They are
# imported rather than restated so a change to the instrument's scale reaches
# this module; the fallback exists only so reliability can be imported and
# tested against an injected item bank before esci is on the path.
#
# ITEM_SOURCE_NOTICE is esci's NOTICE, aliased rather than restated. The
# rating instrument's own header carries this warning and it travels with the
# items everywhere they go -- into a rater's console, into an export, and into
# this report, which names every item it scores. A licensing warning that
# exists in two places is a licensing warning that will eventually say two
# different things, and the weaker copy is the one that ends up in front of
# somebody. Do not remove it and do not bury it: the CLI prints it above the
# item table, not in a footnote.
#
# The fallbacks exist only so this module can be imported and tested against an
# injected item bank before esci is on the path.
try:  # pragma: no cover -- exercised by whichever half of the branch runs
    from .esci import NOTICE as ITEM_SOURCE_NOTICE
    from .esci import SCALE_MAX, SCALE_MIN
except Exception:  # pragma: no cover
    SCALE_MIN, SCALE_MAX = 1, 5
    ITEM_SOURCE_NOTICE = (
        "ESCI items (Boyatzis, Goleman & Korn Ferry) are a proprietary "
        "instrument, reproduced here for research reference only. Confirm "
        "licensing/permission before fielding."
    )

Number = Optional[float]
Matrix = Sequence[Sequence[Number]]


# --------------------------------------------------------------------------
# shared matrix handling
# --------------------------------------------------------------------------

def _validate(matrix: Matrix) -> Tuple[List[List[Number]], int]:
    """Coerce to a rectangular list-of-lists and return (rows, n_raters).

    A ragged matrix is a caller bug, not a data condition, so it raises rather
    than being reported as "not computable": the distinction matters, because
    everything this module returns as not-computable is something a real wave
    can legitimately produce and a researcher has to be able to read past.
    """
    rows = [list(r) for r in matrix]
    if not rows:
        return [], 0
    widths = {len(r) for r in rows}
    if len(widths) != 1:
        raise ValueError(
            f"matrix is ragged: rows have widths {sorted(widths)}; every row "
            "must carry one cell per rater, with None where a rater gave none"
        )
    for r in rows:
        for v in r:
            if v is not None and not isinstance(v, (int, float)):
                raise ValueError(f"non-numeric rating {v!r}")
            if isinstance(v, bool):  # bool is an int; it is never a rating
                raise ValueError("boolean is not a rating")
    return rows, len(rows[0])


def _drop_empty_raters(rows: List[List[Number]]) -> Tuple[List[List[Number]], List[int]]:
    """Drop rater columns that are entirely None, returning the kept indices.

    A rater who was assigned nothing (or who marked every item N/A) is not a
    rater with zero agreement, they are a rater who is not in this analysis.
    Leaving the column in would make every subject incomplete and take the ICC
    to zero subjects.
    """
    if not rows:
        return rows, []
    keep = [j for j in range(len(rows[0]))
            if any(r[j] is not None for r in rows)]
    if len(keep) == len(rows[0]):
        return rows, keep
    return [[r[j] for j in keep] for r in rows], keep


# --------------------------------------------------------------------------
# ICC
# --------------------------------------------------------------------------

def icc(matrix: Matrix) -> Dict[str, Any]:
    """ICC(2,1) and ICC(2,k): two-way random effects, absolute agreement.

    Shrout, P. E. & Fleiss, J. L. (1979), "Intraclass correlations: uses in
    assessing rater reliability", Psychological Bulletin 86(2), 420-428, case
    2; equivalently McGraw, K. O. & Wong, S. P. (1996), "Forming inferences
    about some intraclass correlation coefficients", Psychological Methods
    1(1), 30-46, forms ICC(A,1) and ICC(A,k).

    Partition the n x k table with a two-way ANOVA (n subjects, k raters):

        MSR  between-subject mean square,  df = n - 1
        MSC  between-rater   mean square,  df = k - 1
        MSE  residual        mean square,  df = (n - 1)(k - 1)

                             MSR - MSE
        ICC(2,1) = ---------------------------------------------
                   MSR + (k-1)*MSE + k*(MSC - MSE)/n

                             MSR - MSE
        ICC(2,k) = -----------------------------
                   MSR + (MSC - MSE)/n

    The ``k*(MSC - MSE)/n`` term is what makes this *absolute* agreement rather
    than consistency: a rater who is reliably two points harsh than the rest is
    penalised here, and would not be by ICC(3,1). That is the right choice for
    this study, because the gold label is used as a number -- a Phase-3 model
    trained against it learns the raters' level, not just their ranking.

    Missing data is handled by listwise deletion of subjects: a subject any
    rater left blank is dropped, and the count is reported. That is the only
    thing the ANOVA identity permits (an unbalanced two-way random model needs
    REML, which needs a dependency we do not have). It is also why
    ``krippendorff_alpha`` is computed alongside for every cell in ``report``:
    where N/A is systematic, alpha is the number to read.

    Returns a dict rather than a float so a degenerate wave can say why it is
    degenerate instead of returning a plausible-looking zero.
    """
    given, _ = _validate(matrix)
    n_given = len(given)
    rows, kept = _drop_empty_raters(given)
    k = len(rows[0]) if rows else 0

    out: Dict[str, Any] = {
        "icc_2_1": None,
        "icc_2_k": None,
        "n": 0,
        "k": k,
        "n_subjects_given": n_given,
        "n_subjects_dropped": 0,
        "n_raters_given": len(given[0]) if given else 0,
        "n_raters_dropped": (len(given[0]) - len(kept)) if given else 0,
        "ms_subjects": None,
        "ms_raters": None,
        "ms_error": None,
        "df_subjects": None,
        "df_raters": None,
        "df_error": None,
        "f_subjects": None,
        "computable": False,
        "reason": None,
    }

    complete = [r for r in rows if all(v is not None for v in r)]
    out["n"] = len(complete)
    out["n_subjects_dropped"] = n_given - len(complete)

    # An empty table has no raters in it, so the k < 2 branch below would
    # describe it as "fewer than two raters with any data (0)" -- true of the
    # matrix, and read as a claim about the wave it came from, which is where a
    # researcher starts hunting for ratings that are not missing. Say what is
    # actually the matter instead.
    if not given:
        out["reason"] = (
            "the table has no rows: no subject carries a rating here, so there "
            "is nothing to partition"
        )
        return out
    if k < 2:
        out["reason"] = (
            f"fewer than two raters with any data ({k}); an intraclass "
            "correlation needs at least two"
        )
        return out
    if len(complete) < 2:
        out["reason"] = (
            f"fewer than two subjects rated by all {k} raters "
            f"({len(complete)} of {n_given}); ICC(2,1) needs a complete "
            "subject-by-rater table"
        )
        return out

    n = len(complete)
    values = [float(v) for r in complete for v in r]  # type: ignore[arg-type]
    grand = sum(values) / len(values)
    row_means = [sum(float(v) for v in r) / k for r in complete]  # type: ignore[arg-type]
    col_means = [sum(float(r[j]) for r in complete) / n for j in range(k)]  # type: ignore[arg-type]

    ss_total = sum((v - grand) ** 2 for v in values)
    ss_rows = k * sum((m - grand) ** 2 for m in row_means)
    ss_cols = n * sum((m - grand) ** 2 for m in col_means)
    # Floating point can push a structurally-zero residual very slightly
    # negative (the ICC(2,1)=0.9375 case in the tests does exactly this), which
    # would make MSE negative and the coefficient nonsense. Clamp at zero: a
    # residual sum of squares cannot be negative.
    ss_err = max(0.0, ss_total - ss_rows - ss_cols)

    df_rows, df_cols, df_err = n - 1, k - 1, (n - 1) * (k - 1)
    ms_rows, ms_cols, ms_err = ss_rows / df_rows, ss_cols / df_cols, ss_err / df_err
    out.update({
        "ms_subjects": ms_rows, "ms_raters": ms_cols, "ms_error": ms_err,
        "df_subjects": df_rows, "df_raters": df_cols, "df_error": df_err,
        "f_subjects": (ms_rows / ms_err) if ms_err > 0 else None,
    })

    if ss_total == 0:
        out["reason"] = (
            "every rating in the table is identical, so there is no variance "
            "to partition into subject and rater components; agreement is "
            "total but reliability is undefined (a panel that always says 3 "
            "agrees perfectly and discriminates nothing)"
        )
        return out

    denom_1 = ms_rows + (k - 1) * ms_err + k * (ms_cols - ms_err) / n
    denom_k = ms_rows + (ms_cols - ms_err) / n
    if denom_1 == 0 or denom_k == 0:
        out["reason"] = "the ICC denominator is zero; the estimate is undefined"
        return out

    out["icc_2_1"] = (ms_rows - ms_err) / denom_1
    out["icc_2_k"] = (ms_rows - ms_err) / denom_k
    out["computable"] = True
    # A negative ICC is reported as it stands. It means the residual exceeded
    # the between-subject variance, i.e. two raters scoring the same encounter
    # disagree more than two raters scoring different encounters. That is a
    # real and important finding about a rater panel, and rounding it up to
    # zero would hide it.
    if out["icc_2_1"] is not None and out["icc_2_1"] < 0:
        out["reason"] = (
            "negative ICC: residual variance exceeds between-subject variance, "
            "i.e. raters agree less about the same encounter than about "
            "different ones"
        )
    return out


# --------------------------------------------------------------------------
# quadratic weighted kappa
# --------------------------------------------------------------------------

def _kappa_pairs(a: Iterable[Number], b: Iterable[Number], k: int
                 ) -> List[Tuple[int, int]]:
    av, bv = list(a), list(b)
    if len(av) != len(bv):
        raise ValueError(
            f"quadratic_weighted_kappa needs two equal-length rating vectors; "
            f"got {len(av)} and {len(bv)}"
        )
    if k < 2:
        raise ValueError(f"k must be at least 2; got {k}")
    pairs: List[Tuple[int, int]] = []
    for x, y in zip(av, bv):
        # Pairwise deletion, as the instrument's administration note 3
        # requires: an item one rater marked N/A cannot contribute to the
        # agreement between them, but the rest of the encounter still can.
        if x is None or y is None:
            continue
        for v in (x, y):
            if isinstance(v, bool) or float(v) != int(v):
                raise ValueError(f"rating {v!r} is not an integer category")
            if not (1 <= int(v) <= k):
                raise ValueError(f"rating {v!r} is outside 1..{k}")
        pairs.append((int(x), int(y)))
    return pairs


def quadratic_weighted_kappa_detail(a: Iterable[Number], b: Iterable[Number],
                                    k: int = 5) -> Dict[str, Any]:
    """The kappa plus everything needed to see why it is what it is.

    ``quadratic_weighted_kappa`` is the thin float-returning wrapper the module
    contract names; this is what ``report`` calls, because a bare NaN in a
    reliability table is a question, not an answer.
    """
    pairs = _kappa_pairs(a, b, k)
    out: Dict[str, Any] = {
        "kappa": float("nan"),
        "n": len(pairs),
        "observed_disagreement": None,
        "expected_disagreement": None,
        "k": k,
        "computable": False,
        "reason": None,
    }
    if len(pairs) < 2:
        out["reason"] = (
            f"only {len(pairs)} subject(s) scored by both raters; kappa needs "
            "at least two"
        )
        return out

    n = len(pairs)
    # Cohen, J. (1968), "Weighted kappa: nominal scale agreement with provision
    # for scaled disagreement or partial credit", Psychological Bulletin 70(4),
    # 213-220. Quadratic weights w_ij = (i-j)^2 / (k-1)^2, so the weight is a
    # *disagreement* and kappa = 1 - sum(w*O)/sum(w*E).
    span = float((k - 1) ** 2)

    def w(i: int, j: int) -> float:
        return ((i - j) ** 2) / span

    observed = sum(w(i, j) for i, j in pairs) / n

    ma = [0] * (k + 1)
    mb = [0] * (k + 1)
    for i, j in pairs:
        ma[i] += 1
        mb[j] += 1
    expected = 0.0
    for i in range(1, k + 1):
        if not ma[i]:
            continue
        pa = ma[i] / n
        for j in range(1, k + 1):
            if not mb[j] or i == j:
                continue
            expected += pa * (mb[j] / n) * w(i, j)

    out["observed_disagreement"] = observed
    out["expected_disagreement"] = expected
    if expected == 0:
        # Both raters used exactly one category, and the same one. Chance
        # agreement is also 100%, so kappa is 0/0. Reporting 1.0 here (as some
        # implementations do) would claim perfect reliability for a panel that
        # pressed the same button 27 times, which is the single most dangerous
        # thing this module could say.
        out["reason"] = (
            "both raters used a single category throughout, so agreement "
            "expected by chance is already total and kappa is undefined; "
            "check for straight-lining before reading this as agreement"
        )
        return out
    out["kappa"] = 1.0 - observed / expected
    out["computable"] = True
    return out


def quadratic_weighted_kappa(a: Iterable[Number], b: Iterable[Number],
                             k: int = 5) -> float:
    """Quadratic weighted kappa between two raters on a 1..k ordinal scale.

    ``a`` and ``b`` are aligned rating vectors, one entry per subject; ``None``
    entries (the instrument's N/A) are deleted pairwise. Returns NaN when the
    coefficient is undefined -- see ``quadratic_weighted_kappa_detail`` for the
    reason, which is always worth reading before treating a NaN as a gap.

    ``k`` bounds the valid categories; it does not scale the result. The
    (k-1)^2 normaliser is a constant in both the observed and the expected
    weighted disagreement and cancels in their ratio, and the expected term
    uses only the marginals actually observed, so a category nobody used
    changes nothing. Rating the same data with k=5 and k=3 gives the same
    number. That is worth stating because it looks like it should not.
    """
    return float(quadratic_weighted_kappa_detail(a, b, k)["kappa"])


# --------------------------------------------------------------------------
# Krippendorff's alpha
# --------------------------------------------------------------------------

def _coincidences(rows: List[List[Number]]) -> Tuple[List[float], Dict[Tuple[int, int], float],
                                                     List[float], int, int]:
    """Build Krippendorff's coincidence matrix from units x observers data.

    Returns (values, o, n_c, units_used, units_dropped) where ``values`` is the
    sorted list of distinct observed values, ``o`` is the coincidence matrix
    keyed by index pairs into ``values``, and ``n_c`` its row sums.

    Krippendorff, K. (2011), "Computing Krippendorff's Alpha-Reliability",
    Departmental Papers (ASC), University of Pennsylvania. Each unit
    contributes its pairable values: a unit rated by m observers contributes
    each ordered pair once, divided by (m - 1), so a unit with more raters does
    not weigh more heavily than its information warrants. A unit with a single
    value is unpairable and is dropped -- which is exactly the tolerance for
    missing data this estimator is here for.
    """
    present = sorted({float(v) for r in rows for v in r if v is not None})
    index = {v: i for i, v in enumerate(present)}
    o: Dict[Tuple[int, int], float] = {}
    used = dropped = 0
    for r in rows:
        vals = [float(v) for v in r if v is not None]
        m = len(vals)
        if m < 2:
            dropped += 1
            continue
        used += 1
        share = 1.0 / (m - 1)
        for x in range(m):
            for y in range(m):
                if x == y:
                    continue
                key = (index[vals[x]], index[vals[y]])
                o[key] = o.get(key, 0.0) + share
    n_c = [0.0] * len(present)
    for (i, j), v in o.items():
        n_c[i] += v
    return present, o, n_c, used, dropped


def _delta2(level: str, values: List[float], n_c: List[float]) -> Any:
    """The squared difference function for a level of measurement.

    ``nominal``  0 when equal, 1 otherwise.
    ``ordinal``  (sum of the marginal frequencies from rank c to rank k, minus
                 half of the two end frequencies) squared -- Krippendorff's
                 ordinal metric, which measures distance in *observed ranks*
                 rather than in the arbitrary numbers used to label them. This
                 is the right level for a 1..5 "Never .. Consistently" scale:
                 nothing in the instrument claims the step from Never to Rarely
                 is the same size as Often to Consistently.
    ``interval`` (c - k)^2. Used by ``report`` for construct-scale scores,
                 which are means over items and no longer sit on the 5 ordinal
                 rungs the ordinal metric counts.

    These three and no more. Krippendorff also defines a ratio metric, and it
    would be four lines; it is absent because the tests here check every level
    against a published worked example and there is no such example for ratio
    that this module's author could confirm. An unchecked estimator in a module
    whose whole job is to be checkable is worse than a missing one, and nothing
    in this study measures anything on a ratio scale anyway.
    """
    if level == "nominal":
        return lambda i, j: 0.0 if i == j else 1.0
    if level == "interval":
        return lambda i, j: (values[i] - values[j]) ** 2
    if level == "ordinal":
        prefix = [0.0]
        for v in n_c:
            prefix.append(prefix[-1] + v)

        def _ord(i: int, j: int) -> float:
            lo, hi = (i, j) if i <= j else (j, i)
            between = prefix[hi + 1] - prefix[lo]
            return (between - (n_c[lo] + n_c[hi]) / 2.0) ** 2
        return _ord
    raise ValueError(
        f"unknown level of measurement {level!r}; expected one of "
        "nominal, ordinal, interval"
    )


def krippendorff_alpha_detail(matrix: Matrix, level: str = "ordinal") -> Dict[str, Any]:
    """Alpha with the observed and expected disagreements it was built from."""
    rows, _ = _validate(matrix)
    values, o, n_c, used, dropped = _coincidences(rows)
    n = sum(n_c)
    out: Dict[str, Any] = {
        "alpha": float("nan"),
        "level": level,
        "n_units": len(rows),
        "n_units_used": used,
        "n_units_dropped": dropped,
        "n_pairable_values": int(round(n)),
        "n_distinct_values": len(values),
        "observed_disagreement": None,
        "expected_disagreement": None,
        "computable": False,
        "reason": None,
    }
    # Validate the level even on a degenerate matrix, so a typo in `level` is
    # an error rather than a quietly uncomputable cell.
    delta2 = _delta2(level, values, n_c)
    if used < 2 or n < 2:
        # Two pairable units, not one. A single unit does not merely give a
        # weak estimate, it gives a fixed one: with one unit the coincidence
        # matrix and the product of its own marginals are built from the same
        # handful of values, so Do and De coincide and alpha comes out at
        # exactly 0 whatever the raters said. (Take one unit with two unequal
        # values x and y: o_xy = o_yx = 1, n_x = n_y = 1, n = 2, so
        # Do = delta^2 and De = 2*delta^2/(2*1) = delta^2.) An item 25 of 26
        # raters marked "not enough information to judge" would otherwise be
        # published as alpha = 0.000, which reads as "the raters disagreed"
        # when the truth is "nobody rated it".
        out["reason"] = (
            f"only {used} unit(s) carry two or more ratings; alpha needs at "
            "least two pairable units, because a single unit forces observed "
            "and expected disagreement to be equal and alpha to zero "
            "regardless of the ratings"
        )
        return out
    if len(values) < 2:
        out["reason"] = (
            "every rating is the same value, so expected disagreement is zero "
            "and alpha is undefined; agreement is total and discrimination is "
            "nil"
        )
        return out

    do = sum(v * delta2(i, j) for (i, j), v in o.items()) / n
    de = 0.0
    for i in range(len(values)):
        for j in range(len(values)):
            if i == j:
                continue
            de += n_c[i] * n_c[j] * delta2(i, j)
    de /= n * (n - 1)
    out["observed_disagreement"] = do
    out["expected_disagreement"] = de
    if de == 0:
        out["reason"] = "expected disagreement is zero; alpha is undefined"
        return out
    out["alpha"] = 1.0 - do / de
    out["computable"] = True
    return out


def krippendorff_alpha(matrix: Matrix, level: str = "ordinal") -> float:
    """Krippendorff's alpha over a units x raters matrix, ``None`` for missing.

    Returns NaN when undefined; ``krippendorff_alpha_detail`` says why.
    """
    return float(krippendorff_alpha_detail(matrix, level)["alpha"])


# --------------------------------------------------------------------------
# assembling a wave into matrices
# --------------------------------------------------------------------------

# How many candidate rater subsets the block search will hold before it gives
# up on being exhaustive. A wave at this study's design needs a few dozen, and
# a twelve-rater pool scoring five at a time needs about 1500; the cap exists
# because the closure below is exponential in the pool in the worst case, and
# this report is served from an HTTP endpoint that must not be made to sit on a
# pathological wave for a minute.
_BLOCK_SEARCH_MAX_CANDIDATES = 5000


def _largest_complete_block(rows_by_subject: Dict[str, Dict[str, Number]],
                            rater_ids: List[str]
                            ) -> Tuple[List[str], List[str], bool]:
    """Largest complete rater-by-subject block: (raters, subjects, exhaustive).

    ICC(2,1) models a fixed panel: the same k raters scored every subject. A
    wave assigned by ``raters.assign`` with a pool larger than
    ``per_encounter`` is NOT fully crossed -- encounter 1 may be rated by
    raters A, B, C and encounter 2 by B, C, D -- and listwise deletion over the
    whole pool can leave zero complete subjects, which would report the whole
    wave as uncomputable when there is plenty of agreement to measure. So the
    ICC is computed on a restriction, reported explicitly (``raters_used``,
    ``subjects_used``) because nobody should read an ICC without knowing what
    it was computed on.

    WHICH restriction is the whole problem. Maximise raters and you get the
    pool, which is complete on nothing; maximise subjects and you get a pair.
    The quantity worth maximising is the block's area, raters x subjects, and
    it is NOT reachable by improving one dimension at a time: with a pool of
    five rating three at a time, every subset of five and of four is complete
    on zero subjects, so a hill climb that only takes improving steps sits on
    that zero plateau and reports "not computable" over a wave with a complete
    2 x 11 block in it. Two steps have to be taken before anything improves.

    So enumerate instead, exactly, using the fact that bounds the search: if a
    set of raters R is complete on a set of subjects S, then so is the
    intersection of the rater sets of the subjects in S, which contains R and
    is therefore at least as good. Every optimal block's rater set is an
    intersection of subject rater-sets, so closing the observed rater-sets
    under intersection enumerates a superset of the optima -- a few dozen
    candidates for a real wave rather than 2**pool. Only blocks that ICC can
    actually be computed on (two raters, two subjects) are eligible; a wider
    block complete on one subject beats a 2 x 2 on area and is worth nothing.

    Returns the whole pool and its complete subjects when no eligible block
    exists at all, and a third value saying whether the enumeration ran to
    completion -- the caller may only tell a researcher that no block exists
    when the search actually looked everywhere. Deterministic: ties go to more
    subjects, then more raters, then the first rater ids alphabetically.

    Cost, stated because ``report`` calls this once per item and per construct
    behind an uncached endpoint, and because "a few dozen candidates" above is
    true of a ROTATED wave and not of every wave. A rotation gives the pool at
    most ``len(pool)`` distinct rater sets; an allocator free to pick panels
    gives nearly every encounter its own, and the closure then yields tens of
    thousands of candidates. Measured on this machine, one row of a 20-rater,
    600-encounter, 8-per-encounter wave with freely chosen panels: 597 distinct
    sets, ~16000 candidates, 0.12s per row against 0.70s before the candidate
    scoring moved onto rater-word intersections. Over the 22 items and four
    constructs that is ~3s of the report's ~5s, down from ~18s of ~21s. Do not
    read the improvement as making it free: ``GET /api/reliability`` computes
    the report on every request by deliberate choice (app.py, api_reliability:
    a stale reliability number is worse than a slow one), so a wave at those
    numbers still holds a worker for about five seconds. The study's own design
    -- pool of 5, 3 per encounter, 26 encounters -- is 0.02s for the whole
    report and is untouched either way, which is why the on-demand choice is
    sound as the study stands. Anyone growing the pool past twenty, or letting
    an allocator pick panels freely, should re-measure before assuming it
    still is.
    """
    raters = sorted(rater_ids)
    subjects = sorted(rows_by_subject)

    def complete_subjects(chosen: Sequence[str]) -> List[str]:
        return [s for s in subjects
                if all(rows_by_subject[s].get(r) is not None for r in chosen)]

    if len(raters) < 2:
        return raters, complete_subjects(raters), True

    bits = {r: 1 << i for i, r in enumerate(raters)}
    # Two views of the same table, built in one pass. ``counts`` maps a rater
    # set to how many subjects were scored by exactly that set, and drives the
    # closure below. ``scored_by`` is the transpose: one integer per rater, bit
    # j set when that rater scored subjects[j]. The second exists because the
    # scoring step needs, for every candidate panel, the number of subjects
    # ALL of its raters scored -- and reading that off ``counts`` means walking
    # every distinct rater set in the wave once per candidate. That is a
    # product of two large numbers on a wave whose panels were picked freely
    # rather than rotated: a 20-rater, 600-encounter wave has ~600 distinct
    # sets and the closure yields ~16000 candidates, so ~9.5 million mask
    # tests for ONE row and ~250 million for a report of 22 items and four
    # constructs -- 20s on /api/reliability, which nothing caches. Intersecting
    # the raters' words instead is a handful of machine-word operations per
    # candidate for the same answer.
    counts: Dict[int, int] = {}
    scored_by: Dict[int, int] = {bits[r]: 0 for r in raters}
    for j, s in enumerate(subjects):
        cell = rows_by_subject[s]
        mask = 0
        for r in raters:
            if cell.get(r) is not None:
                mask |= bits[r]
                scored_by[bits[r]] |= 1 << j
        counts[mask] = counts.get(mask, 0) + 1

    # Subjects scored by fewer than two raters can be in no complete block, and
    # intersections only ever shrink, so masks below two bits are dropped here
    # rather than propagated through the closure.
    seeds = sorted(m for m in counts if m.bit_count() >= 2)
    candidates = set(seeds)
    frontier = seeds
    exhaustive = True
    while frontier:
        found: List[int] = []
        for a in frontier:
            for b in seeds:
                c = a & b
                if c.bit_count() >= 2 and c not in candidates:
                    candidates.add(c)
                    found.append(c)
        # The cap is checked after a full round, never inside one: the first
        # round is what produces every pairwise intersection, and those are the
        # blocks a not-fully-crossed wave actually gets its ICC from. Stopping
        # mid-round could drop the winner and leave the answer dependent on
        # iteration order.
        if len(candidates) > _BLOCK_SEARCH_MAX_CANDIDATES:
            exhaustive = False
            break
        frontier = sorted(found)

    def n_scored_by_all(mask: int) -> int:
        """Subjects every rater in ``mask`` scored, as a popcount.

        Identical arithmetic to summing ``counts`` over every superset of
        ``mask``, because a subject is in both counts exactly when its own
        rater set contains ``mask``. Walking the set bits rather than all
        ``raters`` keeps this proportional to the size of the candidate panel
        and not to the size of the pool.
        """
        rest, word = mask, None
        while rest:
            low = rest & -rest
            word = scored_by[low] if word is None else word & scored_by[low]
            rest ^= low
        return 0 if word is None else word.bit_count()

    eligible = []
    for mask in candidates:
        n_sub = n_scored_by_all(mask)
        if n_sub < 2:
            continue
        names = [r for r in raters if mask & bits[r]]
        eligible.append((len(names) * n_sub, n_sub, len(names), names))
    if not eligible:
        return raters, complete_subjects(raters), exhaustive
    eligible.sort(key=lambda e: (-e[0], -e[1], -e[2], e[3]))
    chosen = eligible[0][3]
    return chosen, complete_subjects(chosen), exhaustive


def _matrix(rows_by_subject: Dict[str, Dict[str, Number]],
            subjects: List[str], raters: List[str]) -> List[List[Number]]:
    return [[rows_by_subject[s].get(r) for r in raters] for s in subjects]


def _mean(xs: Sequence[float]) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def _cell_block(label: str,
                rows_by_subject: Dict[str, Dict[str, Number]],
                rater_ids: List[str],
                *,
                level: str,
                na_counts: Tuple[int, int],
                kappa_scale: Optional[int]) -> Dict[str, Any]:
    """One row of the reliability report: an item, or a construct scale."""
    subjects = sorted(rows_by_subject)
    scored, na = na_counts
    block: Dict[str, Any] = {
        "label": label,
        "n_encounters": len(subjects),
        "n_raters": len(rater_ids),
        "n_scores": scored,
        "n_na": na,
        "na_rate": (na / (scored + na)) if (scored + na) else None,
        "mean": None,
        "icc": None,
        "krippendorff_alpha": None,
        "alpha_detail": None,
        "qwk": None,
    }
    all_values = [float(v) for s in subjects for v in rows_by_subject[s].values()
                  if v is not None]
    block["mean"] = _mean(all_values)

    used_raters, used_subjects, searched_all = _largest_complete_block(
        rows_by_subject, rater_ids)
    if len(used_raters) >= 2 and len(used_subjects) >= 2:
        icc_result = icc(_matrix(rows_by_subject, used_subjects, used_raters))
        n_used = len(used_subjects)
    else:
        # No block worth restricting to. Handing icc() the empty restriction
        # would make it describe the restriction -- zero rows, therefore zero
        # raters -- and a reader takes "fewer than two raters with any data" as
        # a statement about the wave and goes looking for ratings that are not
        # missing. Hand it the whole table instead, so its counts and its
        # reason are about the data the researcher actually has.
        #
        # Read the honest count BEFORE the next line widens the restriction.
        # Up to here ``used_subjects`` is still the encounters every rater in
        # the named panel scored; after it, it is the whole table.
        n_named_panel_scored = len(used_subjects)
        used_raters, used_subjects = sorted(rater_ids), subjects
        icc_result = icc(_matrix(rows_by_subject, used_subjects, used_raters))
        # ``subjects_used`` means the encounters the named panel ALL scored --
        # that is what it means in the branch above and what the report's
        # not-fully-crossed warning promises a reader it means. Two other
        # numbers are close to hand here and both of them are false.
        #
        # The table's own height would print "all 6 raters, 15 of 15
        # encounters, not computable", which reads as a wave that had every
        # rating and failed anyway, and sends the reader looking for a data
        # problem that is not there. And ``icc_result["n"]`` is the rows that
        # survived listwise deletion of the matrix icc() KEPT -- icc() drops
        # rater columns that are entirely empty before it counts, so on the
        # systematically-N/A item this module is built to expect (items 3 and
        # 49 in S2's dyadic setting) the count that comes back is the count
        # belonging to whichever rater did score it. Published beside the
        # two-name panel that is still printed in ``raters_used``, that is
        # "rt_a and rt_b, 5 of 5 encounters" for a row those two never once
        # scored together: a confident false statement about the data, which
        # is the one thing this module exists to refuse.
        n_used = n_named_panel_scored
        if (searched_all and len(used_raters) >= 2
                and not icc_result["computable"] and icc_result["reason"]):
            icc_result["reason"] += (
                ". No sub-panel rescues it either: no two of these raters both "
                "scored two or more of the same encounters, so this row has no "
                "complete rater-by-encounter block of any size. Krippendorff's "
                "alpha uses every rating and is the number to read here"
            )
    icc_result["raters_used"] = used_raters
    icc_result["subjects_used"] = n_used
    icc_result["subjects_available"] = len(subjects)
    # Carried per row because the cap trips per row: ``report``'s
    # not-fully-crossed warning tells the reader the ICCs were computed on the
    # largest block that EXISTS, and that is a guarantee only a search which
    # ran to completion actually gave. Drop this and the warning states a
    # guarantee the search did not make.
    icc_result["block_search_exhaustive"] = searched_all
    block["icc"] = icc_result

    alpha = krippendorff_alpha_detail(
        _matrix(rows_by_subject, subjects, rater_ids), level)
    # NaN is the right answer from the float-returning estimators, and the
    # wrong thing to put in a report: this dict is serialised straight out of
    # GET /api/reliability, json.dumps writes a bare NaN token, and a browser's
    # JSON.parse (and jq, and anything else that follows the grammar) rejects
    # the whole document over one undefined item. Undefined becomes null here.
    if isinstance(alpha["alpha"], float) and math.isnan(alpha["alpha"]):
        alpha = dict(alpha, alpha=None)
    block["alpha_detail"] = alpha
    block["krippendorff_alpha"] = alpha["alpha"] if alpha["computable"] else None

    if kappa_scale is None:
        block["qwk"] = {
            "mean": None,
            "pairs": [],
            "reason": (
                "quadratic weighted kappa is defined on the 1..5 ordinal "
                "categories; a construct scale is a mean over items and no "
                "longer lands on them. Read the item rows for kappa, and this "
                "row's ICC and alpha for the scale."
            ),
        }
    else:
        pairs = []
        for i in range(len(rater_ids)):
            for j in range(i + 1, len(rater_ids)):
                ra, rb = rater_ids[i], rater_ids[j]
                a = [rows_by_subject[s].get(ra) for s in subjects]
                b = [rows_by_subject[s].get(rb) for s in subjects]
                d = quadratic_weighted_kappa_detail(a, b, kappa_scale)
                pairs.append({
                    "raters": [ra, rb], "n": d["n"],
                    "kappa": d["kappa"] if d["computable"] else None,
                    "reason": d["reason"],
                })
        usable = [p["kappa"] for p in pairs if p["kappa"] is not None]
        block["qwk"] = {
            "mean": _mean(usable),
            "n_pairs": len(pairs),
            "n_pairs_computable": len(usable),
            "pairs": pairs,
            "reason": None if usable else "no rater pair shares two or more encounters",
        }
    return block


def _load_ratings(cohort: Optional[str],
                  ratings: Optional[Iterable[dict]]) -> Tuple[List[dict], str]:
    if ratings is not None:
        # Injected ratings are taken as already scoped to the cohort the caller
        # named; this path exists for tests and for reporting over an ingested
        # Qualtrics export that never went through the store.
        return list(ratings), "injected"
    try:
        from . import ratings as ratings_mod
        return list(ratings_mod.all_ratings(cohort)), "server.ratings"
    except Exception as exc:  # pragma: no cover -- depends on sibling module
        return [], f"unavailable ({exc.__class__.__name__}: {exc})"


def _load_items(items: Optional[Iterable[dict]]) -> Tuple[List[dict], str]:
    if items is not None:
        return list(items), "injected"
    try:
        from . import esci
        return list(esci.all_items()), "server.esci"
    except Exception as exc:  # pragma: no cover -- depends on sibling module
        return [], f"unavailable ({exc.__class__.__name__}: {exc})"


def _reverse_coded(item: dict, raw: Number) -> Number:
    """Apply reverse scoring: 6 - raw on a 1..5 scale, for flagged items.

    Deliberately the same rule as ``esci.score_value`` rather than a call to
    it, because ``report`` must also run over an injected item bank -- a pilot
    subset, or the item map from a Qualtrics export -- whose ids esci does not
    carry. Items 11, 15 and 24 are the reverse-keyed ones (rating-instrument.md
    administration note 2); they are presented to raters as written and coded
    here, at analysis, which is the only place it may happen.
    """
    if raw is None:
        return None
    return (SCALE_MIN + SCALE_MAX - float(raw)) if item.get("reverse") else float(raw)


def report(cohort: Optional[str] = None, *,
           ratings: Optional[Iterable[dict]] = None,
           items: Optional[Iterable[dict]] = None) -> Dict[str, Any]:
    """Reliability across a whole wave, per construct and per item.

    Every row states its n, its rater count and how much data was missing,
    because a reliability coefficient without those three is unreadable: ICC =
    0.81 on four encounters and ICC = 0.81 on twenty-six are different facts,
    and an item every rater marked N/A has no reliability at all rather than
    poor reliability.

    Construct rows score the competency scale -- the mean of that construct's
    reverse-coded items that the rater actually answered, per encounter per
    rater, which is the "Competency scores" definition in the rating
    instrument's scoring plan, with N/A excluded pairwise per administration
    note 3. They are analysed at interval level, since a mean over 5 or 6 items
    no longer sits on the five ordinal rungs. Item rows are analysed at ordinal
    level and additionally carry pairwise quadratic weighted kappa.

    ``ratings`` and ``items`` override the module sources; leave them unset in
    production, where the sources are ``server.ratings`` and ``server.esci``.

    Cost is dominated by the per-row block search and is worth knowing before
    this is wired to anything impatient: the study's own design is 0.02s, and a
    20-rater, 600-encounter wave whose panels were picked freely rather than
    rotated is ~5s (~21s before the search was reworked). ``GET
    /api/reliability`` computes this on every request, uncached, on purpose.
    ``_largest_complete_block`` carries the measurements.
    """
    rating_rows, rating_source = _load_ratings(cohort, ratings)
    item_rows, item_source = _load_items(items)

    out: Dict[str, Any] = {
        "cohort": cohort,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # The notice travels with the items. This report names all 22 of them.
        "item_source_notice": ITEM_SOURCE_NOTICE,
        "sources": {"ratings": rating_source, "items": item_source},
        "n_ratings": len(rating_rows),
        "n_encounters": 0,
        "n_raters": 0,
        "design": {},
        "missing": {},
        "constructs": {},
        "items": {},
        "warnings": [],
    }
    warnings: List[str] = out["warnings"]
    if not item_rows:
        warnings.append(
            f"no item bank: items source is {item_source}. Nothing can be "
            "scored without it."
        )
        return out
    if not rating_rows:
        warnings.append(
            f"no ratings: ratings source is {rating_source}"
            + (f" and no submitted rating matches cohort {cohort!r}"
               if rating_source != "injected" and cohort else "")
            + ". Reliability cannot be computed before rating begins."
        )
        return out

    by_id = {i["id"]: i for i in item_rows}
    constructs: List[str] = []
    for i in item_rows:
        if i["construct"] not in constructs:
            constructs.append(i["construct"])

    # subject -> rater -> value, one map per item and one per construct scale.
    item_cells: Dict[str, Dict[str, Dict[str, Number]]] = {
        i["id"]: {} for i in item_rows}
    construct_cells: Dict[str, Dict[str, Dict[str, Number]]] = {
        c: {} for c in constructs}
    item_na: Dict[str, List[int]] = {i["id"]: [0, 0] for i in item_rows}
    construct_na: Dict[str, List[int]] = {c: [0, 0] for c in constructs}

    encounters: List[str] = []
    raters: List[str] = []
    raters_per_encounter: Dict[str, set] = {}
    unknown_items: set = set()
    duplicates: List[str] = []
    seen: set = set()

    for row in rating_rows:
        sid = row.get("session_id") or row.get("encounter_id")
        rid = row.get("rater_id")
        if not sid or not rid:
            warnings.append(f"skipped a rating with no session_id/rater_id: {row!r}")
            continue
        if (sid, rid) in seen:
            # Two submissions from one rater for one encounter. The last one
            # wins (that is what a resubmission means), but it is said out loud
            # because it silently halves nothing and doubles nothing -- it
            # overwrites, and an analyst counting rows will not see it.
            duplicates.append(f"{rid}@{sid}")
        seen.add((sid, rid))
        if sid not in encounters:
            encounters.append(sid)
        if rid not in raters:
            raters.append(rid)
        raters_per_encounter.setdefault(sid, set()).add(rid)

        scores = row.get("scores") or {}
        per_construct: Dict[str, List[float]] = {}
        for item_id, item in by_id.items():
            if item_id not in scores:
                continue
            raw = scores[item_id]
            coded = _reverse_coded(item, raw)
            counter = item_na[item_id]
            if coded is None:
                counter[1] += 1
            else:
                counter[0] += 1
                per_construct.setdefault(item["construct"], []).append(coded)
            item_cells[item_id].setdefault(sid, {})[rid] = coded
        for unknown in set(scores) - set(by_id):
            unknown_items.add(unknown)
        for c in constructs:
            vals = per_construct.get(c, [])
            scale = _mean(vals)
            construct_cells[c].setdefault(sid, {})[rid] = scale
            construct_na[c][0 if scale is not None else 1] += 1

    out["n_encounters"] = len(encounters)
    out["n_raters"] = len(raters)

    counts = sorted(len(v) for v in raters_per_encounter.values())
    fully_crossed = bool(counts) and all(c == len(raters) for c in counts)
    thin = sorted(s for s, v in raters_per_encounter.items() if len(v) < 2)
    out["design"] = {
        "fully_crossed": fully_crossed,
        "raters_per_encounter": {
            "min": counts[0] if counts else 0,
            "max": counts[-1] if counts else 0,
            "mean": _mean([float(c) for c in counts]),
        },
        "encounters_with_fewer_than_2_raters": thin,
        "duplicate_submissions": sorted(set(duplicates)),
    }

    total_scored = sum(v[0] for v in item_na.values())
    total_na = sum(v[1] for v in item_na.values())
    expected_cells = len(encounters) * len(raters) * len(by_id)
    out["missing"] = {
        "item_cells_scored": total_scored,
        "item_cells_na": total_na,
        "na_rate": (total_na / (total_scored + total_na)) if (total_scored + total_na) else None,
        "item_cells_expected_if_fully_crossed": expected_cells,
        "item_cells_never_assigned": max(0, expected_cells - total_scored - total_na),
    }

    for c in constructs:
        block = _cell_block(
            c, construct_cells[c], raters,
            level="interval",
            na_counts=(construct_na[c][0], construct_na[c][1]),
            kappa_scale=None,
        )
        # A construct row's own na_rate is the rate at which the *scale* could
        # not be formed, which is near zero by construction: it takes one
        # answered item out of five or six to produce a mean. That is the right
        # number for this row but it is not the missingness a researcher is
        # looking for, and printed in the same column as the item rows' N/A it
        # would read as "no data was missing". So carry the underlying item
        # missingness too, and let the table show that one.
        scored = sum(item_na[i["id"]][0] for i in item_rows if i["construct"] == c)
        na = sum(item_na[i["id"]][1] for i in item_rows if i["construct"] == c)
        block["item_cells_scored"] = scored
        block["item_cells_na"] = na
        block["item_na_rate"] = (na / (scored + na)) if (scored + na) else None
        out["constructs"][c] = block
    for item in item_rows:
        iid = item["id"]
        block = _cell_block(
            item.get("text", iid), item_cells[iid], raters,
            level="ordinal",
            na_counts=(item_na[iid][0], item_na[iid][1]),
            kappa_scale=SCALE_MAX,
        )
        block.update({
            "item_id": iid,
            "number": item.get("number"),
            "construct": item.get("construct"),
            "reverse": bool(item.get("reverse")),
        })
        out["items"][iid] = block

    # --- warnings a researcher must not have to derive from the tables ---
    if len(raters) < 2:
        warnings.append(
            f"only {len(raters)} rater(s) in this wave; no agreement statistic "
            "is defined. The design calls for k >= 3."
        )
    if not fully_crossed and len(raters) >= 2:
        # "each ICC below was computed on the largest complete block" is not
        # true of a row that has no complete block at all -- a pool where no
        # two raters share two encounters computes nothing, and a blanket
        # promise here would have the reader hunting the tables for a block
        # that was never found. The promise is conditional and the row's own
        # reason string carries the other case.
        #
        # "that EXISTS in its row" is the second half of the same promise, and
        # it is a claim about the search rather than about the data: the
        # enumeration is exact only while it stays under its candidate cap, and
        # a capped search returns the largest block it FOUND. This is reachable
        # at the shipped cap, not only in a test that lowers it -- 14 raters,
        # 56 encounters, each scored by all but one rater (a leave-one-out
        # rotation) closes to 2**14 candidate panels, and the search returns
        # 8 raters x 24 encounters = 192 against the true optimum of
        # 7 x 28 = 196. Close enough to be useful, not close enough to call it
        # the largest that exists. A statistics module that overstates its own
        # guarantee has made the same species of false statement as a row
        # reporting encounters nobody scored, so the guarantee is withdrawn
        # wherever any row's search stopped early.
        searched_every_row = all(
            block["icc"]["block_search_exhaustive"]
            for block in list(out["constructs"].values())
            + list(out["items"].values())
        )
        block_clause = (
            "the largest complete rater-by-encounter block that exists in its "
            "row, and reports in raters_used/subjects_used which raters and "
            "how many encounters that was; where no such block exists the ICC "
            "is not computable and the row says why"
            if searched_every_row else
            "the largest complete rater-by-encounter block the search found in "
            "its row -- on at least one row the candidate enumeration hit its "
            "cap, so that block is the largest found and not provably the "
            "largest that exists, and a larger one may be there; on sampled "
            "capped designs the shortfall ran from 13% to more than 20% of "
            "the block's area, one of them nearly doubling the encounters "
            "available -- "
            "and reports in raters_used/subjects_used which raters and how "
            "many encounters that was; where none was found the ICC is not "
            "computable and the row says why"
        )
        warnings.append(
            "raters are not fully crossed with encounters (raters per "
            f"encounter {counts[0]}..{counts[-1]} out of a pool of "
            f"{len(raters)}). ICC(2,1)/ICC(2,k) assume one fixed panel scored "
            f"every encounter, so each ICC below was computed on {block_clause}"
            ". Krippendorff's alpha uses all of the data and is the number to "
            "read when the two disagree."
        )
    if thin:
        warnings.append(
            f"{len(thin)} encounter(s) carry fewer than two ratings and "
            "contribute nothing to agreement: " + ", ".join(thin[:10])
            + (" ..." if len(thin) > 10 else "")
        )
    if duplicates:
        warnings.append(
            "duplicate submissions (one rater, one encounter, more than once); "
            "the last submission was used: " + ", ".join(sorted(set(duplicates))[:10])
        )
    if unknown_items:
        warnings.append(
            "ratings carried item ids that are not in the item bank and were "
            "ignored: " + ", ".join(sorted(unknown_items)[:10])
        )
    for iid, block in out["items"].items():
        rate = block["na_rate"]
        if rate is not None and rate >= 0.5:
            # The instrument predicts exactly this for items 3 and 49 in S2's
            # dyadic setting and calls it informative rather than broken -- so
            # it is a warning, not a failure.
            warnings.append(
                f"{iid} was marked 'not enough information to judge' on "
                f"{rate:.0%} of ratings; a systematically N/A cell is "
                "informative (rating-instrument.md, administration note 3) "
                "but has little or no reliability to report."
            )
    for name, block in out["constructs"].items():
        if not block["icc"]["computable"]:
            warnings.append(
                f"construct {name}: ICC not computable -- {block['icc']['reason']}"
            )
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _fmt(x: Optional[float], places: int = 3) -> str:
    if x is None:
        return "  -  "
    if isinstance(x, float) and math.isnan(x):
        return "  -  "
    return f"{x:.{places}f}"


def _print_report(rep: Dict[str, Any]) -> None:
    print(f"\nReliability -- cohort {rep['cohort'] or 'all'} -- {rep['generated_at']}")
    print(f"sources: ratings={rep['sources']['ratings']} items={rep['sources']['items']}")
    print(f"{rep['n_ratings']} rating(s) · {rep['n_encounters']} encounter(s) "
          f"· {rep['n_raters']} rater(s)")
    design = rep.get("design") or {}
    if design:
        rpe = design["raters_per_encounter"]
        print(f"design: {'fully crossed' if design['fully_crossed'] else 'NOT fully crossed'}"
              f" · raters per encounter {rpe['min']}-{rpe['max']} "
              f"(mean {_fmt(rpe['mean'], 2)})")
    missing = rep.get("missing") or {}
    if missing:
        print(f"missing: {missing['item_cells_na']} N/A of "
              f"{missing['item_cells_na'] + missing['item_cells_scored']} item cells "
              f"({_fmt(missing['na_rate'], 3)})")

    if rep["constructs"]:
        print("\nPer construct (competency scale, interval alpha)")
        print(f"  {'construct':28} {'n':>3} {'k':>2} {'ICC(2,1)':>9} {'ICC(2,k)':>9} "
              f"{'alpha':>7} {'item N/A':>9}")
        for name, b in rep["constructs"].items():
            ic = b["icc"]
            print(f"  {name:28} {b['n_encounters']:>3} {b['n_raters']:>2} "
                  f"{_fmt(ic['icc_2_1']):>9} {_fmt(ic['icc_2_k']):>9} "
                  f"{_fmt(b['krippendorff_alpha']):>7} "
                  f"{_fmt(b.get('item_na_rate'), 2):>9}")

    if rep["items"]:
        # The notice sits directly above the table that names the items, not in
        # a footer nobody scrolls to.
        print(f"\nPer item -- {rep['item_source_notice']}")
        print(f"  {'item':10} {'construct':24} {'n':>3} {'ICC(2,1)':>9} {'ICC(2,k)':>9} "
              f"{'alpha':>7} {'QWK':>7} {'N/A':>6}  text")
        for iid, b in rep["items"].items():
            ic = b["icc"]
            rev = " (R)" if b["reverse"] else ""
            print(f"  {iid:10} {str(b['construct']):24} {b['n_encounters']:>3} "
                  f"{_fmt(ic['icc_2_1']):>9} {_fmt(ic['icc_2_k']):>9} "
                  f"{_fmt(b['krippendorff_alpha']):>7} "
                  f"{_fmt((b['qwk'] or {}).get('mean')):>7} "
                  f"{_fmt(b['na_rate'], 2):>6}  {b['label']}{rev}")

    if rep["warnings"]:
        print("\nWarnings")
        for w in rep["warnings"]:
            print(f"  ! {w}")
    print()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server.reliability",
        description=("Inter-rater reliability over the submitted Phase 2 "
                     "ratings: ICC(2,1)/ICC(2,k), Krippendorff's alpha and "
                     "pairwise quadratic weighted kappa, per construct and "
                     "per item."),
    )
    parser.add_argument("--cohort", default=None,
                        help="restrict to one cohort: study, internal, unattributed")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON instead of a table")
    args = parser.parse_args(argv)

    rep = report(args.cohort)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        _print_report(rep)
    # Exit non-zero when there was nothing to report, so a nightly job that
    # expects a wave to be rateable notices that it is not.
    return 0 if rep["items"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
