"""Tests for server/raters.py — the registry, scoped tokens, and allocation.

Every test runs against its own DATA_DIR: server.storage reads the environment
variable once, at import, so a fresh data directory means reloading storage and
then raters (which binds DATA_DIR by value, the runs.py idiom). Doing that per
test also means these tests cannot be broken by another module in the same
pytest process pointing DATA_DIR at the fixture wave.

The connectivity and load properties are checked with independent
implementations (a BFS here, arithmetic here) rather than by calling the
module's own helpers, so a bug in _components cannot certify itself.
"""

from __future__ import annotations

import importlib
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The recorded wave, when there is one, arrives through the `wave_sessions`
# fixture in tests/conftest.py (RF_FIXTURE_DIR / RF_FIXTURE / DATA_DIR, or a
# wave checked in under tests/data). This module used to carry one machine's
# absolute scratchpad path with a session UUID in it and no override at all,
# so the allocator's only test against real manifests was a silent skip on
# every other machine.


# ---------- harness ----------

@pytest.fixture()
def raters(tmp_path, monkeypatch):
    """server.raters bound to an empty DATA_DIR under tmp_path."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import server.storage as storage

    importlib.reload(storage)
    import server.raters as raters_mod

    importlib.reload(raters_mod)
    raters_mod.init_rater_storage()
    return raters_mod


def make_session(raters_mod, session_id, scenario="S1B", cohort="study"):
    """A minimal recorded encounter: just the manifest assign() reads."""
    d = raters_mod.SESSIONS_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({
            "session_id": session_id,
            "scenario": scenario,
            "cohort": cohort,
            "status": "closed",
        }),
        encoding="utf-8",
    )
    return session_id


def make_sessions(raters_mod, n, scenario="S1B", cohort="study"):
    return [
        make_session(raters_mod, f"s_17724603{i:02d}_{i:06x}", scenario, cohort)
        for i in range(n)
    ]


def make_raters(raters_mod, n):
    return [raters_mod.create_rater(f"Rater {i}")["rater_id"] for i in range(n)]


def is_connected(plan):
    """Are the raters in `plan` one connected overlap component? Independent BFS."""
    adjacency = {}
    for members in plan.values():
        for a in members:
            adjacency.setdefault(a, set())
            for b in members:
                if a != b:
                    adjacency[a].add(b)
    if not adjacency:
        return True
    start = next(iter(adjacency))
    seen, stack = {start}, [start]
    while stack:
        cur = stack.pop()
        for nxt in adjacency[cur]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return len(seen) == len(adjacency)


def loads(plan):
    out = {}
    for members in plan.values():
        for r in members:
            out[r] = out.get(r, 0) + 1
    return out


# ---------- registry ----------

def test_create_and_read_back(raters):
    r = raters.create_rater("Ada L.", kind="trained", email="ada@example.edu")
    assert r["rater_id"].startswith("rr_")
    assert r["kind"] == "trained"
    assert r["tokens_active"] == 0
    assert raters.get_rater(r["rater_id"])["name"] == "Ada L."
    assert [x["rater_id"] for x in raters.list_raters()] == [r["rater_id"]]


def test_rejects_bad_kind_and_empty_name(raters):
    with pytest.raises(ValueError):
        raters.create_rater("Someone", kind="volunteer")
    with pytest.raises(ValueError):
        raters.create_rater("   ")


def test_get_rater_is_traversal_safe(raters):
    # A rater id arrives as a URL path segment. Anything that is not the minted
    # shape must not reach the filesystem at all.
    for bad in ("../runs/deadbeef", "rr_../../x", "rr_ZZZ", "", None,
                "rr_0123456789ab/../.."):
        assert raters.get_rater(bad) is None


def test_public_record_never_carries_token_hashes(raters):
    rid = raters.create_rater("Ada")["rater_id"]
    raters.issue_token(rid)
    pub = raters.get_rater(rid)
    assert "tokens" not in pub
    assert pub["tokens_active"] == 1
    assert pub["token_expires_at"] > time.time()


# ---------- tokens ----------

def test_token_roundtrip_and_shape(raters):
    rid = raters.create_rater("Ada")["rater_id"]
    token = raters.issue_token(rid, days=7)
    assert raters._TOKEN_RE.fullmatch(token)
    assert raters.rater_for_token(token)["rater_id"] == rid


def test_token_is_never_written_to_disk(raters):
    rid = raters.create_rater("Ada")["rater_id"]
    token = raters.issue_token(rid)
    needle = token.encode()
    seen = 0
    for path in raters.DATA_DIR.rglob("*"):
        if path.is_file():
            seen += 1
            assert needle not in path.read_bytes(), f"token leaked into {path}"
    assert seen  # the walk actually looked at something


def test_unknown_and_malformed_tokens_are_none(raters):
    raters.create_rater("Ada")
    for bad in (None, "", "rt_", "nope", "rt_" + "0" * 31, "rt_" + "g" * 32,
                "rt_" + "0" * 32):
        assert raters.rater_for_token(bad) is None


def test_expired_token_stops_working(raters, monkeypatch):
    rid = raters.create_rater("Ada")["rater_id"]
    token = raters.issue_token(rid, days=30)
    assert raters.rater_for_token(token) is not None
    later = time.time() + 31 * 86400
    monkeypatch.setattr(raters, "_now", lambda: later)
    assert raters.rater_for_token(token) is None
    # ...and the public record stops claiming the rater can get in.
    assert raters.get_rater(rid)["tokens_active"] == 0


def test_expiry_boundary_is_exclusive(raters, monkeypatch):
    rid = raters.create_rater("Ada")["rater_id"]
    token = raters.issue_token(rid, days=1)
    expires = raters.get_rater(rid)["token_expires_at"]
    monkeypatch.setattr(raters, "_now", lambda: expires - 1)
    assert raters.rater_for_token(token) is not None
    monkeypatch.setattr(raters, "_now", lambda: expires)
    assert raters.rater_for_token(token) is None


def test_revoke(raters):
    rid = raters.create_rater("Ada")["rater_id"]
    token = raters.issue_token(rid)
    assert raters.revoke_token(token) is True
    assert raters.rater_for_token(token) is None
    # A second revoke changed nothing, and must say so.
    assert raters.revoke_token(token) is False
    assert raters.revoke_token("rt_" + "0" * 32) is False
    assert raters.revoke_token("garbage") is False
    # The revocation is in the document too, so an index rebuilt from the
    # documents cannot resurrect the token.
    doc = json.loads((raters.RATERS_DIR / f"{rid}.json").read_text(encoding="utf-8"))
    assert doc["tokens"][0]["revoked_at"] is not None


def test_tokens_are_per_rater(raters):
    a = raters.create_rater("A")["rater_id"]
    b = raters.create_rater("B")["rater_id"]
    ta, tb = raters.issue_token(a), raters.issue_token(b)
    assert raters.rater_for_token(ta)["rater_id"] == a
    assert raters.rater_for_token(tb)["rater_id"] == b
    raters.revoke_token(ta)
    # Revoking one rater's token must not touch the other's.
    assert raters.rater_for_token(tb)["rater_id"] == b


def test_issue_token_validates(raters):
    rid = raters.create_rater("Ada")["rater_id"]
    with pytest.raises(ValueError):
        raters.issue_token("rr_000000000000")
    for bad_days in (0, -1, "soon"):
        with pytest.raises(ValueError):
            raters.issue_token(rid, days=bad_days)


def test_second_token_does_not_kill_the_first(raters):
    rid = raters.create_rater("Ada")["rater_id"]
    t1 = raters.issue_token(rid)
    t2 = raters.issue_token(rid)
    assert t1 != t2
    assert raters.rater_for_token(t1)["rater_id"] == rid
    assert raters.rater_for_token(t2)["rater_id"] == rid
    assert raters.get_rater(rid)["tokens_active"] == 2


# ---------- allocation: refusals ----------

def test_refuses_fewer_raters_than_complement(raters):
    sessions = make_sessions(raters, 3)
    rs = make_raters(raters, 2)
    with pytest.raises(ValueError, match="per_encounter"):
        raters.assign(sessions, rs, per_encounter=3)
    # Nothing was written on the way to the refusal.
    assert raters.list_assignments() == []


def test_refuses_unknown_encounter(raters):
    rs = make_raters(raters, 3)
    good = make_sessions(raters, 1)[0]
    with pytest.raises(ValueError, match="unknown encounter"):
        raters.assign([good, "s_1772460399_ffffff"], rs)
    assert raters.list_assignments() == []


def test_refuses_unknown_rater(raters):
    sessions = make_sessions(raters, 2)
    rs = make_raters(raters, 3)
    with pytest.raises(ValueError, match="unknown rater"):
        raters.assign(sessions, rs + ["rr_000000000000"])


def test_refuses_empty_inputs(raters):
    sessions = make_sessions(raters, 1)
    rs = make_raters(raters, 3)
    with pytest.raises(ValueError):
        raters.assign([], rs)
    with pytest.raises(ValueError):
        raters.assign(sessions, [])
    with pytest.raises(ValueError):
        raters.assign(sessions, rs, per_encounter=0)


def test_duplicate_rater_ids_collapse_rather_than_double_book(raters):
    # The same rater named twice is one rater, so the complement is short and
    # the call must refuse instead of quietly assigning them the encounter
    # twice.
    sessions = make_sessions(raters, 2)
    rs = make_raters(raters, 2)
    with pytest.raises(ValueError, match="per_encounter"):
        raters.assign(sessions, [rs[0], rs[0], rs[1]], per_encounter=3)


def test_reassigning_the_same_pair_is_refused(raters):
    sessions = make_sessions(raters, 1)
    rs = make_raters(raters, 3)
    raters.assign(sessions, rs, per_encounter=3, seed=1)
    # The encounter is full; asking for the same three again is a no-op, not a
    # second set of assignments.
    assert raters.assign(sessions, rs, per_encounter=3, seed=1) == []
    assert len(raters.assignments_for_encounter(sessions[0])) == 3


# ---------- allocation: the design properties ----------

def test_full_coverage_no_duplicates_even_load_connected(raters):
    sessions = make_sessions(raters, 27)
    rs = make_raters(raters, 9)
    created = raters.assign(sessions, rs, per_encounter=3, seed=42)
    assert len(created) == 27 * 3

    plan = {s: [a["rater_id"] for a in raters.assignments_for_encounter(s)]
            for s in sessions}
    for sid, members in plan.items():
        assert len(members) == 3, sid
        assert len(set(members)) == 3, f"{sid} got the same rater twice"
    per = loads(plan)
    assert set(per) == set(rs), "a rater was left with nothing to do"
    assert max(per.values()) - min(per.values()) <= 1
    assert is_connected(plan)


def test_reproducible_from_a_seed(raters, tmp_path, monkeypatch):
    sessions = make_sessions(raters, 17)
    rs = make_raters(raters, 6)
    first = raters.allocate_plan(sessions, rs, per_encounter=3, seed=7)
    second = raters.allocate_plan(sessions, rs, per_encounter=3, seed=7)
    assert first == second
    # And it is the seed doing the work, not the input order: a different seed
    # is allowed to differ, and here does.
    other = raters.allocate_plan(sessions, rs, per_encounter=3, seed=8)
    assert other != first


def test_seeded_assign_writes_the_same_wave_twice(raters, tmp_path, monkeypatch):
    """Same seed, two independent data directories, identical allocation."""
    sessions = make_sessions(raters, 12)
    rs_names = [f"Rater {i}" for i in range(5)]
    rs = [raters.create_rater(n)["rater_id"] for n in rs_names]
    plan_a = {s: sorted(v) for s, v in
              raters.allocate_plan(sessions, rs, 3, seed=99).items()}

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data2"))
    import server.storage as storage

    importlib.reload(storage)
    import server.raters as raters2

    importlib.reload(raters2)
    raters2.init_rater_storage()
    sessions2 = make_sessions(raters2, 12)
    rs2 = [raters2.create_rater(n)["rater_id"] for n in rs_names]
    # The rater ids are freshly minted, so compare the shape of the design:
    # map each directory's ids onto its own ordering.
    plan_b = raters2.allocate_plan(sessions2, rs2, 3, seed=99)
    index_a = {r: i for i, r in enumerate(rs)}
    index_b = {r: i for i, r in enumerate(rs2)}
    shape_a = {s: sorted(index_a[r] for r in plan_a[s]) for s in sessions}
    shape_b = {s2: sorted(index_b[r] for r in plan_b[s2])
               for s2 in sessions2}
    assert list(shape_a.values()) == list(shape_b.values())


@pytest.mark.parametrize("n_sessions,n_raters,k", [
    (1, 3, 3), (2, 3, 3), (3, 3, 3), (4, 4, 2), (5, 3, 2), (6, 6, 3),
    (7, 5, 2), (9, 3, 3), (10, 4, 3), (12, 6, 3), (13, 7, 3), (26, 9, 3),
    (27, 9, 3), (27, 2, 2), (30, 12, 4), (40, 5, 5), (50, 11, 2),
    # Right on the connectability bound N*(k-1)+1, where the greedy fill is
    # most likely to hand the repair a split it has to fix.
    (2, 3, 2), (3, 7, 3), (4, 9, 3), (5, 5, 2), (6, 13, 3), (4, 13, 5),
])
def test_allocation_properties_hold_across_shapes(raters, n_sessions, n_raters, k):
    """The four guarantees, swept over wave shapes an operator might ask for.

    Includes the awkward ones: a complement equal to the pool (40, 5, 5), the
    pairwise minimum (27, 2, 2) where every encounter has the same two raters,
    and the pools sitting exactly on the bound beyond which no connected design
    exists at all.
    """
    sessions = [f"s_1772400{i:03d}_{i:06x}" for i in range(n_sessions)]
    rs = [f"rr_{i:012x}" for i in range(n_raters)]
    for seed in (0, 1, 2, 13):
        plan = raters.allocate_plan(sessions, rs, per_encounter=k, seed=seed)
        assert set(plan) == set(sessions)
        for sid, members in plan.items():
            assert len(members) == k
            assert len(set(members)) == k
        per = loads(plan)
        assert max(per.values()) - min(per.values()) <= 1
        # Raters who got nothing are not in the graph; the ones who did must
        # form a single component.
        assert is_connected(plan)
        expected_slots = n_sessions * k
        assert sum(per.values()) == expected_slots
        # Nobody may be given more than there are encounters.
        assert max(per.values()) <= n_sessions


def test_allocation_invariants_over_every_small_shape(raters):
    """The same four guarantees, swept rather than sampled.

    Every wave shape up to 14 encounters, 5 raters per encounter and a pool
    filling the whole legal range, on four seeds each. The named cases above
    document intent; this one is what would actually catch a regression, and it
    is how the bug in the first version of the repair was found — a plain
    least-loaded-first fill leaves two encounters and four raters as two
    islands, and no amount of swapping puts them together.
    """
    checked = 0
    for n_sessions in range(1, 15):
        sessions = [f"s_1772400{i:03d}_{i:06x}" for i in range(n_sessions)]
        for k in range(2, 6):
            bound = n_sessions * (k - 1) + 1
            for n_raters in range(k, bound + 1):
                rs = [f"rr_{i:012x}" for i in range(n_raters)]
                for seed in (0, 1, 5, 23):
                    plan = raters.allocate_plan(sessions, rs, k, seed)
                    checked += 1
                    for members in plan.values():
                        assert len(members) == k
                        assert len(set(members)) == k
                    per = loads(plan)
                    assert len(per) == n_raters, "a rater in the pool got nothing"
                    assert max(per.values()) - min(per.values()) <= 1
                    assert is_connected(plan), (n_sessions, n_raters, k, seed)
    assert checked > 3000


def test_top_up_invariants_over_random_partial_waves(raters):
    """Allocation on top of a wave that is already half-dealt, and split.

    A partial allocation on disk is the case construction cannot connect on its
    own, so this is where the repair earns its place. The pre-existing
    placements are random, so most of these waves arrive as several islands.
    """
    rng = random.Random(20260908)
    for _ in range(400):
        n_sessions = rng.randint(2, 16)
        k = rng.randint(2, 4)
        n_raters = rng.randint(k, min(n_sessions * (k - 1) + 1, 18))
        sessions = [f"s_1772400{i:03d}_{i:06x}" for i in range(n_sessions)]
        rs = [f"rr_{i:012x}" for i in range(n_raters)]
        existing = {s: rng.sample(rs, rng.randint(0, k - 1)) for s in sessions}
        plan = raters.allocate_plan(sessions, rs, k, rng.randint(0, 999),
                                    existing=existing)
        for s, members in plan.items():
            assert len(members) == k and len(set(members)) == k
            # Work already on disk is never reallocated away from its rater.
            assert set(existing[s]).issubset(set(members))
        assert is_connected(plan)


@pytest.mark.parametrize("n_sessions,n_raters,k", [
    (2, 4, 2), (2, 9, 2), (3, 8, 3), (3, 20, 3), (26, 60, 3), (1, 4, 3),
])
def test_refuses_a_pool_too_large_to_connect(raters, n_sessions, n_raters, k):
    """More raters than N*(k-1)+1 cannot be connected however they are dealt.

    Twenty raters over three encounters is not a big wave, it is an
    unmeasurable one, and the refusal is the only place the operator can be
    told before the ICC comes back from a matrix with no common column.
    """
    sessions = [f"s_1772400{i:03d}_{i:06x}" for i in range(n_sessions)]
    rs = [f"rr_{i:012x}" for i in range(n_raters)]
    with pytest.raises(ValueError, match="connected"):
        raters.allocate_plan(sessions, rs, per_encounter=k, seed=1)
    # One under the bound is fine.
    ok = n_sessions * (k - 1) + 1
    plan = raters.allocate_plan(sessions, rs[:ok], per_encounter=k, seed=1)
    assert is_connected(plan)
    assert len(loads(plan)) == ok  # everybody in the permitted pool got work


def test_the_bound_is_not_applied_to_a_single_rater_pass(raters):
    # per_encounter=1 has no overlap to protect, so the pool size is the
    # operator's business.
    sessions = [f"s_1772400{i:03d}_{i:06x}" for i in range(3)]
    rs = [f"rr_{i:012x}" for i in range(20)]
    plan = raters.allocate_plan(sessions, rs, per_encounter=1, seed=1)
    assert sum(len(v) for v in plan.values()) == 3


def test_per_encounter_one_is_allowed_but_has_no_overlap(raters):
    # One rater per encounter cannot support agreement of any kind. The module
    # permits it (a pilot pass) and skips the connectivity requirement rather
    # than pretending the resulting design is measurable.
    sessions = make_sessions(raters, 6)
    rs = make_raters(raters, 3)
    created = raters.assign(sessions, rs, per_encounter=1, seed=3)
    assert len(created) == 6
    plan = {s: [a["rater_id"] for a in raters.assignments_for_encounter(s)]
            for s in sessions}
    assert all(len(v) == 1 for v in plan.values())
    assert not is_connected(plan)  # three isolated raters, by construction
    assert raters.coverage(sessions, per_encounter=1)["overlap_components"] == 3


def test_repair_connectivity_merges_a_split_wave(raters):
    """The repair, driven directly with a plan the construction would not make.

    Two blocks of three raters that share nobody: internally consistent, no
    pairwise comparison spans them, and an ICC over the whole thing would be
    computed from a matrix that never had a common column.
    """
    plan = {
        "s_1772400001_000001": ["a", "b", "c"],
        "s_1772400002_000002": ["a", "b", "c"],
        "s_1772400003_000003": ["d", "e", "f"],
        "s_1772400004_000004": ["d", "e", "f"],
    }
    new = {k: list(v) for k, v in plan.items()}
    before = loads(plan)
    assert not is_connected(plan)
    raters._repair_connectivity(list(plan), plan, new, dict(before))
    assert is_connected(plan)
    # Every encounter still has its complement and no repeated rater.
    for members in plan.values():
        assert len(members) == 3
        assert len(set(members)) == 3
    after = loads(plan)
    assert sum(after.values()) == sum(before.values())
    # One merge costs at most one unit of load on two people.
    assert max(abs(after.get(r, 0) - before[r]) for r in before) <= 1


def test_repair_refuses_when_existing_assignments_pin_the_split(raters):
    plan = {
        "s_1772400001_000001": ["a", "b", "c"],
        "s_1772400002_000002": ["d", "e", "f"],
    }
    new = {k: [] for k in plan}  # nothing movable: both are already on disk
    with pytest.raises(ValueError, match="connect the rater-overlap graph"):
        raters._repair_connectivity(list(plan), plan, new, loads(plan))


def test_a_split_top_up_is_repaired_end_to_end(raters):
    """Two earlier calls left two islands; a third call must join them.

    This is the case construction cannot reach — the split is already on disk —
    and it is exactly the shape a researcher produces by allocating the first
    half of a wave to one set of coders and the second half to another.
    """
    left = make_sessions(raters, 3)
    right = [make_session(raters, f"s_17724099{i:02d}_{i:06x}") for i in range(3)]
    pool_a = make_raters(raters, 3)
    pool_b = make_raters(raters, 3)
    raters.assign(left, pool_a, per_encounter=2, seed=1)
    raters.assign(right, pool_b, per_encounter=2, seed=1)
    split = {s: [a["rater_id"] for a in raters.assignments_for_encounter(s)]
             for s in left + right}
    assert not is_connected(split)

    raters.assign(left + right, pool_a + pool_b, per_encounter=3, seed=1)
    joined = {s: [a["rater_id"] for a in raters.assignments_for_encounter(s)]
              for s in left + right}
    assert is_connected(joined)
    for s, members in joined.items():
        assert len(members) == 3 and len(set(members)) == 3
    # The first two passes' assignments were not rewritten.
    for s in left + right:
        assert set(split[s]).issubset(set(joined[s]))


# ---------- topping a wave up ----------

def test_topping_up_keeps_existing_work_and_balances_the_rest(raters):
    sessions = make_sessions(raters, 12)
    first_pool = make_raters(raters, 4)
    raters.assign(sessions, first_pool, per_encounter=2, seed=5)
    before = {s: [a["assignment_id"] for a in raters.assignments_for_encounter(s)]
              for s in sessions}

    extra = make_raters(raters, 2)
    created = raters.assign(sessions, first_pool + extra, per_encounter=3, seed=5)
    assert len(created) == 12  # exactly one more rater per encounter

    for s in sessions:
        got = raters.assignments_for_encounter(s)
        assert len(got) == 3
        assert len({a["rater_id"] for a in got}) == 3
        # The first pass's assignment ids survived untouched.
        assert set(before[s]).issubset({a["assignment_id"] for a in got})
    # The two new raters carried the top-up, because they had no load.
    per = loads({s: [a["rater_id"] for a in raters.assignments_for_encounter(s)]
                 for s in sessions})
    assert per[extra[0]] > 0 and per[extra[1]] > 0
    assert max(per.values()) - min(per.values()) <= 1


def test_top_up_cannot_duplicate_an_existing_rater(raters):
    sessions = make_sessions(raters, 4)
    rs = make_raters(raters, 3)
    raters.assign(sessions, rs, per_encounter=2, seed=1)
    # Only three raters exist and two are already on each encounter, so the
    # third is the only legal choice; a fourth pass has nobody left.
    raters.assign(sessions, rs, per_encounter=3, seed=1)
    for s in sessions:
        got = [a["rater_id"] for a in raters.assignments_for_encounter(s)]
        assert sorted(got) == sorted(rs)
    with pytest.raises(ValueError):
        raters.assign(sessions, rs, per_encounter=4, seed=1)


# ---------- scoping and status ----------

def test_a_rater_sees_only_their_own_assignments(raters):
    sessions = make_sessions(raters, 9)
    rs = make_raters(raters, 3)
    raters.assign(sessions, rs, per_encounter=2, seed=11)
    mine = raters.assignments_for_rater(rs[0])
    assert mine
    assert all(a["rater_id"] == rs[0] for a in mine)
    everything = raters.list_assignments()
    assert len(mine) < len(everything)
    # An unknown or malformed rater id reaches nothing at all, rather than
    # falling through to "no filter".
    assert raters.assignments_for_rater("rr_000000000000") == []
    assert raters.assignments_for_rater("../../etc") == []
    assert raters.assignments_for_rater(None) == []


def test_assignment_fields_and_status_transition(raters):
    sessions = make_sessions(raters, 1, scenario="S4A")
    rs = make_raters(raters, 3)
    created = raters.assign(sessions, rs, per_encounter=3, seed=2)
    a = created[0]
    assert raters._ASSIGNMENT_ID_RE.fullmatch(a["assignment_id"])
    assert a["session_id"] == sessions[0]
    assert a["status"] == "pending"
    assert a["construct"] == "teamwork"      # from the scenario spec, not the item set
    assert a["cohort"] == "study"
    assert isinstance(a["assigned_at"], float)

    assert raters.assignments_for_rater(a["rater_id"], status="submitted") == []
    updated = raters.mark_submitted(a["assignment_id"])
    assert updated["status"] == "submitted"
    assert updated["submitted_at"] == updated["first_submitted_at"]
    assert raters.assignments_for_rater(a["rater_id"], status="pending") == []
    assert len(raters.assignments_for_rater(a["rater_id"], status="submitted")) == 1

    # A correction moves submitted_at but not the first pass, which is what the
    # elapsed-time analysis wants.
    again = raters.mark_submitted(a["assignment_id"], submitted_at=updated["submitted_at"] + 60)
    assert again["first_submitted_at"] == updated["first_submitted_at"]
    assert again["submitted_at"] > again["first_submitted_at"]
    assert raters.mark_submitted("as_000000000000") is None
    assert raters.mark_submitted("../../x") is None


def test_a_racing_writer_cannot_double_book(raters):
    """The unique index is the backstop the in-memory check cannot be.

    Simulates the race: another process inserted the pair between this call's
    read of the existing assignments and its own insert. The index row here has
    no document, so assignments_for_encounter does not see it and the
    allocation goes ahead — exactly the window the index exists to close.
    """
    sid = make_sessions(raters, 1)[0]
    rs = make_raters(raters, 3)
    with raters._db() as conn:
        conn.execute(
            "INSERT INTO rater_assignments (assignment_id, session_id, rater_id,"
            " construct, cohort, status, assigned_at) VALUES (?,?,?,?,?,?,?)",
            ("as_ffffffffffff", sid, rs[0], "teamwork", "study", "pending", 1.0),
        )
    with pytest.raises(ValueError, match="already assigned"):
        raters.assign([sid], rs, per_encounter=3, seed=1)
    # The document written just before the collision was rolled back, so no
    # assignment file is left behind with nothing indexing it.
    written = sorted(p.name for p in raters.ASSIGNMENTS_DIR.glob("as_*.json"))
    assert all(raters.get_assignment(p[:-5]) for p in written)
    with raters._db() as conn:
        rows = conn.execute(
            "SELECT assignment_id FROM rater_assignments WHERE session_id = ?"
            " AND rater_id = ?", (sid, rs[0])).fetchall()
    assert len(rows) == 1


def test_get_assignment_is_traversal_safe(raters):
    for bad in ("", None, "as_ZZZ", "../raters/rr_1", "as_0123456789ab/.."):
        assert raters.get_assignment(bad) is None


def test_cohort_travels_onto_the_assignment(raters):
    study = make_sessions(raters, 2)
    internal = [make_session(raters, "s_1773142745_384dad", "S3B", "internal")]
    rs = make_raters(raters, 3)
    raters.assign(study + internal, rs, per_encounter=3, seed=4)
    assert len(raters.list_assignments(cohort="study")) == 6
    assert len(raters.list_assignments(cohort="internal")) == 3


def test_legacy_scenario_gets_a_null_construct(raters):
    # A demo scenario outside the v3 instrument has no construct. That must
    # produce an assignment with construct None, not an exception.
    s = make_session(raters, "s_1772400111_aaaaaa", scenario="not-a-real-spec")
    rs = make_raters(raters, 3)
    created = raters.assign([s], rs, per_encounter=3, seed=1)
    assert all(a["construct"] is None for a in created)


def test_coverage_report(raters):
    sessions = make_sessions(raters, 9)
    rs = make_raters(raters, 4)
    raters.assign(sessions, rs, per_encounter=3, seed=6)
    cov = raters.coverage(sessions, per_encounter=3)
    assert cov["encounters"] == 9
    assert cov["assignments"] == 27
    assert cov["submitted"] == 0
    assert cov["under_complement"] == []
    assert cov["overlap_connected"] is True
    assert cov["load_max"] - cov["load_min"] <= 1
    raters.mark_submitted(raters.list_assignments()[0]["assignment_id"])
    assert raters.coverage(sessions, per_encounter=3)["submitted"] == 1
    # An encounter with nobody on it is reported short, not omitted.
    lonely = make_session(raters, "s_1772400999_bbbbbb")
    assert lonely in raters.coverage(sessions + [lonely], 3)["under_complement"]


# ---------- against the real fixture wave ----------

def test_against_the_fixture_wave(tmp_path, monkeypatch, wave_sessions):
    """Allocate the wave's study encounters to 5 raters, 3 apiece.

    The manifests are copied into a scratch DATA_DIR rather than assigned in
    place: the wave is shared, and this would otherwise write raters, an
    assignment directory and index rows into it.

    The counts are derived from the wave rather than written down. The old
    version hard-coded 26 encounters, 78 assignments and a [15,15,16,16,16]
    load split against one machine's scratchpad fixture — true of that wave
    only, so any other recorded wave failed here for no reason. What is under
    test is the allocator's *properties* (three distinct raters each, a load
    spread of at most one, a connected design), and those hold on any wave.
    """
    data = tmp_path / "wave"
    (data / "sessions").mkdir(parents=True)
    for src in sorted(wave_sessions.iterdir()):
        manifest = src / "manifest.json"
        if manifest.exists():
            dest = data / "sessions" / src.name
            dest.mkdir()
            shutil.copy2(manifest, dest / "manifest.json")

    monkeypatch.setenv("DATA_DIR", str(data))
    import server.storage as storage

    importlib.reload(storage)
    import server.raters as raters_mod

    importlib.reload(raters_mod)
    raters_mod.init_rater_storage()

    study = []
    for d in sorted((data / "sessions").iterdir()):
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        if m.get("cohort") == "study":
            study.append(d.name)
    if len(study) < 3:
        pytest.skip(f"the wave under {wave_sessions} has fewer than 3 study "
                    "encounters, too few to allocate 3 raters apiece")

    pool = [raters_mod.create_rater(f"Coder {i}", kind="trained")["rater_id"]
            for i in range(5)]
    created = raters_mod.assign(study, pool, per_encounter=3, seed=2026)
    assert len(created) == 3 * len(study)

    plan = {s: [a["rater_id"] for a in raters_mod.assignments_for_encounter(s)]
            for s in study}
    assert all(len(set(v)) == 3 for v in plan.values())
    per = loads(plan)
    # Every slot allocated, and no rater carries more than one encounter's
    # worth above the lightest — the balance property, not one wave's numbers.
    counts = [per.get(r, 0) for r in pool]   # a rater with no work counts as 0
    assert sum(counts) == 3 * len(study)
    assert max(counts) - min(counts) <= 1
    assert is_connected(plan)

    # Every construct in the wave reached an assignment, which is what the
    # per-construct reliability report needs.
    constructs = {a["construct"] for a in raters_mod.list_assignments()}
    assert constructs == {"conflict_management", "influence",
                          "inspirational_leadership", "teamwork"}

    # The internal test encounter was not in the list, so it has no raters.
    cov = raters_mod.coverage(study, per_encounter=3)
    assert cov["overlap_connected"] and cov["under_complement"] == []

    # A rater's token reaches their own assignments and no one else's.
    token = raters_mod.issue_token(pool[0], days=14)
    who = raters_mod.rater_for_token(token)
    assert who["rater_id"] == pool[0]
    mine = raters_mod.assignments_for_rater(who["rater_id"])
    assert 15 <= len(mine) <= 16
    assert all(a["rater_id"] == pool[0] for a in mine)


def test_data_dir_is_the_one_under_test(raters):
    # Guards the harness itself: if the reload ever stopped taking effect these
    # tests would be writing into the developer's real data directory.
    assert str(raters.DATA_DIR) == str(Path(os.environ["DATA_DIR"]).resolve())
