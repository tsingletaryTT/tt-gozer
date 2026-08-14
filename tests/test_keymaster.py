import os
import signal
import subprocess
import sys
import pytest
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster, Grant, parse_duration
from conftest import QUIETBOX, GALAXY_LIKE


def fake_proc(tmp_path, pids=(1,)):
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        (root / str(pid) / "fd").mkdir(parents=True)
        (root / str(pid) / "comm").write_text("python\n")
    return str(root)


def make(tmp_path, sysfs, chips=QUIETBOX):
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(chips),
                    proc_root=fake_proc(tmp_path))
    return Keymaster(gk), gk


@pytest.mark.parametrize("text,secs", [
    ("90s", 90), ("45m", 2700), ("2h", 7200), ("30", 1800)])
def test_parse_duration(text, secs):
    assert parse_duration(text) == secs


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_acquire_grants_and_records_the_lease(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", reason="unit test", pid=1)
    assert isinstance(grant, Grant)
    assert len(grant.bdfs) == 2          # board grain: one chip request -> two chips
    assert grant.expanded is True
    assert grant.requested == 1
    lease = gk.read_lease(grant.lease_id)
    assert lease["who"] == "claude:test"
    assert lease["reason"] == "unit test"
    assert lease["chips"] == grant.bdfs


def test_acquire_marks_the_unit_held(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    assert gk.unit_lease(grant.units[0]) is not None


def test_second_acquire_takes_the_other_board(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    b = km.acquire("1", who="claude:b", pid=1)
    assert set(a.bdfs).isdisjoint(b.bdfs)


def test_acquire_rolls_back_units_it_already_won_if_it_loses_a_later_race(tmp_path, sysfs):
    # The brief's rollback loop (units already claimed are released again if a
    # later unit in the same multi-unit request loses claim_unit's mkdir race)
    # has no dedicated regression test yet. Simulate the race directly: force
    # the second of two chip-grain units to lose claim_unit, and prove the
    # first unit -- which *did* win -- is not left stranded, held by no lease.
    km, gk = make(tmp_path, sysfs, chips=GALAXY_LIKE)  # chip grain: 1 bdf per unit
    real_claim = gk.claim_unit
    won = []

    def flaky_claim(unit_key, lease):
        if not won:
            won.append(unit_key)
            return real_claim(unit_key, lease)
        return False  # lose the race on every unit after the first

    gk.claim_unit = flaky_claim
    result = km.acquire("2", who="claude:test", pid=1)

    assert not isinstance(result, Grant)          # falls back to a queue ticket
    assert won                                     # the race actually happened
    assert gk.unit_lease(won[0]) is None            # rolled back, not stranded
    assert gk.held_units() == {}
    assert gk.all_leases() == []


def test_third_acquire_is_queued_with_a_ticket(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    queued = km.acquire("1", who="claude:c", pid=1)
    assert not isinstance(queued, Grant)
    assert queued["ticket"]
    assert gk.queue_position(queued["ticket"]) == 1


def test_acquire_clears_the_clean_marker_on_the_granted_unit(tmp_path, sysfs):
    # clear_clean has no other test yet -- acquire() is its first real caller.
    # Prove it is actually invoked: mark a unit clean via a prior release,
    # then confirm the next acquire that is granted that same (clean-first
    # sorted) unit clears the marker rather than leaving a released-and-reset
    # unit permanently "clean" across every future lease.
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    first = km.acquire("1", who="claude:a", pid=1)
    km.release(first.lease_id)
    assert gk.is_clean(first.units[0]) is True

    second = km.acquire("1", who="claude:b", pid=1)
    # Clean units sort first (see Gatekeeper._unit_sort_key), so the just
    # -cleaned board is the one granted here.
    assert second.units[0] == first.units[0]
    assert gk.is_clean(second.units[0]) is False


def test_no_expansion_note_at_chip_grain(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs, chips=GALAXY_LIKE)
    grant = km.acquire("1", who="claude:test", pid=1)
    assert len(grant.bdfs) == 1
    assert grant.expanded is False


def test_env_emits_comma_separated_bdfs(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    env = km.env_for(grant)
    assert env["TT_VISIBLE_DEVICES"] == ",".join(grant.bdfs)
    assert ":" in env["TT_VISIBLE_DEVICES"]   # BDFs, never bare integers


def test_release_frees_the_unit_and_resets(tmp_path, sysfs):
    calls = []
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: calls.append(argv) or _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    ok, msg = km.release(grant.lease_id)
    assert ok is True
    assert gk.unit_lease(grant.units[0]) is None
    assert gk.read_lease(grant.lease_id) is None
    assert calls and calls[0][1] == "-r"
    assert set(calls[0][2].split(",")) == set(grant.bdfs)


def test_release_marks_the_unit_clean_for_fresh_requests(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    km.release(grant.lease_id)
    assert gk.is_clean(grant.units[0]) is True


def test_release_with_no_reset_skips_the_reset(tmp_path, sysfs):
    calls = []
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: calls.append(argv) or _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    km.release(grant.lease_id, no_reset=True)
    assert calls == []
    assert gk.is_clean(grant.units[0]) is False


def test_release_of_unknown_lease_reports_failure(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    ok, msg = km.release("nosuch")
    assert ok is False and "not found" in msg.lower()


def test_release_refuses_while_an_fd_is_open(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    # pid 1 now holds one of the leased devices open.
    fd_dir = os.path.join(gk.proc_root, "1", "fd")
    os.symlink(f"/dev/tenstorrent/{grant.dev_indices[0]}", os.path.join(fd_dir, "9"))
    ok, msg = km.release(grant.lease_id)
    assert ok is False and "still open" in msg.lower()
    assert gk.unit_lease(grant.units[0]) is not None


def test_release_force_overrides_an_open_fd(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    fd_dir = os.path.join(gk.proc_root, "1", "fd")
    os.symlink(f"/dev/tenstorrent/{grant.dev_indices[0]}", os.path.join(fd_dir, "9"))
    ok, msg = km.release(grant.lease_id, force=True)
    assert ok is True
    assert gk.unit_lease(grant.units[0]) is None


def test_run_releases_on_normal_exit(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    rc = km.run([sys.executable, "-c", "pass"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc == 0
    assert gk.all_leases() == []


def test_run_releases_on_nonzero_exit(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    rc = km.run([sys.executable, "-c", "raise SystemExit(3)"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc == 3
    assert gk.all_leases() == []


def test_run_exports_visible_devices_to_the_child(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    out = tmp_path / "seen.txt"
    code = f"import os; open({str(out)!r}, 'w').write(os.environ['TT_VISIBLE_DEVICES'])"
    km.run([sys.executable, "-c", code], chips_spec="1", who="claude:test", pid=1)
    assert ":" in out.read_text()


def test_run_releases_when_the_child_is_killed(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    rc = km.run([sys.executable, "-c",
                 "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc != 0
    assert gk.all_leases() == []


def test_run_returns_10_when_queued(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:hog", pid=1)
    rc = km.run([sys.executable, "-c", "pass"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc == 10


class _Ok:
    returncode, stdout, stderr = 0, "", ""
