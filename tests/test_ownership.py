"""`HELD` versus `HELD-FOREIGN` for a lease taken with an owner pid.

A supervisor that starts the model server as its own child holds the lease under
its pid, while the device is opened by the child. gozer must read that as the
owner's own work (`HELD`) and keep `HELD-FOREIGN` for a holder that has nothing
to do with the owner, because the gatekeeper skill tells agents to investigate
`HELD-FOREIGN` as a lease that is lying.
"""
import shutil

from conftest import QUIETBOX, fake_proc_tree
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster

OWNER = 5_000_100
CHILD = 5_000_200
GRANDCHILD = 5_000_300
STRANGER = 5_000_400


def board_with_lease(tmp_path, sysfs):
    """Take the lease first, with only the owner running.

    A process that already has a device open would make that board look busy,
    and acquire would put the lease on the other board. The tests add the
    device holders afterwards, with fake_proc_tree.
    """
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=fake_proc_tree(tmp_path, {OWNER: {"ppid": 1}}))
    grant = Keymaster(gk).acquire("1", who="orchard:test", pid=OWNER)
    assert 0 in grant.dev_indices
    return gk


def state_of(gk, dev_index):
    return {s.chip.dev_index: s.state for s in gk.reconcile(reap=False)}[dev_index]


def test_a_child_of_the_owner_holding_the_device_is_held(tmp_path, sysfs):
    gk = board_with_lease(tmp_path, sysfs)
    fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER, "devs": [0]}})
    assert state_of(gk, 0) == "HELD"


def test_a_grandchild_of_the_owner_holding_the_device_is_held(tmp_path, sysfs):
    gk = board_with_lease(tmp_path, sysfs)
    fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER},
                              GRANDCHILD: {"ppid": CHILD, "devs": [0]}})
    assert state_of(gk, 0) == "HELD"


def test_an_unrelated_holder_is_still_held_foreign(tmp_path, sysfs):
    gk = board_with_lease(tmp_path, sysfs)
    fake_proc_tree(tmp_path, {STRANGER: {"ppid": 1, "devs": [0]}})
    assert state_of(gk, 0) == "HELD-FOREIGN"


def test_a_child_whose_owner_has_exited_is_held_foreign(tmp_path, sysfs):
    # The owner pid is gone and the server was re-parented to init. Someone
    # holds the device and nobody owns the lease, so the alarm must still fire.
    gk = board_with_lease(tmp_path, sysfs)
    fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER, "devs": [0]}})
    shutil.rmtree(tmp_path / "proc" / str(OWNER))
    (tmp_path / "proc" / str(CHILD) / "status").write_text("Name:\tpython\nPPid:\t1\n")
    assert state_of(gk, 0) == "HELD-FOREIGN"


def test_the_owner_pid_itself_holding_the_device_is_still_held(tmp_path, sysfs):
    gk = board_with_lease(tmp_path, sysfs)
    fake_proc_tree(tmp_path, {OWNER: {"ppid": 1, "devs": [0]}})
    assert state_of(gk, 0) == "HELD"


def test_a_detached_lease_never_reports_held_foreign(tmp_path, sysfs):
    # Unchanged behavior, pinned so the new rule cannot widen into it.
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=fake_proc_tree(tmp_path, {}))
    grant = Keymaster(gk).acquire("1", who="claude:test")   # no pid -> detached
    assert 0 in grant.dev_indices
    fake_proc_tree(tmp_path, {STRANGER: {"ppid": 1, "devs": [0]}})
    assert state_of(gk, 0) == "HELD"


def test_a_lease_with_no_owner_pid_never_asks_about_descendants(tmp_path, sysfs, monkeypatch):
    # A malformed non-detached lease with no pid has no owner whose children
    # could count. Any holder is foreign, and the walk is not started with
    # None as the ancestor.
    import json
    from gozer import procfd
    gk = board_with_lease(tmp_path, sysfs)
    path = tmp_path / "state" / "gate"
    lease_json = next(path.glob("*.lock")) / "lease.json"
    lease = json.loads(lease_json.read_text())
    lease.pop("pid"); lease.pop("pgid", None)
    lease_json.write_text(json.dumps(lease))
    fake_proc_tree(tmp_path, {STRANGER: {"ppid": 1, "devs": [0]}})
    def boom(*a, **k):
        raise AssertionError("is_descendant called for a lease with no owner")
    monkeypatch.setattr(procfd, "is_descendant", boom)
    assert state_of(gk, 0) == "HELD-FOREIGN"
