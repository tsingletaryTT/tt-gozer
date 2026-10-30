# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""A queued `acquire --owner-pid N` keeps N when `gozer wait` grants it.

Before this, the ticket recorded nothing about the owner pid, so `wait` made
a detached lease (or, for a supervisor, one that went stale once the waiter
exited) and the chips looked free while the work was still running.
"""
import json

import pytest

from conftest import QUIETBOX
from gozer.cli import main
from gozer.gatekeeper import Gatekeeper


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    for pid in (1, 555):
        d = tmp_path / "proc" / str(pid) / "fd"
        d.mkdir(parents=True)
        (tmp_path / "proc" / str(pid) / "comm").write_text("python\n")
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return tmp_path


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def fill_board_then_queue(capsys, *extra):
    """Take every chip, then queue a second request. Returns (holder, ticket)."""
    code, out = run(["acquire", "--chips", "all", "--who", "holder", "--json"],
                    capsys)
    assert code == 0
    holder = json.loads(out)["lease_id"]
    code, out = run(["acquire", "--chips", "1", "--who", "queued", "--json",
                     *extra], capsys)
    assert code == 10
    return holder, json.loads(out)["ticket"]


def wait_for_grant(env, capsys, holder, ticket):
    code, _ = run(["release", holder], capsys)
    assert code == 0
    code, out = run(["wait", ticket, "--timeout", "10s", "--json"], capsys)
    assert code == 0
    return Gatekeeper().read_lease(json.loads(out)["lease_id"])


def test_wait_grants_under_the_owner_pid_the_ticket_was_queued_with(env, capsys):
    holder, ticket = fill_board_then_queue(capsys, "--owner-pid", "555")
    lease = wait_for_grant(env, capsys, holder, ticket)
    assert lease["pid"] == 555
    assert lease["detached"] is False


def test_wait_without_owner_pid_still_grants_a_detached_lease(env, capsys):
    holder, ticket = fill_board_then_queue(capsys)
    lease = wait_for_grant(env, capsys, holder, ticket)
    assert lease["detached"] is True


def test_wait_grant_reports_the_owner_pid(env, capsys):
    holder, ticket = fill_board_then_queue(capsys, "--owner-pid", "555")
    run(["release", holder], capsys)
    code, out = run(["wait", ticket, "--timeout", "10s", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["owner_pid"] == 555


def test_wait_with_a_dead_owner_pid_falls_back_to_detached_and_says_so(
        env, capsys):
    """A supervisor that died while queued must not mint a lease reapable the
    instant it exists, and must not strand the ticket. The grant is detached
    (the grace window then reaps it) and the output names the dead pid."""
    import shutil
    holder, ticket = fill_board_then_queue(capsys, "--owner-pid", "555")
    shutil.rmtree(env / "proc" / "555")
    run(["release", holder], capsys)
    code, out = run(["wait", ticket, "--timeout", "10s"], capsys)
    assert code == 0
    assert "555" in out and "not alive" in out
    leases = Gatekeeper().all_leases()
    assert len(leases) == 1 and leases[0]["detached"] is True
