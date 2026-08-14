"""Exercise TicketQueue directly, with no Gatekeeper involved.

This module intentionally imports only gozer.queue (never gozer.gatekeeper),
which is itself the structural proof that TicketQueue does not depend back on
Gatekeeper: if gozer/queue.py needed to import gozer/gatekeeper.py, importing
gozer.queue here would drag gatekeeper.py in transitively and (per the task
brief) create an import cycle that gatekeeper.py's own `import gozer.queue`
would then fail on. The fact this file runs at all with that one import is
the decoupling proof, alongside the behavioural assertions below.
"""
import contextlib
import os

import pytest

from gozer.queue import TicketQueue


def _fake_proc(tmp_path, pids):
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        (root / str(pid) / "fd").mkdir(parents=True)
        (root / str(pid) / "comm").write_text("python\n")
    return str(root)


def make_queue(tmp_path, critical_section=contextlib.nullcontext, live_pids=(1,)):
    """A TicketQueue built without any Gatekeeper: temp dir, a no-op mutex
    (contextlib.nullcontext is a trivial context-manager factory), and a fake
    proc_root for liveness checks."""
    return TicketQueue(
        queue_dir=str(tmp_path / "queue"),
        critical_section=critical_section,
        proc_root=_fake_proc(tmp_path, live_pids),
    )


def req(who, pid=1):
    return {"who": who, "pid": pid, "min_chips": 1, "max_chips": 1}


def test_full_enqueue_position_dequeue_cycle_without_a_gatekeeper(tmp_path):
    q = make_queue(tmp_path)
    a = q.enqueue(req("claude:a"))
    b = q.enqueue(req("claude:b"))

    assert [e["who"] for e in q.entries()] == ["claude:a", "claude:b"]
    assert q.position(a["ticket"]) == 1
    assert q.position(b["ticket"]) == 2

    q.dequeue(a["ticket"])

    assert q.position(a["ticket"]) is None
    assert q.position(b["ticket"]) == 1


def test_prune_drops_tickets_whose_process_is_gone(tmp_path):
    q = make_queue(tmp_path, live_pids=(1,))
    alive = q.enqueue(req("claude:alive", pid=1))
    dead = q.enqueue(req("claude:dead", pid=999))

    dropped = q.prune()

    assert dropped == [dead["ticket"]]
    assert [e["who"] for e in q.entries()] == ["claude:alive"]
    assert q.position(alive["ticket"]) == 1


class RecordingSection:
    """Stands in for Gatekeeper.critical_section: a real callable-returns-a-
    context-manager, but one that counts how many times it was entered, so a
    test can prove a method took the lock rather than merely not crashing."""

    def __init__(self):
        self.enters = 0

    @contextlib.contextmanager
    def __call__(self):
        self.enters += 1
        try:
            yield
        finally:
            pass


def test_dequeue_is_safe_to_call_standalone_outside_any_critical_section(tmp_path):
    """dequeue must take the mutex itself when called with no surrounding
    critical_section already held -- Task 9's cmd_cancel will call it exactly
    this way. Proven two ways: the lock is demonstrably taken (RecordingSection
    counts an entry), and the mutation (unlink + window close) still happens.
    """
    section = RecordingSection()
    q = TicketQueue(
        queue_dir=str(tmp_path / "queue"),
        critical_section=section,
        proc_root=_fake_proc(tmp_path, (1,)),
    )
    a = q.enqueue(req("claude:a"))
    q.open_claim_window(a["ticket"])

    enters_before = section.enters
    # No outer "with section():" here -- this is the standalone call site.
    q.dequeue(a["ticket"])

    assert section.enters > enters_before, (
        "dequeue() did not take the mutex when called standalone"
    )
    assert q.position(a["ticket"]) is None
    assert q._claim_window_ticket() is None


def test_dequeue_of_unknown_ticket_is_a_noop(tmp_path):
    q = make_queue(tmp_path)
    q.dequeue("does-not-exist")  # must not raise
