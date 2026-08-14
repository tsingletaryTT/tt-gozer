import json
import os
import multiprocessing
import pytest
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
