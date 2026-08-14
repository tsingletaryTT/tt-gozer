import pytest
from gozer.topology import read_topology, lease_grain, all_chips, TopologyError
from conftest import QUIETBOX, SINGLE_CHIP_BOARDS, GALAXY_LIKE


def test_reads_quietbox_as_two_boards_of_two(sysfs):
    boards = read_topology(sysfs(QUIETBOX))
    assert [b.serial for b in boards] == ["0000046131924055", "0000046131924062"]
    assert all(len(b.chips) == 2 for b in boards)


def test_maps_bdf_and_asic_id(sysfs):
    chips = all_chips(read_topology(sysfs(QUIETBOX)))
    assert [c.bdf for c in chips] == [
        "0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "0000:04:00.0"]
    assert chips[0].asic_id == "FCF9BCF9E3C8B89E"
    assert chips[0].card == "p300c"


def test_grain_is_board_for_two_chip_boards(sysfs):
    # A p300c holds 2 chips; UMD expands TT_VISIBLE_DEVICES to the whole board.
    assert lease_grain(read_topology(sysfs(QUIETBOX))) == "board"


def test_grain_is_chip_for_single_chip_boards(sysfs):
    assert lease_grain(read_topology(sysfs(SINGLE_CHIP_BOARDS))) == "chip"


def test_grain_is_chip_when_a_board_holds_more_than_two(sysfs):
    # UMD skips board expansion for >2 chips per board, so we can lease per chip.
    assert lease_grain(read_topology(sysfs(GALAXY_LIKE))) == "chip"


def test_missing_tree_raises(tmp_path):
    with pytest.raises(TopologyError):
        read_topology(str(tmp_path / "nope"))
