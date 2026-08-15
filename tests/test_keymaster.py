import os
import signal
import subprocess
import sys
import threading
import time
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
    # Mark the lowest-indexed chip clean beforehand: _unit_sort_key sorts
    # clean units first regardless of device index, so this chip is the one
    # allocate() picks first, and therefore the one claim_unit succeeds on
    # before the race is lost on the second unit -- exactly the scenario
    # where a naive rollback would wrongly scrub a marker it never should
    # have touched (see the is_clean assertion below).
    gk.mark_clean("0000:01:00.0")

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
    assert won == ["0000:01:00.0"]                # the race actually happened,
                                                    # and happened on the clean unit
    assert gk.unit_lease(won[0]) is None            # rolled back, not stranded
    assert gk.held_units() == {}
    assert gk.all_leases() == []
    # The unit that WAS won, and was clean before this aborted request ever
    # touched it, must still be clean -- clear_clean must not run until every
    # unit in the request has been claimed, or a rolled-back request leaves
    # an untouched board wrongly needing a reset it never required.
    assert gk.is_clean("0000:01:00.0") is True


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


def test_a_satisfied_elastic_request_is_not_reported_as_expanded(tmp_path, sysfs):
    """`--chips 1-3` granted 2 chips got exactly what it asked for.

    `expanded` compared the grant against the request's *minimum*, so any
    elastic request that got more than its floor printed "asked for 1 -- UMD
    expands TT_VISIBLE_DEVICES to the whole board" -- teaching an agent
    something false about a grant that was simply inside its own range.
    """
    km, gk = make(tmp_path, sysfs)
    # 1-3 rather than 1-4: allocate stops adding units once the next one
    # would overshoot the maximum, so this grants exactly one 2-chip board --
    # more than the minimum, still inside the range.
    grant = km.acquire("1-3", who="claude:test", pid=1)
    assert len(grant.bdfs) == 2      # a whole board, well inside 1-3
    assert grant.expanded is False


def test_expansion_is_still_reported_when_the_grain_overshoots(tmp_path, sysfs):
    """The other side of the same comparison: `--chips 1` on a 2-chip board
    really did get more than it could ask for, and must say so."""
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    assert len(grant.bdfs) == 2
    assert grant.expanded is True


def test_env_emits_comma_separated_bdfs(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    env = km.env_for(grant)
    assert env["TT_VISIBLE_DEVICES"] == ",".join(grant.bdfs)
    assert ":" in env["TT_VISIBLE_DEVICES"]   # BDFs, never bare integers


def test_env_for_a_raw_lease_dict_reads_the_chips_key(tmp_path, sysfs):
    # env_for() has two branches -- a Grant, or a raw lease dict (e.g. as
    # returned by gk.read_lease()). Only the Grant branch was covered.
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    lease = gk.read_lease(grant.lease_id)
    assert isinstance(lease, dict)
    env = km.env_for(lease)
    assert env["TT_VISIBLE_DEVICES"] == ",".join(lease["chips"])
    assert env["TT_VISIBLE_DEVICES"] == ",".join(grant.bdfs)


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


def test_release_of_a_failed_reset_does_not_mark_clean_but_still_tears_down(tmp_path, sysfs):
    # Global constraint: "a unit is marked clean only when the reset
    # actually succeeded." A unit wrongly marked clean would be handed to a
    # --fresh requester as reset silicon when it was never actually reset.
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Fail()
    grant = km.acquire("1", who="claude:test", pid=1)
    ok, msg = km.release(grant.lease_id)
    assert ok is True                              # a failed reset must not
                                                     # strand the lease
    assert gk.is_clean(grant.units[0]) is False      # never marked clean
    assert gk.unit_lease(grant.units[0]) is None     # unit is still released
    assert gk.read_lease(grant.lease_id) is None
    assert "failed" in msg.lower()


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


def test_release_never_resets_chips_that_now_belong_to_someone_else(tmp_path, sysfs,
                                                                    monkeypatch):
    """The B2 race, reproduced through the /proc scan that opens the window.

    A stale lease is released by hand -- exactly what the gatekeeper skill
    tells a human or successor agent to do. The scan of /proc/*/fd takes real
    time, and another agent's `acquire` legitimately reaps the stale lease and
    claims the unit inside that window. The release must notice on its way
    out and abort: running `tt-smi -r` here would reset the new tenant's chips
    mid-startup, and the teardown that follows would delete their lock and
    open a claim window on it.
    """
    from gozer import keymaster as keymaster_mod
    km, gk = make(tmp_path, sysfs)
    resets = []
    km.reset_runner = lambda argv, **kw: resets.append(argv) or _Ok()
    grant = km.acquire("1", who="claude:stale", pid=1)
    unit = grant.units[0]

    def stealing_holders(proc_root, **kw):
        # Stand in for the hundreds of milliseconds the real scan takes: the
        # other agent reaps our lease and claims the unit while we are in here.
        gk.release_unit(unit)
        gk.claim_unit(unit, {"lease_id": "newtenant", "who": "claude:b"})
        return {}

    monkeypatch.setattr(keymaster_mod.procfd, "holders", stealing_holders)
    ok, msg = km.release(grant.lease_id)

    assert ok is False
    assert resets == [], "reset ran against another lease's chips"
    assert "newtenant" in msg
    assert gk.unit_lease(unit)["lease_id"] == "newtenant"  # lock untouched


def test_release_skips_the_reset_when_its_own_lock_is_already_gone(tmp_path, sysfs):
    """A lease whose lock was already reaped is still cleanable, but must not
    fire a reset: those chips are free for anyone to take right now, so a
    reset would land on whoever grabs them next. The lease record is removed
    so the caller is not left with an un-releasable id."""
    calls = []
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: calls.append(argv) or _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    gk.release_unit(grant.units[0])  # reaped out from under us

    ok, msg = km.release(grant.lease_id)

    assert ok is True
    assert calls == []
    assert "not resetting" in msg
    assert gk.read_lease(grant.lease_id) is None


def test_release_does_not_hold_the_mutex_across_the_reset(tmp_path, sysfs):
    """A reset takes tens of seconds. Holding the global mutex across it would
    block every other user of the box for that whole time, so the gate is
    revalidated under the lock and the lock is dropped before tt-smi runs."""
    km, gk = make(tmp_path, sysfs)
    held_during_reset = []

    def watching_runner(argv, **kw):
        mutex = os.path.join(gk.root, "mutex", ".gatekeeper.lock")
        held_during_reset.append(os.path.isdir(mutex))
        return _Ok()

    km.reset_runner = watching_runner
    grant = km.acquire("1", who="claude:test", pid=1)
    km.release(grant.lease_id)

    assert held_during_reset == [False]


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


@pytest.mark.timeout(5)
def test_run_forwards_sigterm_to_a_running_child(tmp_path, sysfs):
    # test_run_releases_when_the_child_is_killed sends the signal *inside*
    # the child, to its own pid -- it proves release survives an unclean
    # child exit, but never exercises run()'s own SIGINT/SIGTERM handlers or
    # _forward at all. This test sends the signal to the *parent* (this
    # process, running km.run()) while it is genuinely blocked in
    # proc.wait() on a long-lived child, so _forward actually has to fire
    # and actually has to call proc.send_signal for the child to die.
    #
    # Bounded by @pytest.mark.timeout(5): the child sleeps for 10s, far
    # longer than the timeout, so if forwarding silently breaks this test
    # fails fast (via pytest-timeout) instead of hanging the suite.
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()

    def send_after_a_beat():
        time.sleep(0.5)  # give run() time to acquire and install handlers
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=send_after_a_beat, daemon=True).start()
    rc = km.run([sys.executable, "-c", "import time; time.sleep(10)"],
                chips_spec="1", who="claude:test", pid=1)
    # A child terminated by an uncaught SIGTERM reports a negative
    # returncode equal to -signum (verified empirically: Popen.wait() after
    # sending SIGTERM to an unmodified `python3 -c "time.sleep(...)"` child
    # yields -15). If _forward never fired, the child would instead run to
    # completion after its full 10s sleep and rc would be 0.
    assert rc == -signal.SIGTERM
    assert gk.all_leases() == []


@pytest.mark.timeout(5)
def test_run_forwards_sigint_to_a_running_child(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()

    def send_after_a_beat():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=send_after_a_beat, daemon=True).start()
    rc = km.run([sys.executable, "-c", "import time; time.sleep(10)"],
                chips_spec="1", who="claude:test", pid=1)
    # Python's own SIGINT default handler converts it to KeyboardInterrupt,
    # but an unmodified child still dies from the raw signal in this setup
    # (verified empirically): Popen.wait() reports -2 (-SIGINT).
    assert rc == -signal.SIGINT
    assert gk.all_leases() == []


def test_run_releases_and_restores_handlers_on_a_signal_pending_before_popen(tmp_path, sysfs):
    # Round-2 review finding: SIG_UNBLOCK must be the FIRST statement inside
    # the inner try/finally, not the statement before it. If a signal is
    # already pending at the moment of unblock, _forward fires immediately
    # -- before Popen has ever run, so `proc` is still None -- and takes the
    # no-live-child branch, raising KeyboardInterrupt. That raise must land
    # inside the try whose finally restores the signal handlers and
    # releases the lease, or both leak: the lease stays held forever, and
    # _forward (closing over a permanently-None proc) is left installed as
    # this process's SIGINT/SIGTERM handler for the rest of its life.
    #
    # The `_before_unblock` seam runs while SIGINT/SIGTERM are still
    # blocked, so the signal it sends here is held pending by the kernel,
    # not delivered on the spot -- this is the only reliable way to hit the
    # otherwise sub-millisecond "pending at unblock time" race from a test.
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)

    def make_a_signal_pending():
        os.kill(os.getpid(), signal.SIGTERM)

    with pytest.raises(KeyboardInterrupt):
        km.run([sys.executable, "-c", "pass"],
               _before_unblock=make_a_signal_pending,
               chips_spec="1", who="claude:test", pid=1)

    assert gk.all_leases() == []
    assert signal.getsignal(signal.SIGTERM) == original_sigterm
    assert signal.getsignal(signal.SIGINT) == original_sigint


def test_run_returns_10_when_queued(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:hog", pid=1)
    rc = km.run([sys.executable, "-c", "pass"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc == 10


def test_a_ticket_taken_by_run_survives_the_next_prune(tmp_path, sysfs):
    """B3 survived on the `run` path, and this is where it hurt.

    `run` supplies its own pid, so its ticket was written detached: False --
    but `run` does not block when it is queued: it prints "queued: ticket X",
    returns 10, and exits. The pid on that ticket is dead a moment later, so
    the next prune dropped it and `gozer wait X` -- which the README and the
    keymaster skill both tell you to run next -- came back exit 13 on a
    ticket the user had just been handed.

    The fixture's fake /proc contains only pid 1, so this process's own pid
    (what run() records) is "dead" here exactly as it is on a real box a
    second after the command returns.
    """
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:hog", pid=1)

    rc = km.run([sys.executable, "-c", "pass"], chips_spec="1", who="run:queued")

    assert rc == 10
    issued = [e["ticket"] for e in gk.queue_entries()]
    assert len(issued) == 1

    gk.prune_queue()

    assert [e["ticket"] for e in gk.queue_entries()] == issued


def test_run_returns_a_nonzero_int_when_popen_itself_fails(tmp_path, sysfs):
    # run()'s contract is "-> int": a bad executable must not raise out of
    # run(), and the lease must still be released by the finally either way.
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    rc = km.run(["/no/such/executable-gozer-test"],
                chips_spec="1", who="claude:test", pid=1)
    assert isinstance(rc, int)
    assert rc != 0
    assert gk.all_leases() == []


class _Ok:
    returncode, stdout, stderr = 0, "", ""


class _Fail:
    returncode, stdout, stderr = 1, "", "tt-smi: reset failed"
