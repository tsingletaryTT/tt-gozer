import pytest
from gozer.gatekeeper import Gatekeeper, parse_chip_request, utcnow
from conftest import QUIETBOX, GALAXY_LIKE


def fake_proc(tmp_path, live_pids=(1,)):
    """A /proc where the given pids exist and hold nothing open.

    pid 1 must be alive here: several tests below plant a lease owned by pid 1,
    and free_units() reconciles first. Against an empty/absent proc tree that
    pid reads as dead, reconcile reaps the lease as STALE, and the very
    contention those tests are asserting silently disappears.
    """
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid in live_pids:
        (root / str(pid) / "fd").mkdir(parents=True, exist_ok=True)
        (root / str(pid) / "comm").write_text("python\n")
    return str(root)


def make(tmp_path, sysfs, chips=QUIETBOX):
    return Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(chips),
                      proc_root=fake_proc(tmp_path))


@pytest.mark.parametrize("spec,total,expected", [
    ("1", 4, (1, 1)),
    ("2", 4, (2, 2)),
    ("all", 4, (4, 4)),
    ("1-4", 4, (1, 4)),
    ("2-3", 4, (2, 3)),
])
def test_parse_chip_request(spec, total, expected):
    assert parse_chip_request(spec, total) == expected


@pytest.mark.parametrize("spec", ["", "zero", "3-1", "-2", "1-", "0"])
def test_parse_chip_request_rejects_garbage(spec):
    with pytest.raises(ValueError):
        parse_chip_request(spec, 4)


def test_allocates_one_board_for_one_chip_at_board_grain(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    units = gk.allocate(1, 1)
    assert len(units) == 1
    # Asking for 1 yields 2 chips, because UMD expands to the whole p300c board.
    assert len(gk.chips_in_unit(units[0])) == 2


def test_allocates_all_boards_for_all(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    units = gk.allocate(4, 4)
    assert len(units) == 2


def test_returns_none_when_minimum_cannot_be_met(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    gk.claim_unit("0000046131924055", {"lease_id": "x", "pid": 1})
    gk.claim_unit("0000046131924062", {"lease_id": "y", "pid": 1})
    assert gk.allocate(1, 1) is None


def test_elastic_takes_what_is_available_above_the_minimum(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    gk.claim_unit("0000046131924055", {"lease_id": "x", "pid": 1})
    units = gk.allocate(1, 4)  # only one board left
    assert units == ["0000046131924062"]


def test_prefers_the_lower_indexed_board_for_determinism(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.allocate(1, 1) == ["0000046131924062"]  # holds dev 0,1


def test_exact_selects_a_named_chip_or_board(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.allocate(1, 1, exact="0000:03:00.0") == ["0000046131924055"]
    assert gk.allocate(1, 1, exact="2") == ["0000046131924055"]


def test_exact_returns_none_when_that_unit_is_taken(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    gk.claim_unit("0000046131924055", {"lease_id": "x", "pid": 1})
    assert gk.allocate(1, 1, exact="0000:03:00.0") is None


def test_fresh_requires_a_clean_unit(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.allocate(1, 1, fresh=True) is None
    gk.mark_clean("0000046131924055")
    assert gk.allocate(1, 1, fresh=True) == ["0000046131924055"]


def test_chip_grain_allocates_individual_chips(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, chips=GALAXY_LIKE)
    assert gk.grain == "chip"
    units = gk.allocate(1, 1)
    assert len(units) == 1
    assert len(gk.chips_in_unit(units[0])) == 1


def test_eth_neighbours_reports_other_tenants_on_shared_boards(tmp_path, sysfs):
    # At chip grain, two tenants can share one board's eth mesh.
    gk = make(tmp_path, sysfs, chips=GALAXY_LIKE)
    gk.claim_unit("0000:01:00.0", {"lease_id": "x", "pid": 1, "who": "claude:other"})
    neighbours = gk.eth_neighbours(["0000:02:00.0"])
    assert neighbours == {"0000:01:00.0": "claude:other"}


def test_no_eth_neighbours_when_whole_board_is_yours(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.eth_neighbours(["0000046131924062"]) == {}
