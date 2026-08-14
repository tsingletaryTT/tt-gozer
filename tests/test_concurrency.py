"""The spec's headline concurrency requirement, exercised end to end.

Spec line 454: "Concurrent `acquire` from N processes grants disjoint chip
sets, never overlapping."

Until this file existed the only concurrency test in the suite raced
`claim_unit` alone -- a bare `mkdir`, the one primitive that was never in
doubt. Everything layered above it (allocate's reap, the lease record, the
gate lock's lifetime) went unexercised across processes, which is how two
lock-deleting races survived 33 commits and 162 green tests.

These tests use real processes, not threads: the gate's exclusion is
filesystem-based and per-process (`critical_section`'s depth counter is
per-instance), so threads would not exercise the same code paths.
"""
import itertools
import multiprocessing
import os
import time

import pytest

import gozer.gatekeeper as gatekeeper
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Grant, Keymaster
from conftest import QUIETBOX

# The contested unit on the QUIETBOX fixture: the board holding chips 0 and 1,
# and the one `allocate` picks first (lowest device index wins the tiebreak).
CONTESTED_UNIT = "0000000000000002"
STALE_SINCE = "2000-01-01T00:00:00Z"


def _empty_proc(tmp_path):
    """A /proc with no processes in it: no lease's pid is ever 'alive' here,
    and nothing holds a device fd. Detached leases are therefore judged purely
    by their grace window, which is what both tests below turn on."""
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _acquire_worker(args):
    """One full `gozer acquire`, in its own process. Returns the granted BDFs
    (empty when the request was queued instead of granted)."""
    root, sysfs_root, proc_root, who = args
    gk = Gatekeeper(root=root, sysfs_root=sysfs_root, proc_root=proc_root)
    result = Keymaster(gk).acquire("1", who=who)
    return list(result.bdfs) if isinstance(result, Grant) else []


def test_concurrent_acquire_grants_disjoint_chip_sets(tmp_path, sysfs):
    """N processes, one state dir, full acquire each: no chip twice.

    This is the requirement the whole tool exists to satisfy. It asserts on
    the *granted* sets rather than on how many won, because how many win is a
    function of the fixture (2 boards) while disjointness is the invariant.
    """
    root = str(tmp_path / "state")
    sysfs_root = sysfs(QUIETBOX)
    proc_root = _empty_proc(tmp_path)
    Gatekeeper(root=root, sysfs_root=sysfs_root)  # create the state dirs first

    n = 6
    with multiprocessing.Pool(n) as pool:
        results = pool.map(_acquire_worker,
                           [(root, sysfs_root, proc_root, f"claude:{i}")
                            for i in range(n)])

    granted = [set(r) for r in results if r]
    assert granted, "no process was granted anything at all"
    for a, b in itertools.combinations(granted, 2):
        assert a.isdisjoint(b), f"overlapping grants: {a} and {b}"
    # Never hand out more chips than the machine has, however the races fell.
    assert sum(len(g) for g in granted) <= len(QUIETBOX)

    # And the gate on disk must agree with what was handed out: every granted
    # chip belongs to a unit that is still locked.
    gk = Gatekeeper(root=root, sysfs_root=sysfs_root, proc_root=proc_root)
    locked_bdfs = {c.bdf for unit in gk.held_units()
                   for c in gk.chips_in_unit(unit)}
    assert set().union(*granted) <= locked_bdfs


def _slow_reconcile_worker(root, sysfs_root, proc_root, marker, delay):
    """A `gozer status`-style reconcile whose snapshot-to-reap window is
    stretched wide open.

    `reconcile` decides what is stale from a `held_units()` snapshot and only
    then reaps. Widening that window (by sleeping once, inside the staleness
    computation, after the snapshot has been taken) turns a millisecond race
    into a deterministic one, without changing any of the logic under test.
    The marker file tells the parent the snapshot has been taken and it is
    now safe to race an `acquire` in.
    """
    gk = Gatekeeper(root=root, sysfs_root=sysfs_root, proc_root=proc_root)
    real_elapsed = gatekeeper._elapsed_seconds
    slept = []

    def slow_elapsed(since, now):
        value = real_elapsed(since, now)
        if not slept:
            slept.append(True)
            with open(marker, "w") as f:
                f.write("snapshot taken\n")
            time.sleep(delay)
        return value

    gatekeeper._elapsed_seconds = slow_elapsed
    gk.reconcile(reap=True)


def test_reconcile_never_deletes_a_lock_acquired_after_its_snapshot(tmp_path, sysfs):
    """The B1 race, reproduced: a reconcile must not reap a lease that was
    created after it took its snapshot.

    Sequence:
      1. A stale detached lease sits on the contested unit.
      2. Process A (`gozer status`) snapshots, decides that lease is stale,
         and is held in that window.
      3. Process B (`gozer acquire`) legitimately reaps the stale lease and
         claims the unit for itself.
      4. Process A finishes its reap.

    Step 4 must not touch B's brand-new lock. Before the fix it deleted it
    outright: the gate then reported the board free while B held a valid
    lease and export line, and the next acquirer was granted the same chips.
    """
    root = str(tmp_path / "state")
    sysfs_root = sysfs(QUIETBOX)
    proc_root = _empty_proc(tmp_path)
    gk = Gatekeeper(root=root, sysfs_root=sysfs_root, proc_root=proc_root)

    stale = {"lease_id": "old", "who": "claude:crashed", "pid": 424242,
             "pgid": 424242, "detached": True, "since": STALE_SINCE,
             "chips": ["0000:01:00.0", "0000:02:00.0"], "dev_indices": [0, 1],
             "units": [CONTESTED_UNIT], "state": "active"}
    gk.claim_unit(CONTESTED_UNIT, stale)
    gk.write_lease(stale)

    marker = str(tmp_path / "snapshot-taken")
    reconciler = multiprocessing.Process(
        target=_slow_reconcile_worker,
        args=(root, sysfs_root, proc_root, marker, 1.5))
    reconciler.start()
    try:
        deadline = time.monotonic() + 5
        while not os.path.exists(marker) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert os.path.exists(marker), "reconciler never reached its snapshot"

        grant = Keymaster(gk).acquire("1", who="claude:winner")
        assert isinstance(grant, Grant), "the racing acquire was not granted"
        assert CONTESTED_UNIT in grant.units
    finally:
        reconciler.join(30)

    assert reconciler.exitcode == 0
    held = gk.unit_lease(CONTESTED_UNIT)
    assert held is not None, (
        "the concurrent reconcile deleted a lock created after its snapshot")
    assert held["lease_id"] == grant.lease_id
    assert gk.read_lease(grant.lease_id) is not None


def test_release_unit_refuses_to_delete_someone_elses_lock(tmp_path, sysfs):
    """The primitive-level half of the same guard.

    The check belongs in `release_unit`, not only at reconcile's call site:
    every caller that decided from an older read is protected by it.
    """
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=_empty_proc(tmp_path))
    gk.claim_unit(CONTESTED_UNIT, {"lease_id": "mine"})

    assert gk.release_unit(CONTESTED_UNIT, expected_lease_id="theirs") is False
    assert gk.unit_lease(CONTESTED_UNIT)["lease_id"] == "mine"

    assert gk.release_unit(CONTESTED_UNIT, expected_lease_id="mine") is True
    assert gk.unit_lease(CONTESTED_UNIT) is None
