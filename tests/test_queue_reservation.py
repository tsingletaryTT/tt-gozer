# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Queue fairness: a waiting whole-box request must not starve behind
smaller ones, and a freed unit must not sit behind a claim window that the
head of the queue cannot use.

Before this, the queue was enforced only by the 90-second claim window opened
at release, and only for the head ticket:

* a head that wanted 4 chips got a window when one board freed, could not use
  it, blocked every other waiter for 90 s, and was then sent to the back;
* with no window open, a newcomer took a freed board straight past the queue.

So a `--chips all` request could wait for ever on a box kept busy by 2-chip
jobs. Now an *active* ticket (its waiter polled recently) reserves the free
units it would take, in FIFO order, and later requests allocate around those
reservations.
"""
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from gozer import queue as queue_mod
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster, Grant
from conftest import QUIETBOX


def fake_proc(tmp_path, pids=(1,)):
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        (root / str(pid) / "fd").mkdir(parents=True)
        (root / str(pid) / "comm").write_text("python\n")
    return str(root)


def make(tmp_path, sysfs):
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=fake_proc(tmp_path))
    km = Keymaster(gk)
    # Never the real tt-smi: QUIETBOX's BDFs are a real QuietBox's BDFs.
    km.reset_runner = lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "", "")
    return km, gk


def _expire_claim_window(gk):
    path = gk.queue._window_path()
    if os.path.exists(path):
        old = time.time() - queue_mod.CLAIM_WINDOW_SECONDS - 5
        os.utime(path, (old, old))


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_ticket(gk, ticket, seconds, field):
    """Rewrite one timestamp on a ticket to `seconds` ago."""
    path = gk.queue._ticket_path(ticket)
    import json
    with open(path) as f:
        rec = json.load(f)
    rec[field] = _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))
    with open(path, "w") as f:
        json.dump(rec, f)


def test_whole_box_waiter_keeps_a_freed_board_from_a_newcomer(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    b = km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    assert not isinstance(big, Grant)

    km.release(a.lease_id)
    # The window used to go to `big` here, which cannot use one board; once
    # it expired, `big` went to the back and a newcomer took the board.
    _expire_claim_window(gk)
    small = km.acquire("1", who="claude:small", pid=1)
    assert not isinstance(small, Grant)

    km.release(b.lease_id)
    got = km.acquire("all", who="claude:big", ticket=big["ticket"])
    assert isinstance(got, Grant)
    assert len(got.bdfs) == 4


def test_whole_box_waiter_is_not_sent_to_the_back(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    later = km.acquire("1", who="claude:later", pid=1)

    km.release(a.lease_id)
    # No window for a head that cannot fit: nothing to expire, so nothing
    # gets demoted behind `later`.
    assert gk.queue.may_claim(later["ticket"]) is True
    assert [e["ticket"] for e in gk.queue_entries()] == [
        big["ticket"], later["ticket"]]
    # And `later` still cannot take the board `big` has reserved.
    assert not isinstance(
        km.acquire("1", who="claude:later", ticket=later["ticket"]), Grant)


def test_window_goes_to_the_first_ticket_that_fits(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    exact_busy = km.acquire("1", who="claude:wants-b", pid=1,
                            exact="0000:03:00.0")  # board b, still held
    fits = km.acquire("1", who="claude:fits", pid=1)

    km.release(a.lease_id)
    # wants-b is ahead but waits on a different board; it neither gets the
    # window nor reserves the freed board.
    assert gk.queue.may_claim(fits["ticket"]) is True
    assert gk.queue.may_claim(exact_busy["ticket"]) is False
    got = km.acquire("1", who="claude:fits", ticket=fits["ticket"])
    assert isinstance(got, Grant)


def test_inactive_ticket_does_not_reserve(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    # Its waiter has not polled for longer than the reservation lasts.
    for field in ("since", "seen_at"):
        _age_ticket(gk, big["ticket"], queue_mod.RESERVE_ACTIVE_SECONDS + 60,
                    field)

    km.release(a.lease_id)
    _expire_claim_window(gk)
    small = km.acquire("1", who="claude:small", pid=1)
    assert isinstance(small, Grant)
    # Still queued, at the front: it lost the reservation, not its place.
    assert gk.queue_position(big["ticket"]) == 1


def test_a_ticket_that_can_never_fit_does_not_reserve(tmp_path, sysfs):
    """A head asking for more than the healthy box can ever hold must not
    park the healthy boards for ever."""
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    # Pretend board b is degraded: no longer grantable to a normal request.
    gk.free_units = lambda include_degraded=False: (
        [u for u in Gatekeeper.free_units(gk, include_degraded)
         if u != "0000000000000001"])
    gk.grantable_units = lambda: ["0000000000000002"]
    km.release(a.lease_id)
    assert isinstance(km.acquire("1", who="claude:small", pid=1), Grant)
    assert gk.queue_position(big["ticket"]) == 1


def test_polling_refreshes_a_ticket_so_it_does_not_age_out(tmp_path, sysfs):
    """Known limitation closed: a waiter polling correctly behind a
    multi-hour lease used to lose its ticket at the one-hour mark."""
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:a", pid=1)
    t = km.acquire("1", who="claude:waiter", pid=1)
    # Enqueued over an hour ago, polled a few minutes ago.
    _age_ticket(gk, t["ticket"], queue_mod.TICKET_MAX_AGE_SECONDS + 60, "since")
    _age_ticket(gk, t["ticket"], 300, "seen_at")
    gk.prune_queue()
    assert gk.queue_position(t["ticket"]) == 1


def test_a_replayed_ticket_refreshes_seen_at(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:a", pid=1)
    t = km.acquire("1", who="claude:waiter", pid=1)
    _age_ticket(gk, t["ticket"], 300, "seen_at")
    before = gk.queue_entries()[0]["seen_at"]
    km.acquire("1", who="claude:waiter", ticket=t["ticket"])   # a poll
    assert gk.queue_entries()[0]["seen_at"] > before


def test_unpolled_ticket_still_ages_out(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:a", pid=1)
    t = km.acquire("1", who="claude:waiter", pid=1)
    _age_ticket(gk, t["ticket"], queue_mod.TICKET_MAX_AGE_SECONDS + 60, "since")
    _age_ticket(gk, t["ticket"], queue_mod.TICKET_MAX_AGE_SECONDS + 60, "seen_at")
    gk.prune_queue()
    assert gk.queue_position(t["ticket"]) is None


def test_status_and_queue_show_what_a_waiter_holds(tmp_path, sysfs, monkeypatch, capsys):
    """Reservations are visible, and looking at them reaps nothing."""
    import json
    from gozer import cli
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    km.release(a.lease_id)

    monkeypatch.setenv("GOZER_ROOT", gk.root)
    monkeypatch.setenv("GOZER_SYSFS_ROOT", gk.sysfs_root)
    monkeypatch.setenv("GOZER_PROC_ROOT", gk.proc_root)
    assert cli.main(["queue", "--json"]) == 0
    q = json.loads(capsys.readouterr().out)["queue"]
    assert q[0]["ticket"] == big["ticket"]
    assert q[0]["active"] is True
    assert q[0]["holding"] == a.units

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"holding {a.units[0]}" in out


# ---- Review findings (2026-11-11) -----------------------------------------

B1 = "0000000000000002"   # chips 0,1 in QUIETBOX
B2 = "0000000000000001"   # chips 2,3


def test_exact_fresh_waiter_does_not_hold_a_dirty_board(tmp_path, sysfs):
    """It could never take the board, and holding it would stop the
    lease-and-release that cleans it: a permanent stall."""
    km, gk = make(tmp_path, sysfs)
    assert not gk.is_clean(B2)
    t = km.acquire("1", who="claude:fresh", pid=1, exact="0000:03:00.0",
                   fresh=True)
    assert not isinstance(t, Grant)
    other = km.acquire("1", who="claude:other", pid=1, exact="0000:03:00.0")
    assert isinstance(other, Grant)
    km.release(other.lease_id)                 # the reset cleans B2
    got = km.acquire("1", who="claude:fresh", exact="0000:03:00.0",
                     fresh=True, ticket=t["ticket"])
    assert isinstance(got, Grant)


def test_an_unknown_ticket_is_held_to_every_reservation(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    km.release(a.lease_id)
    _expire_claim_window(gk)
    import pytest
    from gozer.keymaster import TicketNotFound
    with pytest.raises(TicketNotFound):
        km.acquire("1", who="claude:jumper", pid=1, ticket="ffff")
    assert gk.queue_position(big["ticket"]) == 1


def test_a_blocked_waiter_stops_holding_after_the_cap(tmp_path, sysfs):
    """Behind a lease that never ends, a polling whole-box waiter must not
    idle the other board for ever. It keeps its place."""
    km, gk = make(tmp_path, sysfs)
    km.acquire("1", who="vllm", pid=1)                 # never released
    big = km.acquire("all", who="claude:big", pid=1)
    early = km.acquire("1", who="claude:early", pid=1)
    assert not isinstance(early, Grant)                # big holds the board
    _age_ticket(gk, big["ticket"], queue_mod.RESERVE_BLOCKED_MAX_SECONDS + 60,
                "since")
    gk.touch_ticket(big["ticket"])                     # still polling
    got = km.acquire("1", who="claude:early", ticket=early["ticket"])
    assert isinstance(got, Grant)
    assert gk.queue_position(big["ticket"]) == 1


def test_a_waiter_does_not_hold_units_counting_on_its_own_lease(tmp_path, sysfs):
    """Hold-and-wait: it holds B1 and asks for all; it can never fit while it
    holds B1, so it must not park B2 too."""
    km, gk = make(tmp_path, sysfs)
    km.acquire("1", who="claude:x", pid=1)
    t = km.acquire("all", who="claude:x", pid=1)
    reserved, _ = gk.plan_queue()
    assert reserved == {}
    assert isinstance(km.acquire("1", who="claude:y", pid=1), Grant)
    assert gk.queue_position(t["ticket"]) == 1


def test_window_holder_keeps_its_window_when_a_ticket_ahead_wakes(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    small = km.acquire("1", who="claude:small", pid=1)
    for f in ("since", "seen_at"):
        _age_ticket(gk, big["ticket"], queue_mod.RESERVE_ACTIVE_SECONDS + 60, f)
    km.release(a.lease_id)
    assert gk.queue._claim_window_ticket() == small["ticket"]
    gk.touch_ticket(big["ticket"])                     # big wakes up
    got = km.acquire("1", who="claude:small", ticket=small["ticket"])
    assert isinstance(got, Grant)


def test_polling_does_not_rewrite_the_ticket_every_time(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:a", pid=1)
    t = km.acquire("1", who="claude:waiter", pid=1)
    path = gk.queue._ticket_path(t["ticket"])
    before = os.stat(path).st_mtime_ns
    km.acquire("1", who="claude:waiter", ticket=t["ticket"])
    assert os.stat(path).st_mtime_ns == before


def test_a_corrupt_ticket_does_not_break_acquire(tmp_path, sysfs):
    import json
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    t = km.acquire("all", who="claude:bad", pid=1)
    path = gk.queue._ticket_path(t["ticket"])
    rec = json.load(open(path))
    rec["seen_at"] = 12345
    rec["min_chips"] = "lots"
    json.dump(rec, open(path, "w"))
    assert isinstance(km.acquire("1", who="claude:ok", pid=1), Grant)
    km.release(a.lease_id)
