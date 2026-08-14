import json
import os
import multiprocessing
import stat
import time
import pytest
import gozer.gatekeeper as gatekeeper
from gozer.gatekeeper import Gatekeeper, utcnow


@pytest.fixture
def gk(tmp_path, sysfs):
    from conftest import QUIETBOX
    return Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX))


def test_creates_state_dirs(gk):
    for sub in ("gate", "leases", "queue"):
        assert os.path.isdir(os.path.join(gk.root, sub))


def test_claim_is_exclusive(gk):
    assert gk.claim_unit("BOARD-A", {"lease_id": "aaa"}) is True
    assert gk.claim_unit("BOARD-A", {"lease_id": "bbb"}) is False


def test_release_allows_reclaim(gk):
    gk.claim_unit("BOARD-A", {"lease_id": "aaa"})
    gk.release_unit("BOARD-A")
    assert gk.claim_unit("BOARD-A", {"lease_id": "bbb"}) is True


def test_unit_lease_round_trips(gk):
    gk.claim_unit("BOARD-A", {"lease_id": "aaa", "who": "claude:test"})
    assert gk.unit_lease("BOARD-A")["who"] == "claude:test"
    assert gk.unit_lease("BOARD-B") is None


def test_update_unit_lease_rewrites_in_place(gk):
    gk.claim_unit("BOARD-A", {"lease_id": "aaa", "expect_done": None})
    assert gk.update_unit_lease("BOARD-A", {"lease_id": "aaa", "expect_done": "later"})
    assert gk.unit_lease("BOARD-A")["expect_done"] == "later"


def test_update_unit_lease_refuses_to_create_a_lock(gk):
    assert gk.update_unit_lease("NEVER-HELD", {"lease_id": "aaa"}) is False
    assert gk.unit_lease("NEVER-HELD") is None


def test_lease_records_round_trip(gk):
    lease = {"lease_id": "2f9a1c", "who": "claude:x", "chips": ["0000:01:00.0"]}
    gk.write_lease(lease)
    assert gk.read_lease("2f9a1c") == lease
    assert gk.read_lease("nope") is None
    assert [l["lease_id"] for l in gk.all_leases()] == ["2f9a1c"]


def test_lease_ids_are_unique_and_short(gk):
    ids = {gk.new_lease_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 6 for i in ids)


def test_utcnow_is_zulu():
    assert utcnow().endswith("Z")


def _racer(args):
    root, sysfs_root, n = args
    gk = Gatekeeper(root=root, sysfs_root=sysfs_root)
    return gk.claim_unit("CONTESTED", {"lease_id": f"r{n}"})


def test_concurrent_claims_yield_exactly_one_winner(tmp_path, sysfs):
    from conftest import QUIETBOX
    root = str(tmp_path / "state")
    sysfs_root = sysfs(QUIETBOX)
    Gatekeeper(root=root, sysfs_root=sysfs_root)  # create dirs first
    with multiprocessing.Pool(8) as pool:
        results = pool.map(_racer, [(root, sysfs_root, n) for n in range(8)])
    assert sum(results) == 1


def test_critical_section_is_reentrant_across_calls(gk):
    with gk.critical_section():
        pass
    with gk.critical_section():
        pass  # must not deadlock on a leftover mutex dir


def test_critical_section_nesting_does_not_deadlock(gk):
    """A nested call to critical_section from *inside* an already-held one
    (e.g. Keymaster.acquire, Task 8, calling may_claim while already holding
    the section) must return immediately rather than trying to take its own
    filesystem lock a second time. The inner call uses a short timeout so a
    regression here fails fast instead of hanging the whole suite: before
    this was made reentrant, the inner call would block for its full
    timeout and then raise TimeoutError (or, worse, self-heal by deleting
    the outer call's own live lock once MUTEX_STALE_SECONDS elapsed)."""
    with gk.critical_section():
        with gk.critical_section(timeout=0.5):
            pass  # must complete immediately, not deadlock or time out


def test_critical_section_recovers_after_exception_in_nested_block(gk):
    """An exception raised inside a nested critical_section block must not
    leave the reentrancy depth counter stuck above zero -- otherwise every
    later call in this process would believe it's already nested and skip
    taking the real lock, silently disabling cross-process exclusion for
    good. Proven behaviourally: after the exception propagates, a fresh
    critical_section() call must take the real mkdir lock again and release
    it cleanly, not skip locking."""
    mutex_path = os.path.join(gk.root, "mutex", ".gatekeeper.lock")

    with pytest.raises(ValueError):
        with gk.critical_section():
            with gk.critical_section():
                raise ValueError("boom")

    with gk.critical_section():
        assert os.path.isdir(mutex_path)  # real lock taken, depth was reset
    assert not os.path.isdir(mutex_path)  # released cleanly afterward


def test_state_dir_modes_support_multi_user_sharing(gk):
    """gate/leases/queue must be sticky-shared like the root; mutex/ must NOT
    be sticky, since a non-sticky parent is what lets any user clear another
    user's crashed mutex (see critical_section's docstring)."""
    def perm_bits(path):
        return stat.S_IMODE(os.stat(path).st_mode)

    assert perm_bits(gk.root) == 0o1777
    for sub in ("gate", "leases", "queue"):
        assert perm_bits(os.path.join(gk.root, sub)) == 0o1777

    mutex_dir = os.path.join(gk.root, "mutex")
    mutex_mode = os.stat(mutex_dir).st_mode
    assert perm_bits(mutex_dir) == 0o777
    assert not (mutex_mode & stat.S_ISVTX)


def test_critical_section_self_heals_stale_mutex_same_user(gk):
    """Same-UID stale-mutex recovery: backdate the mutex dir's mtime past the
    staleness threshold and confirm critical_section reclaims it rather than
    timing out. Cross-user recovery (the actual point of the non-sticky
    mutex/ directory) cannot be exercised here — a single-UID sandbox has no
    second user to be denied permission as — so it is not tested; see the
    task report for that limitation."""
    mutex_path = os.path.join(gk.root, "mutex", ".gatekeeper.lock")
    os.mkdir(mutex_path)
    stale = time.time() - (gatekeeper.MUTEX_STALE_SECONDS + 1)
    os.utime(mutex_path, (stale, stale))

    with gk.critical_section(timeout=2.0):
        pass  # must reclaim the stale dir rather than raising TimeoutError

    assert not os.path.isdir(mutex_path)  # released cleanly afterward


def test_held_units_returns_claimed_units_keyed_by_unit(gk):
    assert gk.held_units() == {}
    gk.claim_unit("BOARD-A", {"lease_id": "aaa"})
    gk.claim_unit("BOARD-B", {"lease_id": "bbb"})
    assert gk.held_units() == {
        "BOARD-A": {"lease_id": "aaa"},
        "BOARD-B": {"lease_id": "bbb"},
    }


def test_held_units_empty_when_nothing_held(gk):
    assert gk.held_units() == {}


def test_delete_lease_removes_record(gk):
    gk.write_lease({"lease_id": "aaa"})
    gk.delete_lease("aaa")
    assert gk.read_lease("aaa") is None


def test_delete_lease_missing_is_a_noop(gk):
    gk.delete_lease("never-existed")  # must not raise
    assert gk.read_lease("never-existed") is None


def test_update_unit_lease_returns_false_when_released_mid_write(gk, monkeypatch):
    """update_unit_lease's isdir-check-then-write is not atomic together: if
    release_unit() lands in that window, the write must fail closed (return
    False) rather than raising, per its documented contract."""
    gk.claim_unit("BOARD-A", {"lease_id": "aaa"})
    real_write = gatekeeper._atomic_write_json

    def racing_write(path, payload):
        gk.release_unit("BOARD-A")  # simulate the race landing here
        real_write(path, payload)

    monkeypatch.setattr(gatekeeper, "_atomic_write_json", racing_write)
    assert gk.update_unit_lease("BOARD-A", {"lease_id": "aaa2"}) is False
