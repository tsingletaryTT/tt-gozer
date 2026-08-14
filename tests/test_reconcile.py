import os
import pytest
from gozer.gatekeeper import Gatekeeper, utcnow
from conftest import QUIETBOX


def fake_proc(tmp_path, pids):
    root = tmp_path / "proc"
    for pid, devs in pids.items():
        d = root / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text("python\n")
        for i, dev in enumerate(devs):
            os.symlink(f"/dev/tenstorrent/{dev}", d / "fd" / str(i + 3))
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def make(tmp_path, sysfs, pids=None):
    proc = fake_proc(tmp_path, pids or {})
    return Gatekeeper(root=str(tmp_path / "state"),
                      sysfs_root=sysfs(QUIETBOX), proc_root=proc)


def states(gk):
    return {s.chip.dev_index: s.state for s in gk.reconcile()}


def lease_for(gk, chips, pid, lease_id="aaa", expect_done=None,
              detached=False, since=None):
    return {
        "lease_id": lease_id, "who": "claude:test", "pid": pid, "pgid": pid,
        "chips": [c for c in chips], "since": since or utcnow(),
        "expect_done": expect_done, "state": "active", "detached": detached,
    }


def test_free_when_no_lease_and_no_fd(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert states(gk) == {0: "FREE", 1: "FREE", 2: "FREE", 3: "FREE"}


def test_busy_untracked_when_fd_but_no_lease(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, pids={861926: [0, 1, 2, 3]})
    assert states(gk) == {i: "BUSY-UNTRACKED" for i in range(4)}


def test_held_when_lease_pid_holds_the_fd(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, pids={4242: [0, 1]})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    s = states(gk)
    assert s[0] == "HELD" and s[1] == "HELD"
    assert s[2] == "FREE"


def test_held_foreign_when_another_pid_holds_it(tmp_path, sysfs):
    # Lease says pid 4242, but pid 9999 is the one with the device open.
    gk = make(tmp_path, sysfs, pids={4242: [], 9999: [0, 1]})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    s = states(gk)
    assert s[0] == "HELD-FOREIGN" and s[1] == "HELD-FOREIGN"


def test_claimed_when_pid_alive_but_no_fd_yet(tmp_path, sysfs):
    # Setup phase: leased, process running, device not opened yet.
    gk = make(tmp_path, sysfs, pids={4242: []})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    s = states(gk)
    assert s[0] == "CLAIMED" and s[1] == "CLAIMED"


def test_stale_is_reaped_when_pid_dead_and_no_fd(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)  # pid 4242 does not exist in the fake proc
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    result = {s.chip.dev_index: s.state for s in gk.reconcile(reap=True)}
    assert result[0] == "FREE" and result[1] == "FREE"
    assert gk.unit_lease("0000000000000002") is None
    assert gk.read_lease("aaa") is None


def test_stale_is_reported_but_not_reaped_when_reap_false(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    result = {s.chip.dev_index: s.state for s in gk.reconcile(reap=False)}
    assert result[0] == "STALE"
    assert gk.unit_lease("0000000000000002") is not None


def test_live_pid_past_expect_done_is_overstayed_not_reaped(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, pids={4242: [0, 1]})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242,
                      expect_done="2000-01-01T00:00:00Z")
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    by_idx = {s.chip.dev_index: s for s in gk.reconcile()}
    assert by_idx[0].state == "HELD"
    assert by_idx[0].overstayed is True
    assert gk.unit_lease("0000000000000002") is not None


def test_detached_lease_with_no_fd_freshly_created_is_claimed_not_reaped(tmp_path, sysfs):
    # This is the bug the coordinator confirmed: a bare `gozer acquire` has no
    # process gozer can supervise, and the CLI process that created the lease
    # is typically already gone by the next reconcile. A detached lease must
    # not be judged by pid_alive at all -- pid 4242 here is deliberately dead
    # (not registered in the fake /proc), yet the lease must still survive,
    # because it is brand new (`since` is "now").
    gk = make(tmp_path, sysfs)  # pid 4242 does not exist in the fake proc
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242, detached=True)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    result = {s.chip.dev_index: s.state for s in gk.reconcile(reap=True)}
    assert result[0] == "CLAIMED" and result[1] == "CLAIMED"
    assert gk.unit_lease("0000000000000002") is not None
    assert gk.read_lease("aaa") is not None


def test_detached_lease_held_by_an_unrelated_pid_is_held_not_foreign(tmp_path, sysfs):
    # The recorded pid on a detached lease is purely informational -- the
    # eventual workload's real pid could never have been known at acquire
    # time -- so a holder that doesn't match it is expected, not foreign.
    gk = make(tmp_path, sysfs, pids={4242: [], 9999: [0, 1]})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242, detached=True)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    s = states(gk)
    assert s[0] == "HELD" and s[1] == "HELD"


def test_detached_lease_past_the_grace_window_is_stale_and_reaped(tmp_path, sysfs):
    # Backdate `since` directly into the lease record rather than sleeping or
    # patching the clock -- patching the clock would also affect
    # _elapsed_seconds/utcnow() inside the code under test.
    gk = make(tmp_path, sysfs)
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242,
                      detached=True, since="2000-01-01T00:00:00Z")
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    result = {s.chip.dev_index: s.state for s in gk.reconcile(reap=True)}
    assert result[0] == "FREE" and result[1] == "FREE"
    assert gk.unit_lease("0000000000000002") is None
    assert gk.read_lease("aaa") is None


def test_non_detached_lease_held_by_a_different_pid_is_still_held_foreign(tmp_path, sysfs):
    # Proves the detached branch narrowed HELD-FOREIGN rather than removing
    # it: a `gozer run`-style lease (detached=False) really does know who
    # should hold its chip, so a mismatched holder is still reported loudly.
    gk = make(tmp_path, sysfs, pids={4242: [], 9999: [0, 1]})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242, detached=False)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    s = states(gk)
    assert s[0] == "HELD-FOREIGN" and s[1] == "HELD-FOREIGN"


def test_grain_and_unit_key(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.grain == "board"
    chip = gk.boards[0].chips[0]
    assert gk.unit_key_for(chip, "board") == chip.serial
    assert gk.unit_key_for(chip, "chip") == chip.bdf
