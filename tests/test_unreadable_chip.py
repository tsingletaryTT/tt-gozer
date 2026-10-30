# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""A chip whose tt_serial reads all ones is UNREADABLE, never FREE.

A chip that has fallen off the bus (a failed reset, a killed hung run) reads
0xffffffff from every register, so its tt_serial comes back as
FFFFFFFFFFFFFFFF. That is non-empty, so the empty-serial check let it through;
it then formed a one-chip board of its own and `status` listed it FREE, where a
one-chip or `--fresh` request could be granted it (tt-orchard, 2026-10-30).
"""
import json

import pytest

from conftest import QUIETBOX
from gozer.cli import main
from gozer.topology import TopologyError, all_chips, read_topology, read_topology_full

DEAD = "FFFFFFFFFFFFFFFF"


def chips_with_dead(index=2):
    chips = [dict(c) for c in QUIETBOX]
    chips[index]["serial"] = DEAD
    chips[index]["card"] = "unknown"
    return chips


def test_all_ones_serial_is_set_aside_not_grouped_into_a_board(sysfs):
    topo = read_topology_full(sysfs(chips_with_dead()))
    assert [c.dev_index for c in topo.unreadable] == [2]
    assert 2 not in [c.dev_index for c in all_chips(topo.boards)]
    assert all(b.serial != DEAD for b in topo.boards)


def test_read_topology_still_returns_only_the_readable_boards(sysfs):
    boards = read_topology(sysfs(chips_with_dead()))
    assert DEAD not in [b.serial for b in boards]


@pytest.mark.parametrize("serial", ["ffffffffffffffff", "FFFFFFFF", "0xffffffff"])
def test_all_ones_in_any_spelling_is_unreadable(sysfs, serial):
    chips = chips_with_dead()
    chips[2]["serial"] = serial
    assert [c.dev_index for c in read_topology_full(sysfs(chips)).unreadable] == [2]


def test_a_serial_that_merely_contains_f_is_readable(sysfs):
    chips = [dict(c) for c in QUIETBOX]
    chips[0]["serial"] = chips[1]["serial"] = "00000000000000FF"
    assert read_topology_full(sysfs(chips)).unreadable == []


def test_every_chip_unreadable_is_still_a_topology_error(sysfs):
    chips = [dict(c, serial=DEAD) for c in QUIETBOX]
    with pytest.raises(TopologyError):
        read_topology_full(sysfs(chips))


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


def test_status_json_marks_the_chip_unreadable(env, capsys):
    code, out = run(["status", "--json"], capsys)
    assert code == 0
    states = {c["dev_index"]: c["state"] for c in json.loads(out)["chips"]}
    assert states[2] == "UNREADABLE"
    assert "FREE" not in [states[2]]
    assert states[0] == states[1] == "FREE"


def test_status_text_says_what_it_means_and_who_to_ask(env, capsys):
    code, out = run(["status"], capsys)
    line = next(l for l in out.splitlines() if "0000:03:00.0" in l)
    assert "UNREADABLE" in line
    assert "FREE" not in line
    assert "reset" in out.lower() and "human" in out.lower()


def test_acquire_never_hands_out_the_unreadable_chip(env, capsys):
    granted = set()
    for _ in range(4):
        code, out = run(["acquire", "--chips", "1", "--who", "t",
                         "--no-queue", "--json"], capsys)
        if code == 0:
            granted |= set(json.loads(out)["chips"])
    assert "0000:03:00.0" not in granted
    assert granted, "control: the readable chips were granted"


def test_fresh_never_hands_out_the_unreadable_chip(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "t", "--fresh",
                     "--no-queue", "--json"], capsys)
    if code == 0:
        assert "0000:03:00.0" not in json.loads(out)["chips"]


@pytest.mark.parametrize("target", ["2", "0000:03:00.0"])
def test_exact_on_the_unreadable_chip_is_refused(env, capsys, target):
    code, out = run(["acquire", "--chips", "1", "--who", "t", "--exact", target,
                     "--no-queue"], capsys)
    assert code != 0
    assert "unreadable" in out.lower()


def test_topology_command_lists_the_unreadable_chip(env, capsys):
    code, out = run(["topology", "--json"], capsys)
    assert code == 0
    assert [c["dev_index"] for c in json.loads(out)["unreadable"]] == [2]
