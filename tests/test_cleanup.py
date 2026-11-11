# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Phase 6 cleanups: unknown tickets always exit 13; a unit given back by a
reap (no reset) is flagged to its next tenant until a reset clears it."""
import json
import subprocess

import pytest

from gozer import cli
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Grant, Keymaster, TicketNotFound
from conftest import QUIETBOX, fake_proc_tree


@pytest.fixture
def box(tmp_path, sysfs, monkeypatch):
    proc = fake_proc_tree(tmp_path, {1: {}})
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=proc)
    km = Keymaster(gk)
    km.reset_runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setenv("GOZER_ROOT", gk.root)
    monkeypatch.setenv("GOZER_SYSFS_ROOT", gk.sysfs_root)
    monkeypatch.setenv("GOZER_PROC_ROOT", gk.proc_root)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return km, gk


def test_an_unknown_ticket_is_refused_even_with_chips_free(box, capsys):
    km, gk = box
    with pytest.raises(TicketNotFound):
        km.acquire("1", who="stale", pid=1, ticket="beef")
    assert gk.held_units() == {}
    rc = cli.main(["acquire", "--chips", "1", "--who", "stale", "--ticket", "beef"])
    assert rc == cli.EXIT_NO_LEASE


def test_a_reaped_unit_is_flagged_to_its_next_tenant(box, capsys):
    km, gk = box
    crashed = km.acquire("1", who="crashed", pid=4242)       # owner never existed
    gk.reconcile()                                            # reaps it, no reset
    assert gk.unit_lease(crashed.units[0]) is None
    assert gk.is_unreset(crashed.units[0])
    assert cli.main(["acquire", "--chips", "1", "--who", "next", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unreset"] == crashed.units
    assert cli.main(["acquire", "--chips", "1", "--who", "other"]) == 0
    assert "without a reset" not in capsys.readouterr().out   # the other board is fine


def test_a_reset_clears_the_flag(box, capsys):
    km, gk = box
    crashed = km.acquire("1", who="crashed", pid=4242)
    gk.reconcile()
    nxt = km.acquire("1", who="next", pid=1)
    assert nxt.unreset == crashed.units
    assert km.reset(nxt.lease_id, force=True).ok
    assert not gk.is_unreset(crashed.units[0])
    km.release(nxt.lease_id)
    again = km.acquire("1", who="later", pid=1)
    assert again.unreset == []


def test_a_release_with_reset_clears_the_flag(box):
    km, gk = box
    crashed = km.acquire("1", who="crashed", pid=4242)
    gk.reconcile()
    nxt = km.acquire("1", who="next", pid=1)
    km.release(nxt.lease_id)
    assert not gk.is_unreset(crashed.units[0])


def test_a_claim_window_does_not_block_a_waiter_that_fits_elsewhere(box, capsys):
    """Board A frees and its window goes to H (first in line). W, behind H,
    wants a board too; board B is free as well. W gets B at once instead of
    waiting out H's 90-second window -- and H's board stays H's."""
    km, gk = box
    a = km.acquire("1", who="a", pid=1)
    b = km.acquire("1", who="b", pid=1)
    h = km.acquire("1", who="h", pid=1)
    w = km.acquire("1", who="w", pid=1)
    km.release(a.lease_id)
    assert gk.queue._claim_window_ticket() == h["ticket"]
    km.release(b.lease_id)                      # board B frees too
    assert gk.queue._claim_window_ticket() == h["ticket"]   # still H's window
    assert cli.main(["wait", w["ticket"], "--timeout", "3s", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["granted"] is True
    hgrant = km.acquire("1", who="h", ticket=h["ticket"])
    assert isinstance(hgrant, Grant)            # H still gets a board
    assert set(hgrant.bdfs).isdisjoint(got["chips"])


def test_the_window_holder_keeps_its_board_for_its_window_even_if_idle(box):
    """Its waiter may be off doing other work between polls (it has up to the
    90-second window to come back); a newcomer must not take its board."""
    from datetime import datetime, timedelta, timezone
    from gozer import queue as queue_mod
    km, gk = box
    a = km.acquire("1", who="a", pid=1)
    km.acquire("1", who="b", pid=1)
    h = km.acquire("1", who="h", pid=1)
    km.release(a.lease_id)                      # H is active: it gets the window
    assert gk.queue._claim_window_ticket() == h["ticket"]
    # ...and its last poll then passes the activity limit mid-window.
    path = gk.queue._ticket_path(h["ticket"])
    rec = json.load(open(path))
    old = (datetime.now(timezone.utc) - timedelta(
        seconds=queue_mod.RESERVE_ACTIVE_SECONDS + 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec["since"] = rec["seen_at"] = old
    json.dump(rec, open(path, "w"))
    assert not isinstance(km.acquire("1", who="newcomer", pid=1), Grant)
    assert isinstance(km.acquire("1", who="h", ticket=h["ticket"]), Grant)
