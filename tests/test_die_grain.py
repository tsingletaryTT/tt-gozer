# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""One lease per die on a two-die board (`gozer grain chip`), and the reset
policy that makes it safe.

Measured on a 2x p300c QuietBox on 2026-11-11 (ttnn 0.80.0, tt-smi 6.3.0,
firmware 19.15.0, KMD 2.10.1); see the project CLAUDE.md:

* TT_VISIBLE_DEVICES with one die's BDF opens only that die. UMD does not
  widen it to the board partner, so a one-die lease can be fenced.
* Two processes, one per die of the same board, run side by side.
* A die reset leaves single-die work on its partner running, but takes the
  board's die-to-die link down: a mesh over both dies does not form until the
  whole board is reset.
* Resetting one board does not disturb the other board.

So at chip grain a release resets the whole board when the partner die is
free, and otherwise resets its own die and marks the board split.
"""
import json
import os
import subprocess

import pytest

from gozer import cli
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Grant, Keymaster
from conftest import QUIETBOX, fake_proc_tree

BOARD_A = "0000000000000002"      # chips 0,1  (0000:01:00.0, 0000:02:00.0)
BOARD_B = "0000000000000001"      # chips 2,3


def make(tmp_path, sysfs, procs=None, grain="chip"):
    proc = fake_proc_tree(tmp_path, procs or {1: {}})
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=proc)
    if grain:
        assert gk.set_grain(grain) is None
    km = Keymaster(gk)
    resets = []

    def runner(argv, **kw):
        resets.append(argv[2].split(","))
        return subprocess.CompletedProcess(argv, 0, "", "")
    km.reset_runner = runner
    return km, gk, resets


def test_chip_grain_leases_one_die_and_both_dies_of_a_board_side_by_side(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    assert gk.grain == "chip"
    a = km.acquire("1", who="claude:a", pid=1)
    b = km.acquire("1", who="claude:b", pid=1)
    assert a.bdfs == ["0000:01:00.0"] and not a.expanded
    assert b.bdfs == ["0000:02:00.0"]                 # the partner die
    assert gk.board_of(a.units[0]).serial == gk.board_of(b.units[0]).serial


def test_board_grain_is_unchanged_by_default(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs, grain=None)
    assert gk.grain == "board" and gk.grain_setting() == "auto"
    assert gk.partner_units([BOARD_A]) == []
    a = km.acquire("1", who="claude:a", pid=1)
    assert len(a.bdfs) == 2 and a.expanded


def test_grain_cannot_change_under_a_lease_or_a_queue(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    lease = km.acquire("all", who="claude:a", pid=1)
    assert "leased" in gk.set_grain("board")
    km.acquire("1", who="claude:waiting", pid=1)    # queued
    km.release(lease.lease_id)
    gk.dequeue(gk.queue_entries()[0]["ticket"]) if gk.queue_entries() else None
    for lid in [l["lease_id"] for l in gk.all_leases()]:
        km.release(lid)
    assert gk.set_grain("board") is None
    assert gk.grain == "board"
    assert gk.set_grain("sideways").startswith("unknown grain")


def test_release_resets_the_whole_board_when_the_partner_is_free(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    ok, msg = km.release(a.lease_id)
    assert ok
    assert sorted(resets[-1]) == ["0000:01:00.0", "0000:02:00.0"]
    assert "whole board" in msg
    assert gk.is_clean("0000:01:00.0") and gk.is_clean("0000:02:00.0")
    assert gk.held_units() == {}                    # the partner was only borrowed
    assert not gk.is_split(BOARD_A)


def test_release_resets_only_its_die_while_the_partner_is_leased(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    b = km.acquire("1", who="claude:b", pid=1)
    ok, msg = km.release(a.lease_id)
    assert ok
    assert resets[-1] == ["0000:01:00.0"]
    assert gk.is_split(BOARD_A) and "whole-board reset" in msg
    assert gk.unit_lease("0000:02:00.0")["lease_id"] == b.lease_id

    # The partner leaves with die 0 free: the whole board is reset and healed.
    ok, msg = km.release(b.lease_id)
    assert sorted(resets[-1]) == ["0000:01:00.0", "0000:02:00.0"]
    assert not gk.is_split(BOARD_A)


def test_partner_open_without_a_lease_counts_as_busy(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs, procs={1: {}, 4242: {"devs": [1]}})
    a = km.acquire("1", who="claude:a", pid=1, exact="0000:01:00.0")
    km.release(a.lease_id)
    assert resets[-1] == ["0000:01:00.0"]
    assert gk.is_split(BOARD_A)


def test_partner_lock_race_falls_back_to_a_single_die_reset(tmp_path, sysfs, monkeypatch):
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    real = gk.claim_unit
    monkeypatch.setattr(gk, "claim_unit",
                        lambda unit, lease: False if unit == "0000:02:00.0"
                        else real(unit, lease))
    km.release(a.lease_id)
    assert resets[-1] == ["0000:01:00.0"]
    assert gk.is_split(BOARD_A)
    assert gk.held_units() == {}


def test_failed_whole_board_reset_marks_nothing_clean(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom")
    a = km.acquire("1", who="claude:a", pid=1)
    result = km.release_result(a.lease_id)
    assert result.status == "reset-failed"
    assert not gk.is_clean("0000:02:00.0")
    assert gk.held_units() == {}


def test_no_reset_release_leaves_the_partner_alone(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.release(a.lease_id, no_reset=True)
    assert resets == []
    assert not gk.is_split(BOARD_A)


def test_in_place_reset_of_a_whole_board_heals_a_split(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    gk.mark_split(BOARD_A)
    whole = km.acquire("2", who="claude:mesh", pid=1, exact=None)
    assert sorted(whole.bdfs) == ["0000:01:00.0", "0000:02:00.0"]
    r = km.reset(whole.lease_id, force=True)
    assert r.ok
    assert not gk.is_split(BOARD_A)


def test_in_place_reset_of_one_die_marks_the_board_split(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    one = km.acquire("1", who="claude:a", pid=1)
    r = km.reset(one.lease_id, force=True)
    assert r.ok and gk.is_split(BOARD_A)
    assert "whole-board reset" in r.message


def test_board_grain_release_never_borrows_a_partner(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs, grain="board")
    a = km.acquire("1", who="claude:a", pid=1)
    km.release(a.lease_id)
    assert sorted(resets[-1]) == sorted(a.bdfs)
    assert not gk.is_split(a.units[0])


@pytest.fixture
def cli_env(tmp_path, sysfs, monkeypatch):
    km, gk, resets = make(tmp_path, sysfs)
    monkeypatch.setenv("GOZER_ROOT", gk.root)
    monkeypatch.setenv("GOZER_SYSFS_ROOT", gk.sysfs_root)
    monkeypatch.setenv("GOZER_PROC_ROOT", gk.proc_root)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return km, gk


def test_grain_command_shows_and_sets(cli_env, capsys):
    km, gk = cli_env
    assert cli.main(["grain", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"grain": "chip", "setting": "chip", "derived": "board"}
    assert cli.main(["grain", "auto"]) == 0
    assert gk.grain == "board"
    km.acquire("1", who="claude:a", pid=1)
    assert cli.main(["grain", "chip"]) == cli.EXIT_UNAVAILABLE
    assert "not changed" in capsys.readouterr().out


def test_status_and_grant_explain_dies(cli_env, capsys):
    km, gk = cli_env
    assert cli.main(["acquire", "--chips", "1", "--who", "claude:a"]) == 0
    out = capsys.readouterr().out
    assert "one die of p300c board" in out
    assert "TT_MESH_GRAPH_DESC_PATH" in out
    gk.mark_split(BOARD_B)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "grain: chip (set)" in out
    assert "dies reset separately" in out


# ---- Review findings (2026-11-11, second review) -----------------------------

A0, A1, B0, B1 = "0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "0000:04:00.0"


def test_a_both_dies_release_clears_the_split_mark(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    gk.mark_split(BOARD_A)
    whole = km.acquire("2", who="mesh", pid=1)
    assert sorted(whole.bdfs) == [A0, A1]
    km.release(whole.lease_id)
    assert not gk.is_split(BOARD_A)


def test_reset_all_clears_split_marks(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    gk.mark_split(BOARD_A)
    gk.mark_split(BOARD_B)
    assert km.reset_all(who="ops").ok
    assert not gk.is_split(BOARD_A) and not gk.is_split(BOARD_B)


def test_a_board_grain_release_clears_a_split_left_from_chip_grain(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs, grain="board")
    gk.mark_split(BOARD_A)
    a = km.acquire("1", who="a", pid=1, exact=BOARD_A)
    km.release(a.lease_id)
    assert not gk.is_split(BOARD_A)


def test_split_is_decided_per_board(tmp_path, sysfs):
    """A board reset whole is healed even when another board of the same
    lease could not be."""
    km, gk, resets = make(tmp_path, sysfs)
    km.acquire("1", who="other", pid=1, exact=B1)
    mine = km.acquire("3", who="mine", pid=1)
    assert sorted(mine.bdfs) == [A0, A1, B0]
    km.release(mine.lease_id)
    assert sorted(resets[-1]) == [A0, A1, B0]
    assert not gk.is_split(BOARD_A)
    assert gk.is_split(BOARD_B)


def test_partners_are_borrowed_per_board(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    km.acquire("1", who="other", pid=1, exact=B1)
    hold = km.acquire("1", who="h", pid=1, exact=A1)
    two = km.acquire("2", who="two", pid=1)
    assert sorted(two.bdfs) == [A0, B0]
    km.release(hold.lease_id, no_reset=True)          # A1 now free, B1 still busy
    km.release(two.lease_id)
    assert sorted(resets[-1]) == [A0, A1, B0]         # A1 borrowed, B1 not
    assert not gk.is_split(BOARD_A) and gk.is_split(BOARD_B)


def test_changing_grain_forgets_clean_markers(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    a = km.acquire("1", who="a", pid=1, exact=A0)
    km.release(a.lease_id)                            # A0, A1 clean (BDF keys)
    assert gk.set_grain("board") is None
    b = km.acquire("1", who="dirty", pid=1, exact=BOARD_A)
    km.reset_runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom")
    assert km.release_result(b.lease_id).status == "reset-failed"
    assert gk.set_grain("chip") is None
    g = km.acquire("1", who="fresh", pid=1, exact=A0, fresh=True, no_queue=True)
    assert g is None                                  # not handed out as fresh


def test_a_borrowed_partner_cannot_be_reaped_mid_reset(tmp_path, sysfs):
    """The partner lock is owned by the releasing process, so a detached
    lease past its grace window does not make it STALE."""
    km, gk, resets = make(tmp_path, sysfs)
    fake_proc_tree(tmp_path, {os.getpid(): {"ppid": 1}})   # the releaser is alive
    # The lease's owner has exited (a supervisor that died, or `gozer
    # release` run by hand afterwards): its own record is STALE.
    a = km.acquire("1", who="a", pid=99999, exact=A0)
    km2 = Keymaster(Gatekeeper(root=gk.root, sysfs_root=gk.sysfs_root,
                               proc_root=gk.proc_root))
    grabbed = {}

    def runner(argv, **kw):
        resets.append(argv[2].split(","))
        grabbed["g"] = km2.acquire("1", who="intruder", pid=1, exact=A1,
                                   no_queue=True)
        return subprocess.CompletedProcess(argv, 0, "", "")
    km.reset_runner = runner
    assert km.release_result(a.lease_id).ok
    assert A1 in resets[-1]
    assert grabbed["g"] is None                            # refused mid-reset
    assert gk.is_clean(A1) and gk.unit_lease(A1) is None


def test_a_partner_regranted_mid_reset_is_not_marked_clean(tmp_path, sysfs, monkeypatch):
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="a", pid=1, exact=A0)
    stranger = {"lease_id": "zzzzzz", "who": "intruder", "pid": 1}

    def runner(argv, **kw):
        resets.append(argv[2].split(","))
        # Simulate the lock changing hands while tt-smi runs.
        gk.release_unit(A1, expected_lease_id=a.lease_id)
        gk.claim_unit(A1, stranger)
        return subprocess.CompletedProcess(argv, 0, "", "")
    km.reset_runner = runner
    km.release_result(a.lease_id)
    assert not gk.is_clean(A1)
    assert gk.unit_lease(A1)["who"] == "intruder"


def test_an_interrupted_reset_leaves_no_partner_lock(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="a", pid=1, exact=A0)

    def boom(argv, **kw):
        raise KeyboardInterrupt
    km.reset_runner = boom
    with pytest.raises(KeyboardInterrupt):
        km.release_result(a.lease_id)
    assert gk.unit_lease(A1) is None                  # borrowed lock settled
    km.reset_runner = lambda argv, **kw: (resets.append(argv[2].split(",")),
                                          subprocess.CompletedProcess(argv, 0, "", ""))[1]
    assert km.release_result(a.lease_id).ok
    assert sorted(resets[-1]) == [A0, A1] and not gk.is_split(BOARD_A)


def test_a_partner_left_locked_under_our_own_id_is_ours_to_reset(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="a", pid=1, exact=A0)
    gk.claim_unit(A1, gk.read_lease(a.lease_id))      # left by a killed release
    km.release(a.lease_id)
    assert sorted(resets[-1]) == [A0, A1]
    assert gk.held_units() == {} and not gk.is_split(BOARD_A)


def test_simultaneous_sibling_releases_heal_the_board(tmp_path, sysfs):
    """Seen on hardware: two dies of one board released at the same moment
    each saw the other leased, so the board was left split. The release that
    finishes last now resets it whole."""
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="a", pid=1, exact=A0)
    b = km.acquire("1", who="b", pid=1, exact=A1)
    km2 = Keymaster(Gatekeeper(root=gk.root, sysfs_root=gk.sysfs_root,
                               proc_root=gk.proc_root))
    order = []

    def runner_a(argv, **kw):
        order.append(("a", argv[2].split(",")))
        if len(order) == 1:
            # B releases while A's reset is still running: A is leased.
            assert km2.release(b.lease_id)[0]
        return subprocess.CompletedProcess(argv, 0, "", "")

    def runner_b(argv, **kw):
        order.append(("b", argv[2].split(",")))
        return subprocess.CompletedProcess(argv, 0, "", "")
    km.reset_runner, km2.reset_runner = runner_a, runner_b
    assert km.release(a.lease_id)[0]
    assert order[0] == ("a", [A0]) and order[1] == ("b", [A1])   # both single-die
    assert sorted(order[-1][1]) == [A0, A1]                      # then one heal
    assert not gk.is_split(BOARD_A)
    assert gk.held_units() == {}
    assert gk.is_clean(A0) and gk.is_clean(A1)


def test_no_heal_while_the_partner_is_in_use(tmp_path, sysfs):
    km, gk, resets = make(tmp_path, sysfs)
    a = km.acquire("1", who="a", pid=1, exact=A0)
    km.acquire("1", who="b", pid=1, exact=A1)
    km.release(a.lease_id)
    assert resets == [[A0]] and gk.is_split(BOARD_A)
