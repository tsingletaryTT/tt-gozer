# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""`release` says why it did what it did.

A release whose reset failed still tears the lease down, and used to exit 0
with the failure buried in prose. Callers (tt-orchard) could not tell it from
a clean release and left an unreset chip for the next tenant. The outcome is
now a structured status, a distinct exit code (19) and the exact recovery
commands. `--fresh` already refuses a unit whose reset failed; a test pins it.
"""
import json

import pytest

from conftest import QUIETBOX
from gozer.cli import main
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster


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


def test_clean_release_exits_zero_with_status_released(env, capsys):
    lease = acquire(capsys)
    code, out = run(["release", lease, "--json"], capsys)
    assert code == 0
    assert json.loads(out)["status"] == "released"


def test_failed_reset_exits_19_status_reset_failed_and_prints_recovery(
        env, capsys, monkeypatch):
    lease = acquire(capsys)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/false")
    code, out = run(["release", lease, "--json"], capsys)
    payload = json.loads(out)
    assert code == 19
    assert payload["released"] is True          # the lease is gone
    assert payload["status"] == "reset-failed"
    assert payload["units"]
    # The recovery is the documented acquire-then-release pair, per unit.
    assert all(f"gozer acquire --exact {u}" in payload["recovery"]
               for u in payload["units"])
    assert "gozer release" in payload["recovery"]
    assert Gatekeeper().all_leases() == []


def test_failed_reset_human_output_includes_the_recovery_command(
        env, capsys, monkeypatch):
    lease = acquire(capsys)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/false")
    code, out = run(["release", lease], capsys)
    assert code == 19
    assert "recover" in out.lower() and "gozer acquire --exact" in out


def test_unknown_lease_is_status_not_found_exit_13(env, capsys):
    code, out = run(["release", "nope", "--json"], capsys)
    assert code == 13
    assert json.loads(out)["status"] == "not-found"


def test_a_message_containing_not_found_is_not_taken_for_a_missing_lease(
        env, capsys, monkeypatch):
    """The old exit-code choice matched the substring "not found" in prose,
    and the message can carry tt-smi output."""
    lease = acquire(capsys)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/false")
    monkeypatch.setattr(
        "gozer.reset.reset_chips",
        lambda *a, **k: (False, "tt-smi: device not found"))
    code, _ = run(["release", lease], capsys)
    assert code == 19


def test_no_reset_release_is_status_released_and_exit_zero(env, capsys):
    lease = acquire(capsys)
    code, out = run(["release", lease, "--no-reset", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["status"] == "released"


def test_fresh_refuses_a_unit_whose_release_reset_failed(
        env, capsys, monkeypatch):
    def gk():
        return Gatekeeper(proc_root=str(env / "proc"))

    # A good release marks the unit clean, so --fresh may take it ...
    lease = acquire(capsys)
    run(["release", lease], capsys)
    assert gk().allocate(1, 1, fresh=True), "control: a reset unit is fresh"

    # ... and a release whose reset failed must not.
    lease = acquire(capsys)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/false")
    code, out = run(["release", lease, "--json"], capsys)
    unit = json.loads(out)["units"][0]
    assert code == 19
    assert not gk().is_clean(unit)
    assert gk().allocate(1, 1, fresh=True) is None
