# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""status and reconcile say when a lease's owner is dead but a device is held.

After a suspend the supervisor's pid was gone while its container was still
running on the chips; `reconcile` cleared the lease and said nothing about it
(tt-orchard). A holder gozer can see is named, with its container when the
cgroup says so. A container owned by another user is invisible to an
unprivileged scan, so the output says that rather than implying the chip is
idle.
"""
import json
import os
import shutil

import pytest

from conftest import QUIETBOX, fake_proc_tree
from gozer import procfd
from gozer.cli import main

CID = "ab12cd34ef56" * 5 + "ab12"      # 64 hex characters


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    proc = fake_proc_tree(tmp_path, {555: {"ppid": 1}})
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", proc)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return tmp_path


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def lease_with_owner(capsys, pid=555):
    code, out = run(["acquire", "--chips", "1", "--who", "t", "--owner-pid",
                     str(pid), "--json"], capsys)
    assert code == 0
    return json.loads(out)


def add_holder(tmp_path, pid, dev, cgroup=None):
    fake_proc_tree(tmp_path, {pid: {"ppid": 1, "devs": [dev]}})
    if cgroup:
        (tmp_path / "proc" / str(pid) / "cgroup").write_text(cgroup)


def kill_owner(tmp_path, pid=555):
    shutil.rmtree(tmp_path / "proc" / str(pid))


def test_container_id_is_read_from_the_cgroup(tmp_path):
    root = fake_proc_tree(tmp_path, {9: {}})
    (tmp_path / "proc" / "9" / "cgroup").write_text(
        f"0::/system.slice/docker-{CID}.scope\n")
    assert procfd.container_id(9, root) == CID[:12]


@pytest.mark.parametrize("line", ["0::/user.slice/session-3.scope\n", ""])
def test_a_process_outside_any_container_has_no_container_id(tmp_path, line):
    root = fake_proc_tree(tmp_path, {9: {}})
    (tmp_path / "proc" / "9" / "cgroup").write_text(line)
    assert procfd.container_id(9, root) is None


def test_a_missing_cgroup_file_is_no_container(tmp_path):
    assert procfd.container_id(9, fake_proc_tree(tmp_path, {9: {}})) is None


def test_status_names_the_holder_and_container_of_a_dead_owners_lease(
        env, capsys):
    grant = lease_with_owner(capsys)
    dev = grant["dev_indices"][0]
    add_holder(env, 777, dev, f"0::/system.slice/docker-{CID}.scope\n")
    kill_owner(env)
    code, out = run(["status", "--json"], capsys)
    chip = next(c for c in json.loads(out)["chips"] if c["dev_index"] == dev)
    assert chip["state"] == "HELD-FOREIGN"
    assert chip["owner_dead"] is True
    assert chip["holder_containers"] == {"777": CID[:12]}
    code, text = run(["status"], capsys)
    line = next(l for l in text.splitlines() if f"chip {dev} " in l)
    assert "owner pid 555 is dead" in line
    assert "777" in line and CID[:12] in line


def test_a_live_owner_is_not_reported_dead(env, capsys):
    grant = lease_with_owner(capsys)
    code, out = run(["status", "--json"], capsys)
    assert all(c["owner_dead"] is False for c in json.loads(out)["chips"])


def test_a_stale_lease_whose_owner_died_warns_that_other_users_are_invisible(
        env, capsys):
    grant = lease_with_owner(capsys)
    kill_owner(env)
    code, text = run(["status"], capsys)
    assert "STALE" in text
    assert "owner pid 555 is dead" in text
    assert "not visible" in text and "--sudo" in text


def test_a_detached_stale_lease_gets_no_owner_warning(env, capsys):
    run(["acquire", "--chips", "1", "--who", "t"], capsys)
    code, out = run(["status", "--json"], capsys)
    assert all(c["owner_dead"] is False for c in json.loads(out)["chips"])


def test_reconcile_warns_when_it_reaps_a_lease_whose_owner_died(env, capsys):
    lease_with_owner(capsys)
    kill_owner(env)
    code, out = run(["reconcile"], capsys)
    assert code == 0
    assert "reaped" in out and "owner pid 555 is dead" in out
    assert "not visible" in out and "--sudo" in out


def test_reconcile_with_nothing_to_reap_prints_no_warning(env, capsys):
    lease_with_owner(capsys)
    code, out = run(["reconcile"], capsys)
    assert "reaped" not in out and "owner pid" not in out


def test_reconcile_json_lists_the_reaped_leases(env, capsys):
    grant = lease_with_owner(capsys)
    kill_owner(env)
    code, out = run(["reconcile", "--json"], capsys)
    reaped = json.loads(out)["reaped"]
    assert [r["lease_id"] for r in reaped] == [grant["lease_id"]]
    assert reaped[0]["owner_pid"] == 555
