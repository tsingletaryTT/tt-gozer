# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""A board missing a chip it should have is DEGRADED, never FREE.

A p300 board has two chips. When one reads all ones it is UNREADABLE and set
aside, and its partner, which still reads its serial, would otherwise be a
board of one that `status` called FREE. UMD expands a grant of it to the whole
board, dead chip included, so the tenant's runtime fails to start.
"""
import json

import pytest

from conftest import QUIETBOX
from gozer.cli import main
from gozer.topology import (EXPECTED_CHIPS_PER_BOARD, read_topology_full,
                            short_boards)

DEAD = "FFFFFFFFFFFFFFFF"


def chips_with_dead(index=2):
    chips = [dict(c) for c in QUIETBOX]
    chips[index]["serial"] = DEAD
    chips[index]["card"] = "unknown"
    return chips


def test_expected_sizes_cover_the_p300_and_p150_families():
    assert EXPECTED_CHIPS_PER_BOARD["p300c"] == 2
    assert EXPECTED_CHIPS_PER_BOARD["p150a"] == 1


def test_a_p300c_with_one_readable_chip_is_short(sysfs):
    boards = read_topology_full(sysfs(chips_with_dead())).boards
    short = short_boards(boards)
    assert short == {"0000000000000001": (1, 2)}


def test_a_complete_box_has_no_short_boards(sysfs):
    assert short_boards(read_topology_full(sysfs(QUIETBOX)).boards) == {}


def test_an_unknown_card_type_is_never_called_short(sysfs):
    chips = [dict(c, card="mystery") for c in QUIETBOX[:3]]
    chips[2]["serial"] = "0000000000000001"
    assert short_boards(read_topology_full(sysfs(chips)).boards) == {}


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    (tmp_path / "proc" / "1" / "fd").mkdir(parents=True)
    (tmp_path / "proc" / "1" / "comm").write_text("python\n")
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(chips_with_dead()))
    monkeypatch.setenv("GOZER_PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return tmp_path


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def test_the_partner_of_a_dead_chip_is_degraded_not_free(env, capsys):
    code, out = run(["status", "--json"], capsys)
    states = {c["dev_index"]: c["state"] for c in json.loads(out)["chips"]}
    assert states == {0: "FREE", 1: "FREE", 2: "UNREADABLE", 3: "DEGRADED"}


def test_status_text_names_the_cause_of_degraded(env, capsys):
    code, out = run(["status"], capsys)
    line = next(l for l in out.splitlines() if "0000:04:00.0" in l)
    assert "DEGRADED" in line and "FREE" not in line
    assert "1 of 2" in out


def test_the_partner_is_never_granted(env, capsys):
    granted = set()
    for _ in range(4):
        code, out = run(["acquire", "--chips", "1", "--who", "t", "--no-queue",
                         "--json"], capsys)
        if code == 0:
            granted |= set(json.loads(out)["chips"])
    assert "0000:04:00.0" not in granted
    assert granted == {"0000:01:00.0", "0000:02:00.0"}


@pytest.mark.parametrize("target", ["3", "0000:04:00.0"])
def test_exact_on_the_degraded_chip_is_refused_not_queued(env, capsys, target):
    code, out = run(["acquire", "--chips", "1", "--who", "t", "--exact", target,
                     "--no-queue"], capsys)
    assert code == 12
    assert "degraded" in out.lower()
    code, q = run(["queue", "--json"], capsys)
    assert json.loads(q)["queue"] == []
