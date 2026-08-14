import os
import time
import pytest
from gozer.gatekeeper import Gatekeeper, CLAIM_WINDOW_SECONDS
from conftest import QUIETBOX


def fake_proc(tmp_path, pids):
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        (root / str(pid) / "fd").mkdir(parents=True)
        (root / str(pid) / "comm").write_text("python\n")
    return str(root)


def make(tmp_path, sysfs, live_pids=(1,)):
    return Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                      proc_root=fake_proc(tmp_path, live_pids))


def req(who, pid=1, lo=1, hi=1):
    return {"who": who, "pid": pid, "min_chips": lo, "max_chips": hi}


def test_enqueue_returns_ticket_and_preserves_fifo(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    a = gk.enqueue(req("claude:a"))
    b = gk.enqueue(req("claude:b"))
    assert [e["who"] for e in gk.queue_entries()] == ["claude:a", "claude:b"]
    assert gk.queue_position(a["ticket"]) == 1
    assert gk.queue_position(b["ticket"]) == 2


def test_fifo_survives_more_than_ten_entries(tmp_path, sysfs):
    # Zero-padded sequence numbers, so 10 must not sort before 9.
    gk = make(tmp_path, sysfs)
    tickets = [gk.enqueue(req(f"claude:{i}")) for i in range(12)]
    assert [e["who"] for e in gk.queue_entries()] == [f"claude:{i}" for i in range(12)]
    assert gk.queue_position(tickets[11]["ticket"]) == 12


def test_dequeue_removes_and_renumbers_positions(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    a = gk.enqueue(req("claude:a"))
    b = gk.enqueue(req("claude:b"))
    gk.dequeue(a["ticket"])
    assert gk.queue_position(b["ticket"]) == 1
    assert gk.queue_position(a["ticket"]) is None


def test_prune_drops_tickets_whose_process_is_gone(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, live_pids=(1,))
    alive = gk.enqueue(req("claude:alive", pid=1))
    dead = gk.enqueue(req("claude:dead", pid=999))
    dropped = gk.prune_queue()
    assert dropped == [dead["ticket"]]
    assert [e["who"] for e in gk.queue_entries()] == ["claude:alive"]
    assert gk.queue_position(alive["ticket"]) == 1


def test_claim_window_entitles_only_the_head(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    a = gk.enqueue(req("claude:a"))
    b = gk.enqueue(req("claude:b"))
    gk.open_claim_window(a["ticket"])
    assert gk.may_claim(a["ticket"]) is True
    assert gk.may_claim(b["ticket"]) is False


def test_no_window_open_means_anyone_may_claim(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    a = gk.enqueue(req("claude:a"))
    assert gk.may_claim(a["ticket"]) is True


def test_expired_window_moves_ticket_to_back(tmp_path, sysfs):
    """An abandoned window must not wedge the queue.

    Drives the clock by backdating the marker file rather than monkeypatching
    time.time(), which would also affect the code under test.
    """
    gk = make(tmp_path, sysfs)
    a = gk.enqueue(req("claude:a"))
    gk.enqueue(req("claude:b"))
    gk.open_claim_window(a["ticket"])
    marker = os.path.join(gk.root, "queue", ".claim-window")
    old = time.time() - CLAIM_WINDOW_SECONDS - 5
    os.utime(marker, (old, old))
    assert gk.claim_window_holder() is None
    assert [e["who"] for e in gk.queue_entries()] == ["claude:b", "claude:a"]
