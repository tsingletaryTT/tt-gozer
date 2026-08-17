"""`gozer acquire --owner-pid N` — a supervisor holding a lease on behalf of
work it manages (tt-station-agentd launching a serving container).

The container's own file descriptors are held by a root-owned process, which
an unprivileged gozer can never see in /proc/<pid>/fd. Passing the
supervisor's own pid through as `acquire`'s `pid` sidesteps that: the lease is
judged by pid_alive(owner_pid) instead of the detached grace window, so it
survives exactly as long as the supervisor does, no fd required.

See docs/superpowers/specs/2026-08-14-tt-gozer-design.md, "Detached leases"
and the new "Supervisor-owned leases" subsection.
"""
import json
import os
import shutil
import pytest
from gozer.cli import main
from gozer.gatekeeper import Gatekeeper
from conftest import QUIETBOX


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    proc = tmp_path / "proc" / "1" / "fd"
    proc.mkdir(parents=True)
    (tmp_path / "proc" / "1" / "comm").write_text("python\n")
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return tmp_path


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def add_live_pid(tmp_path, pid):
    d = tmp_path / "proc" / str(pid) / "fd"
    d.mkdir(parents=True)
    (tmp_path / "proc" / str(pid) / "comm").write_text("python\n")


def backdate_lease(gk, lease_id, since="2000-01-01T00:00:00Z"):
    """Age a lease well past DETACHED_GRACE_SECONDS by rewriting its `since`
    directly on disk -- never by patching the clock, which would also affect
    the reconcile code under test (utcnow()/_elapsed_seconds)."""
    lease = gk.read_lease(lease_id)
    lease["since"] = since
    gk.write_lease(lease)
    for unit in lease.get("units", []):
        gk.update_unit_lease(unit, lease)
    return lease


def test_owner_pid_lease_is_not_detached_and_survives_reconcile_past_the_grace_window(
        env, capsys):
    """A detached lease of the same age would be reaped by DETACHED_GRACE_SECONDS
    (900s); an --owner-pid lease is judged by the owner's liveness instead, so
    the same backdated `since` must not touch it."""
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test",
                     "--owner-pid", "1", "--json"], capsys)
    assert code == 0
    lease_id = json.loads(out)["lease_id"]

    gk = Gatekeeper()
    lease = gk.read_lease(lease_id)
    assert lease["detached"] is False
    assert lease["pid"] == 1
    backdate_lease(gk, lease_id)

    code, out = run(["reconcile", "--json"], capsys)
    assert code == 0
    assert gk.read_lease(lease_id) is not None, "owner-pid lease was reaped"
    states = {c["dev_index"]: c["state"] for c in json.loads(out)["chips"]}
    # pid 1 is alive in the fixture's fake /proc, no fd open -> CLAIMED.
    assert states[lease["dev_indices"][0]] == "CLAIMED"


def test_omitting_owner_pid_still_produces_a_detached_lease_reaped_by_the_grace_window(
        env, capsys):
    """Companion/control for the test above: the exact same backdating, with
    no --owner-pid, must still reap under the pre-existing detached rule."""
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test", "--json"],
                    capsys)
    assert code == 0
    lease_id = json.loads(out)["lease_id"]

    gk = Gatekeeper()
    lease = gk.read_lease(lease_id)
    assert lease["detached"] is True
    backdate_lease(gk, lease_id)

    code, out = run(["reconcile", "--json"], capsys)
    assert code == 0
    assert gk.read_lease(lease_id) is None, "detached lease was not reaped"


def test_owner_pid_lease_is_reaped_once_the_pid_is_gone(env, capsys, tmp_path):
    add_live_pid(tmp_path, 555)
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test",
                     "--owner-pid", "555", "--json"], capsys)
    assert code == 0
    lease_id = json.loads(out)["lease_id"]

    shutil.rmtree(tmp_path / "proc" / "555")  # the supervisor is gone

    code, out = run(["reconcile", "--json"], capsys)
    assert code == 0
    gk = Gatekeeper()
    assert gk.read_lease(lease_id) is None


def test_owner_pid_zero_is_refused_with_a_clear_message(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test",
                     "--owner-pid", "0"], capsys)
    assert code == 12
    assert "positive" in out.lower()
    _, q = run(["queue", "--json"], capsys)
    assert json.loads(q)["queue"] == []


def test_owner_pid_negative_is_refused(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test",
                     "--owner-pid", "-5"], capsys)
    assert code == 12
    assert "positive" in out.lower()


def test_owner_pid_that_is_not_alive_is_refused_with_a_clear_message(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test",
                     "--owner-pid", "999999"], capsys)
    assert code == 12
    assert "not alive" in out.lower()
    _, q = run(["queue", "--json"], capsys)
    assert json.loads(q)["queue"] == []
    _, status = run(["status", "--json"], capsys)
    assert {c["state"] for c in json.loads(status)["chips"]} == {"FREE"}


def test_acquire_json_reports_the_owner_pid(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test",
                     "--owner-pid", "1", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["owner_pid"] == 1


def test_acquire_json_reports_owner_pid_null_when_omitted(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test", "--json"],
                    capsys)
    assert code == 0
    assert json.loads(out)["owner_pid"] is None
