# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""`gozer renew <lease>` is a heartbeat for a detached lease, and `release`
says what became of a lease that is gone.

A detached lease is reaped 900 s after `since` if no device is open. Work that
runs longer without opening the device (a long compile, a CPU phase) lost its
chips to another agent, and `release` then said only "not found" (tt-orchard).
"""
import json

import pytest

from conftest import QUIETBOX
from gozer.cli import main
from gozer.gatekeeper import Gatekeeper


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    (tmp_path / "proc" / "1" / "fd").mkdir(parents=True)
    (tmp_path / "proc" / "1" / "comm").write_text("python\n")
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return tmp_path


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def acquire(capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "t", "--json"], capsys)
    assert code == 0
    return json.loads(out)["lease_id"]


def backdate(lease_id, since="2000-01-01T00:00:00Z", **extra):
    gk = Gatekeeper()
    lease = gk.read_lease(lease_id)
    lease["since"] = since
    lease.update(extra)
    gk.write_lease(lease)
    for unit in lease["units"]:
        gk.update_unit_lease(unit, lease)


def test_a_bare_renew_exits_zero_and_stamps_renewed_at(env, capsys):
    lease = acquire(capsys)
    code, out = run(["renew", lease, "--json"], capsys)
    assert code == 0
    assert json.loads(out)["renewed"] is True
    assert Gatekeeper().read_lease(lease)["renewed_at"]


def test_a_renewed_detached_lease_survives_past_the_grace_window(env, capsys):
    lease = acquire(capsys)
    backdate(lease)
    run(["renew", lease], capsys)
    run(["reconcile"], capsys)
    assert Gatekeeper().read_lease(lease) is not None


def test_without_a_renew_the_same_backdated_lease_is_reaped(env, capsys):
    lease = acquire(capsys)
    backdate(lease)
    run(["reconcile"], capsys)
    assert Gatekeeper().read_lease(lease) is None


def test_a_stale_renewal_does_not_save_a_lease(env, capsys):
    lease = acquire(capsys)
    backdate(lease, renewed_at="2000-01-02T00:00:00Z")
    run(["reconcile"], capsys)
    assert Gatekeeper().read_lease(lease) is None


def test_renew_with_expect_still_extends_the_deadline_and_stamps_renewed_at(
        env, capsys):
    lease = acquire(capsys)
    code, out = run(["renew", lease, "--expect", "2h", "--json"], capsys)
    assert code == 0 and json.loads(out)["expect_done"]
    assert Gatekeeper().read_lease(lease)["renewed_at"]


def test_renew_of_an_unknown_lease_is_exit_13(env, capsys):
    code, _ = run(["renew", "nope"], capsys)
    assert code == 13


def test_release_of_a_reaped_lease_says_it_was_reaped_and_when(env, capsys):
    lease = acquire(capsys)
    backdate(lease)
    run(["reconcile"], capsys)
    code, out = run(["release", lease, "--json"], capsys)
    payload = json.loads(out)
    assert code == 13 and payload["status"] == "not-found"
    assert "reaped" in payload["message"]
    assert payload["reaped_at"] and payload["reaped_at"] in payload["message"]
    assert "grace window" in payload["message"]


def test_release_of_an_already_released_lease_says_so(env, capsys):
    lease = acquire(capsys)
    run(["release", lease], capsys)
    code, out = run(["release", lease, "--json"], capsys)
    payload = json.loads(out)
    assert code == 13
    assert "already released" in payload["message"]


def test_release_of_a_lease_that_never_existed_is_unchanged(env, capsys):
    code, out = run(["release", "nope", "--json"], capsys)
    assert code == 13
    assert json.loads(out)["message"] == "lease nope not found"
