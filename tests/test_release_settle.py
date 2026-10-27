"""`gozer run`'s own release waits a bounded time for a just-exited command's
leftover holders before it refuses (tt-orchard issue 117).

A command that exits fast can leave a forked worker holding the device for a
moment. Before this change the release scanned once, saw the worker, refused,
and left a stale lease and chips that were never reset. Every test here uses a
fake /proc, a fake clock and a fake reset, so no chip and no real waiting.
"""
import shutil

from conftest import QUIETBOX, fake_proc_tree
from gozer import keymaster as km_mod
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster

OWNER = 5_000_100
WORKER = 5_000_300


class _Ok:
    returncode, stdout, stderr = 0, "reset ok", ""


class Clock:
    """A clock that only moves when the keymaster sleeps."""

    def __init__(self, on_sleep=None):
        self.now = 1000.0
        self.sleeps = []
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(len(self.sleeps), self.now)


def setup(tmp_path, sysfs, on_sleep=None):
    proc = fake_proc_tree(tmp_path, {OWNER: {"ppid": 1}})
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=proc)
    km = Keymaster(gk)
    resets = []
    km.reset_runner = lambda argv, **kw: resets.append(argv) or _Ok()
    clock = Clock(on_sleep)
    km.sleep, km.monotonic = clock.sleep, clock.monotonic
    grant = km.acquire("1", who="claude:t", pid=OWNER)
    dev = sorted(c.dev_index for u in grant.units for c in gk.chips_in_unit(u))[0]
    return km, gk, grant, dev, resets, clock, proc


def add_worker(tmp_path, proc, dev):
    # fake_proc_tree builds a whole tree; add one more process beside it
    fake_proc_tree(tmp_path, {OWNER: {"ppid": 1}, WORKER: {"ppid": 1, "devs": [dev]}})


def drop_worker(proc):
    shutil.rmtree(f"{proc}/{WORKER}")


def test_release_without_settle_still_refuses_at_once(tmp_path, sysfs):
    """`gozer release` by hand keeps its old behaviour: no waiting."""
    km, gk, grant, dev, resets, clock, proc = setup(tmp_path, sysfs)
    add_worker(tmp_path, proc, dev)
    ok, msg = km.release(grant.lease_id)
    assert not ok and "still open" in msg
    assert clock.sleeps == [] and resets == []


def test_a_holder_that_exits_within_the_window_lets_the_release_go_through(tmp_path, sysfs):
    state = {}

    def on_sleep(n, now):
        if n == 3:                       # the worker exits after three polls
            drop_worker(state["proc"])

    km, gk, grant, dev, resets, clock, proc = setup(tmp_path, sysfs, on_sleep)
    state["proc"] = proc
    add_worker(tmp_path, proc, dev)
    ok, msg = km.release(grant.lease_id, settle=10.0)
    assert ok, msg
    assert len(resets) == 1                       # the chips were reset
    assert len(clock.sleeps) == 3
    assert "still open for" in msg                # the wait is reported
    assert gk.read_lease(grant.lease_id) is None  # the lease is gone


def test_a_holder_that_never_exits_is_still_refused_after_the_bound(tmp_path, sysfs):
    """The safety property: a live holder blocks the reset, however long we wait."""
    km, gk, grant, dev, resets, clock, proc = setup(tmp_path, sysfs)
    add_worker(tmp_path, proc, dev)
    ok, msg = km.release(grant.lease_id, settle=2.0)
    assert not ok
    assert "still open by [%d]" % WORKER in msg
    assert "after waiting" in msg
    assert resets == []
    assert gk.read_lease(grant.lease_id) is not None
    assert 2.0 <= sum(clock.sleeps) < 2.0 + km_mod.RELEASE_SETTLE_POLL_SECONDS + 1e-9


def test_force_does_not_wait(tmp_path, sysfs):
    km, gk, grant, dev, resets, clock, proc = setup(tmp_path, sysfs)
    add_worker(tmp_path, proc, dev)
    ok, _ = km.release(grant.lease_id, force=True, settle=10.0)
    assert ok and clock.sleeps == []


def test_no_holder_means_no_waiting(tmp_path, sysfs):
    km, gk, grant, dev, resets, clock, proc = setup(tmp_path, sysfs)
    ok, msg = km.release(grant.lease_id, settle=10.0)
    assert ok and clock.sleeps == []
    assert "still open for" not in msg


def test_a_holder_of_a_different_chip_does_not_count(tmp_path, sysfs):
    km, gk, grant, dev, resets, clock, proc = setup(tmp_path, sysfs)
    mine = {c.dev_index for u in grant.units for c in gk.chips_in_unit(u)}
    other = sorted({0, 1, 2, 3} - mine)[0]
    add_worker(tmp_path, proc, other)
    ok, _ = km.release(grant.lease_id, settle=10.0)
    assert ok and clock.sleeps == []


def test_the_refusal_is_logged_with_how_long_it_waited(tmp_path, sysfs):
    import json, os
    km, gk, grant, dev, resets, clock, proc = setup(tmp_path, sysfs)
    add_worker(tmp_path, proc, dev)
    km.release(grant.lease_id, settle=1.0)
    with open(os.path.join(gk.history_root, "history.jsonl")) as f:
        refused = [r for r in map(json.loads, f) if r["event"] == "refused"]
    assert refused and refused[-1]["waited_s"] >= 1.0


def test_run_releases_with_the_settle_window(tmp_path, sysfs, monkeypatch):
    """The wiring: run()'s release asks for the window, `gozer release` does not."""
    km, gk, grant, dev, resets, clock, proc = setup(tmp_path, sysfs)
    seen = {}
    monkeypatch.setattr(km, "release",
                        lambda lease_id, **kw: seen.update(kw) or (True, ""))
    km.release(grant.lease_id)           # sanity: the stub records kwargs
    seen.clear()
    rc = km.run(["true"], chips_spec="1", who="claude:t")
    assert rc == 0
    assert seen.get("settle") == km_mod.RELEASE_SETTLE_SECONDS
