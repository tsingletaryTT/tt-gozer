import json
import os
import time
import pytest
import gozer.gatekeeper as gatekeeper
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
    # FIFO order comes from the integer `seq` field (queue_entries sorts on
    # it numerically), not from filename order -- 9 would sort before 10
    # either way. The zero-padded filename is cosmetic: it only keeps `ls`
    # output human-sorted for anyone inspecting queue/ by hand.
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


def test_prune_ignores_pids_entirely(tmp_path, sysfs):
    """Ticket liveness is not a pid question, and never was answerable as one.

    No gozer process survives to wait on a ticket -- `acquire` and `run` both
    print a ticket and exit -- so every recorded pid is dead moments later.
    Pruning on that dropped tickets that were working exactly as intended,
    which is why age is now the only rule.
    """
    gk = make(tmp_path, sysfs, live_pids=(1,))
    alive = gk.enqueue(req("claude:alive", pid=1))
    dead = gk.enqueue(req("claude:dead", pid=999))
    assert gk.prune_queue() == []
    assert [e["who"] for e in gk.queue_entries()] == ["claude:alive", "claude:dead"]
    assert gk.queue_position(alive["ticket"]) == 1
    assert gk.queue_position(dead["ticket"]) == 2


def _backdate_ticket(gk, ticket, since):
    path = gk._ticket_path(ticket)
    with open(path) as f:
        rec = json.load(f)
    rec["since"] = since
    with open(path, "w") as f:
        json.dump(rec, f)


def test_prune_keeps_a_ticket_whose_recorded_pid_is_gone(tmp_path, sysfs):
    """The detached-lease bug, in its ticket form.

    A ticket records the pid of the one-shot CLI process that took it, which
    exits immediately. Judging it by pid liveness deletes it milliseconds
    after it is issued -- on a real box `gozer wait` came back "no longer
    queued" about a second later, and the caller's place in line was gone.
    """
    gk = make(tmp_path, sysfs, live_pids=(1,))
    t = gk.enqueue({**req("claude:detached", pid=999), "detached": True})
    assert gk.prune_queue() == []
    assert gk.queue_position(t["ticket"]) == 1


def test_prune_drops_a_ticket_past_its_max_age(tmp_path, sysfs):
    """Tickets are not immortal either -- they age out, so an abandoned
    waiter cannot sit at the head of the queue forever."""
    from gozer.queue import TICKET_MAX_AGE_SECONDS
    assert TICKET_MAX_AGE_SECONDS > 0
    gk = make(tmp_path, sysfs, live_pids=(1,))
    t = gk.enqueue({**req("claude:abandoned", pid=999), "detached": True})
    _backdate_ticket(gk, t["ticket"], "2000-01-01T00:00:00Z")

    assert gk.prune_queue() == [t["ticket"]]
    assert gk.queue_position(t["ticket"]) is None


def test_a_supervised_looking_ticket_ages_out_like_any_other(tmp_path, sysfs):
    """`detached: False` on a ticket buys it nothing -- neither immortality
    nor an early death. A `gozer run` ticket is written by a process that
    returns 10 and exits, so it gets exactly the same age rule."""
    gk = make(tmp_path, sysfs, live_pids=(1,))
    fresh = gk.enqueue({**req("run:supervised", pid=999), "detached": False})
    old = gk.enqueue({**req("run:forgotten", pid=999), "detached": False})
    _backdate_ticket(gk, old["ticket"], "2000-01-01T00:00:00Z")

    assert gk.prune_queue() == [old["ticket"]]
    assert gk.queue_position(fresh["ticket"]) == 1


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
    assert gk.expire_and_get_claim_holder() is None
    assert [e["who"] for e in gk.queue_entries()] == ["claude:b", "claude:a"]


def test_queue_entries_dedupes_a_ticket_left_with_two_files(tmp_path, sysfs):
    """_send_to_back writes the new record before deleting the old one, so a
    crash between those two steps can leave two files for one ticket. Prove
    queue_entries() resolves that to a single entry at the *higher* sequence
    (its current, correct position) rather than double-counting it or
    reverting the requeue by picking the lower one."""
    gk = make(tmp_path, sysfs)
    a = gk.enqueue(req("claude:a"))
    gk.enqueue(req("claude:b"))

    # Simulate a crash mid-move: hand-write a second, higher-seq copy of
    # ticket a's record without removing the original lower-seq file.
    old_path = gk._ticket_path(a["ticket"])
    with open(old_path) as f:
        rec = json.load(f)
    rec["seq"] = 99
    new_path = os.path.join(gk.root, "queue", f"000099-{a['ticket']}.json")
    with open(new_path, "w") as f:
        json.dump(rec, f)

    entries = gk.queue_entries()
    tickets = [e["ticket"] for e in entries]
    assert tickets.count(a["ticket"]) == 1
    assert [e["seq"] for e in entries if e["ticket"] == a["ticket"]] == [99]
    # The duplicate ticket now sorts last (highest seq), behind claude:b.
    assert [e["who"] for e in entries] == ["claude:b", "claude:a"]


def test_prune_queue_removes_stale_duplicate_ticket_file(tmp_path, sysfs):
    """prune_queue must also sweep up the lower-seq litter file left behind
    by a crash mid-_send_to_back, not just drop dead-pid tickets."""
    gk = make(tmp_path, sysfs)
    a = gk.enqueue(req("claude:a"))

    old_path = gk._ticket_path(a["ticket"])
    with open(old_path) as f:
        rec = json.load(f)
    rec["seq"] = 99
    new_path = os.path.join(gk.root, "queue", f"000099-{a['ticket']}.json")
    with open(new_path, "w") as f:
        json.dump(rec, f)

    gk.prune_queue()

    remaining = [e for e in os.listdir(os.path.join(gk.root, "queue"))
                 if e.endswith(".json")]
    assert remaining == [f"000099-{a['ticket']}.json"]
    assert not os.path.exists(old_path)


def test_enqueue_retries_on_ticket_id_collision(tmp_path, sysfs, monkeypatch):
    """secrets.token_hex(2) has only 65,536 possible values. If a collision
    is not detected, _ticket_path's first-match lookup would silently
    operate on the wrong ticket. Force a collision and confirm enqueue
    retries until it finds a value not already in the queue."""
    gk = make(tmp_path, sysfs)
    tokens = iter(["ab12", "ab12", "cd34"])
    monkeypatch.setattr(gatekeeper.secrets, "token_hex", lambda n: next(tokens))

    a = gk.enqueue(req("claude:a"))
    assert a["ticket"] == "ab12"

    b = gk.enqueue(req("claude:b"))
    assert b["ticket"] == "cd34"  # retried past the collision with "ab12"


def test_enqueue_raises_when_no_unique_ticket_can_be_found(tmp_path, sysfs, monkeypatch):
    """If every attempt collides, fail loudly rather than silently reusing
    or corrupting another ticket's record."""
    gk = make(tmp_path, sysfs)
    monkeypatch.setattr(gatekeeper.secrets, "token_hex", lambda n: "dead")
    gk.enqueue(req("claude:a"))  # ticket "dead" is now taken

    with pytest.raises(RuntimeError):
        gk.enqueue(req("claude:b"))
