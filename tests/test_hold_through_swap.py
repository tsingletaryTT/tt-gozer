"""The supervisor's sequence, as the CLI sees it.

A supervisor takes a lease under its own pid, runs a model server as its child,
stops it, waits until gozer shows the board CLAIMED, resets the board in place,
runs something else, resets again, starts the server again, and finally
releases. This follows skills/gozer-park/SKILL.md step by step. The lease must
survive every step, including a backdated `since` while no device is open,
which is the case that reaps a lease taken without an owner pid.

The fake /proc is built before the lease is taken but device holders are added
only after it. A holder that already has a device open makes that board look
busy, and the acquire would move to the other board.
"""
import json
import os
import shutil
import stat

import pytest

from conftest import QUIETBOX, fake_proc_tree
from gozer.cli import main
from gozer.gatekeeper import Gatekeeper

OWNER = 5_000_100
OTHER_OWNER = 5_000_150
SERVER = 5_000_200
TEST_RUN = 5_000_300
OUTSIDER = 5_000_900


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    marker = tmp_path / "resets"
    script = tmp_path / "reset.sh"
    script.write_text(f"#!/bin/sh\necho reset >> {marker}\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", fake_proc_tree(
        tmp_path, {OWNER: {"ppid": 1}, OTHER_OWNER: {"ppid": 1}}))
    monkeypatch.setenv("GOZER_RESET_CMD", str(script))
    return tmp_path, marker


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def states(capsys, command="status"):
    """Chip states by device index. `status` only reads; `reconcile` also reaps."""
    argv = ["status", "--json"] if command == "status" else ["reconcile", "--json"]
    code, out = run(argv, capsys)
    assert code == 0
    return {c["dev_index"]: c["state"] for c in json.loads(out)["chips"]}


def start_process(tmp_path, pid, ppid, dev=0):
    fake_proc_tree(tmp_path, {pid: {"ppid": ppid, "devs": [dev]}})


def stop_process(tmp_path, pid):
    shutil.rmtree(tmp_path / "proc" / str(pid))


def resets(marker):
    return marker.read_text().count("reset") if marker.exists() else 0


def backdate(lease_id, since="2000-01-01T00:00:00Z"):
    gk = Gatekeeper()
    lease = gk.read_lease(lease_id)
    lease["since"] = since
    gk.write_lease(lease)
    for unit in lease["units"]:
        gk.update_unit_lease(unit, lease)


def acquire(capsys, *extra):
    code, out = run(["acquire", "--chips", "1", "--who", "orchard:test",
                     "--json", *extra], capsys)
    return code, json.loads(out)


def test_the_lease_survives_a_full_swap_and_releases_at_the_end(env, capsys):
    tmp_path, marker = env
    code, grant = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    lease_id = grant["lease_id"]

    start_process(tmp_path, SERVER, OWNER)                 # the server starts as the owner's child
    assert states(capsys)[0] == "HELD"

    code, _ = run(["reset", lease_id], capsys)             # refused: the server still has the device
    assert code == 15 and resets(marker) == 0

    stop_process(tmp_path, SERVER)                         # the server is stopped
    backdate(lease_id)                                     # long past the 900 s detached window
    assert states(capsys)[0] == "CLAIMED"                  # skill: wait for CLAIMED before resetting
    assert states(capsys, "reconcile")[0] == "CLAIMED"     # the reaping path leaves it alone too
    assert Gatekeeper().read_lease(lease_id) is not None

    code, _ = run(["reset", lease_id], capsys)             # reset in place
    assert code == 0 and resets(marker) == 1
    assert Gatekeeper().read_lease(lease_id) is not None

    start_process(tmp_path, TEST_RUN, OWNER)               # the other work runs on the same lease
    assert states(capsys)[0] == "HELD"
    stop_process(tmp_path, TEST_RUN)
    assert states(capsys)[0] == "CLAIMED"                  # confirm it stopped before the second reset

    code, _ = run(["reset", lease_id], capsys)             # clean the chips after the other work
    assert code == 0 and resets(marker) == 2
    assert Gatekeeper().read_lease(lease_id) is not None

    start_process(tmp_path, SERVER, OWNER)                 # the server comes back
    assert states(capsys)[0] == "HELD"
    stop_process(tmp_path, SERVER)

    code, _ = run(["release", lease_id], capsys)
    assert code == 0
    assert Gatekeeper().read_lease(lease_id) is None
    assert resets(marker) == 3                             # release resets too
    assert states(capsys)[0] == "FREE"


def test_control_a_lease_without_an_owner_pid_is_reaped_in_the_same_gap(env, capsys):
    """Why the owner pid matters: the same swap, with a bare acquire, loses the
    lease as soon as a reconcile runs during the gap with no device open."""
    code, grant = acquire(capsys)
    assert code == 0
    lease_id = grant["lease_id"]
    backdate(lease_id)
    assert states(capsys, "reconcile")[0] == "FREE"
    assert Gatekeeper().read_lease(lease_id) is None


def test_a_fresh_bare_lease_survives_the_gap_inside_the_grace_window(env, capsys):
    """Pins the other side of the control: the bare lease is only lost after
    the window, so the control above reaps only because the window has passed."""
    code, grant = acquire(capsys)
    assert code == 0
    assert states(capsys, "reconcile")[0] == "CLAIMED"
    assert Gatekeeper().read_lease(grant["lease_id"]) is not None


def test_the_alarm_still_fires_for_a_holder_outside_the_owners_tree(env, capsys):
    tmp_path, _ = env
    code, _ = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    start_process(tmp_path, OUTSIDER, 1)                   # not a descendant of the owner
    assert states(capsys)[0] == "HELD-FOREIGN"


def test_a_grandchild_of_the_owner_counts_as_the_owners_own_work(env, capsys):
    """A server launched through a shell wrapper is a grandchild of the owner."""
    tmp_path, _ = env
    code, _ = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    fake_proc_tree(tmp_path, {SERVER: {"ppid": OWNER}})
    start_process(tmp_path, TEST_RUN, SERVER)
    assert states(capsys)[0] == "HELD"


def test_a_dead_owner_with_no_device_open_is_stale_and_gets_reaped(env, capsys):
    """The owner pid is the lease's lifeline. When the supervisor dies the board
    comes back, so a crashed run does not hold a board forever."""
    tmp_path, _ = env
    code, grant = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    stop_process(tmp_path, OWNER)
    assert states(capsys)[0] == "STALE"                    # status reports, does not reap
    assert Gatekeeper().read_lease(grant["lease_id"]) is not None
    assert states(capsys, "reconcile")[0] == "FREE"
    assert Gatekeeper().read_lease(grant["lease_id"]) is None


def test_reset_refuses_while_the_servers_device_is_open_and_keeps_the_lease(env, capsys):
    tmp_path, marker = env
    code, grant = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    start_process(tmp_path, SERVER, OWNER)
    code, out = run(["reset", grant["lease_id"], "--json"], capsys)
    assert code == 15
    assert "still open" in json.loads(out)["message"]
    assert resets(marker) == 0
    assert Gatekeeper().read_lease(grant["lease_id"]) is not None


def test_release_refuses_while_the_servers_device_is_open(env, capsys):
    tmp_path, marker = env
    code, grant = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    start_process(tmp_path, SERVER, OWNER)
    code, _ = run(["release", grant["lease_id"]], capsys)
    assert code == 15
    assert resets(marker) == 0
    assert Gatekeeper().read_lease(grant["lease_id"]) is not None


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root can list a mode-0 directory, so the server would stay visible")
def test_a_container_server_gozer_cannot_see_does_not_stop_reset(env, capsys):
    """KNOWN LIMIT, pinned on purpose. gozer reads /proc/<pid>/fd as the calling
    user, so a device handle held by a root-owned container process is
    invisible to it. The server below is alive and holds the device. Its fd
    directory is then made unreadable (mode 0), which is what an unprivileged
    gozer meets for a root-owned process. Before the chmod the server is
    visible, so `status` shows HELD and `reset` refuses. After it `status`
    shows CLAIMED and `reset` goes ahead. The supervisor has to confirm the
    stop itself (tt-model ps, docker ps) before it resets. The "confirm the
    workload has stopped" section of skills/gozer-park/SKILL.md warns about
    this.

    This test pins a known limit. Revisit it if gozer ever refuses or warns for
    an unreadable fd directory, or gains root-visible scanning."""
    tmp_path, marker = env
    code, grant = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    lease_id = grant["lease_id"]
    start_process(tmp_path, SERVER, OWNER)
    fd_dir = tmp_path / "proc" / str(SERVER) / "fd"

    assert states(capsys)[0] == "HELD"                     # visible first
    code, _ = run(["reset", lease_id], capsys)
    assert code == 15 and resets(marker) == 0

    os.chmod(fd_dir, 0)
    try:
        assert states(capsys)[0] == "CLAIMED"              # present but invisible
        code, _ = run(["reset", lease_id], capsys)
        assert code == 0 and resets(marker) == 1           # gozer does not refuse
    finally:
        os.chmod(fd_dir, 0o700)                            # let pytest clean the tree


def test_a_queued_supervisor_retries_with_its_ticket_and_gets_an_owned_lease(env, capsys):
    """The skill's ticket guidance: a third acquire with both boards held exits
    10 with a ticket, and after a release the same acquire with --ticket grants
    a lease that has the owner pid recorded and is not detached. `gozer wait`
    is not used, because it grants without an owner pid.

    The claim window is 90 s of wall clock, so the test does not wait it out.
    It checks the deterministic path, a retry by the head ticket right after
    the release, and that a retry without the ticket does not jump the queue."""
    code, first = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    code, second = acquire(capsys)
    assert code == 0
    gk = Gatekeeper()
    assert not set(gk.read_lease(first["lease_id"])["units"]) & set(
        gk.read_lease(second["lease_id"])["units"])         # one board each

    code, queued = acquire(capsys, "--owner-pid", str(OTHER_OWNER))
    assert code == 10 and queued["queued"] is True
    ticket = queued["ticket"]

    code, again = acquire(capsys, "--ticket", ticket, "--owner-pid", str(OTHER_OWNER))
    assert code == 10 and again["ticket"] == ticket        # still busy, same ticket

    code, _ = run(["release", first["lease_id"]], capsys)
    assert code == 0

    code, jumped = acquire(capsys, "--owner-pid", str(OWNER))   # no ticket: must not jump the queue
    assert code == 10 and jumped["ticket"] != ticket
    code, _ = run(["cancel", jumped["ticket"]], capsys)
    assert code == 0

    code, granted = acquire(capsys, "--ticket", ticket, "--owner-pid", str(OTHER_OWNER))
    assert code == 0 and granted["granted"] is True
    lease = Gatekeeper().read_lease(granted["lease_id"])
    assert lease["pid"] == OTHER_OWNER
    assert not lease.get("detached")
    assert granted["owner_pid"] == OTHER_OWNER


def test_the_owner_holding_the_device_itself_is_its_own_work(env, capsys):
    """A supervisor that opens the device in its own process (not a child) is
    still the owner. Without the owner-equals-holder rule it would look foreign,
    because a process is not its own descendant."""
    tmp_path, _ = env
    code, _ = acquire(capsys, "--owner-pid", str(OWNER))
    assert code == 0
    fake_proc_tree(tmp_path, {OWNER: {"ppid": 1, "devs": [0]}})
    assert states(capsys)[0] == "HELD"


def test_owner_pid_1_does_not_make_every_holder_the_owners_own(env, capsys):
    """Every process descends from pid 1, so counting descendants of pid 1 would
    hide every foreign holder. The alarm must still fire."""
    tmp_path, _ = env
    fake_proc_tree(tmp_path, {1: {"ppid": 0}})
    code, _ = acquire(capsys, "--owner-pid", "1")
    assert code == 0
    start_process(tmp_path, OUTSIDER, 1)
    assert states(capsys)[0] == "HELD-FOREIGN"
