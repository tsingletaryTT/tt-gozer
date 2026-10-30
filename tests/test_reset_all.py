# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""`gozer reset-all`: a leased, host-wide `tt-smi -r all`.

It takes a lease on every unit first, so it can only run when the whole box is
free, and it is the one reset path agents may use for a chip that stays
unreadable after a per-chip reset failed.
"""
import json
import os
import stat

import pytest

from conftest import QUIETBOX
from gozer.cli import main
from gozer.gatekeeper import Gatekeeper

DEAD = "FFFFFFFFFFFFFFFF"


def dead_chip2():
    chips = [dict(c) for c in QUIETBOX]
    chips[2]["serial"] = DEAD
    chips[2]["card"] = "unknown"
    return chips


def script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def box(tmp_path, sysfs, monkeypatch):
    (tmp_path / "proc" / "1" / "fd").mkdir(parents=True)
    (tmp_path / "proc" / "1" / "comm").write_text("python\n")
    sys_root = sysfs(dead_chip2())
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sys_root)
    monkeypatch.setenv("GOZER_PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return tmp_path, sys_root


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def healing_reset(tmp_path, sys_root, log):
    """A reset that records its argv and the gate state, then brings chip 2 back."""
    chip2 = os.path.join(sys_root, "tenstorrent!2")
    return script(tmp_path / "heal.sh", f'''
echo "$@" >> {log}
ls "$GOZER_ROOT/gate" >> {log}
echo 0000000000000001 > {chip2}/tt_serial
echo p300c > {chip2}/tt_card_type
''')


def test_reset_all_runs_tt_smi_reset_all_under_a_lease_on_every_unit(
        box, capsys, monkeypatch):
    tmp_path, sys_root = box
    log = tmp_path / "log"
    monkeypatch.setenv("GOZER_RESET_CMD", healing_reset(tmp_path, sys_root, log))
    code, out = run(["reset-all", "--who", "claude:t", "--json"], capsys)
    assert code == 0, out
    lines = log.read_text().split("\n")
    assert lines[0] == "-r all"
    # while it ran, both units (the healthy board and the lone partner) were locked
    assert sum(1 for l in lines[1:] if l.endswith(".lock")) == 2
    assert Gatekeeper().held_units() == {}
    assert Gatekeeper().all_leases() == []


def test_reset_all_that_heals_the_chip_marks_every_unit_clean(
        box, capsys, monkeypatch):
    tmp_path, sys_root = box
    monkeypatch.setenv("GOZER_RESET_CMD",
                       healing_reset(tmp_path, sys_root, tmp_path / "log"))
    code, out = run(["reset-all", "--who", "claude:t", "--json"], capsys)
    payload = json.loads(out)
    assert payload["status"] == "reset" and payload["unreadable"] == []
    gk = Gatekeeper(proc_root=str(tmp_path / "proc"))
    assert all(gk.is_clean(u) for u in gk.all_units())
    code, out = run(["status", "--json"], capsys)
    assert {c["state"] for c in json.loads(out)["chips"]} == {"FREE"}


def test_reset_all_that_leaves_the_chip_dead_exits_20_and_says_which(
        box, capsys):
    tmp_path, _ = box
    code, out = run(["reset-all", "--who", "claude:t", "--json"], capsys)
    payload = json.loads(out)
    assert code == 20
    assert payload["status"] == "reset-incomplete"
    assert payload["unreadable"] == ["0000:03:00.0"]
    assert "power" in payload["message"].lower()
    assert Gatekeeper().held_units() == {}            # lease released regardless
    gk = Gatekeeper(proc_root=str(tmp_path / "proc"))
    # the partner board must NOT be marked clean while its sibling is dead
    degraded = [u for u in gk.all_units() if u == "0000000000000001"]
    assert degraded and not gk.is_clean(degraded[0])


def test_reset_all_refuses_and_runs_nothing_while_a_unit_is_held(
        box, capsys, monkeypatch):
    tmp_path, _ = box
    marker = tmp_path / "ran"
    monkeypatch.setenv("GOZER_RESET_CMD", script(tmp_path / "r.sh",
                                                f"touch {marker}\n"))
    code, out = run(["acquire", "--chips", "1", "--who", "other", "--json"], capsys)
    assert code == 0
    code, out = run(["reset-all", "--who", "claude:t", "--json"], capsys)
    assert code == 12
    assert json.loads(out)["status"] == "busy"
    assert not marker.exists()
    assert "other" in json.loads(out)["message"]


def test_reset_all_refuses_while_an_unreadable_chips_device_is_open(
        box, capsys, monkeypatch):
    tmp_path, _ = box
    marker = tmp_path / "ran"
    monkeypatch.setenv("GOZER_RESET_CMD", script(tmp_path / "r.sh",
                                                f"touch {marker}\n"))
    fd_dir = tmp_path / "proc" / "777" / "fd"
    fd_dir.mkdir(parents=True)
    (tmp_path / "proc" / "777" / "comm").write_text("python\n")
    (tmp_path / "proc" / "777" / "status").write_text("Name:\tpython\nPPid:\t1\n")
    os.symlink("/dev/tenstorrent/2", fd_dir / "3")
    code, out = run(["reset-all", "--who", "claude:t", "--json"], capsys)
    assert code == 12 and not marker.exists()
    assert "777" in json.loads(out)["message"]


def test_a_failing_tt_smi_exits_17_and_still_releases_the_lease(
        box, capsys, monkeypatch):
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/false")
    code, out = run(["reset-all", "--who", "claude:t", "--json"], capsys)
    assert code == 17
    assert json.loads(out)["status"] == "failed"
    assert Gatekeeper().held_units() == {}


def test_reset_all_is_recorded_in_history(box, capsys):
    run(["reset-all", "--who", "claude:t"], capsys)
    code, out = run(["history", "--json"], capsys)
    events = [e["event"] for e in json.loads(out)["history"]]
    assert "reset_all" in events
