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
import json
import os

import pytest

from gozer.queue import TicketQueue


def make_queue(tmp_path, critical_section=contextlib.nullcontext):
    """A TicketQueue built without any Gatekeeper: a temp dir and a no-op
    mutex (contextlib.nullcontext is a trivial context-manager factory).

    Nothing else is needed -- the queue reads no /proc at all, judging every
    ticket by age (see TICKET_MAX_AGE_SECONDS)."""
    return TicketQueue(
        queue_dir=str(tmp_path / "queue"),
        critical_section=critical_section,
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


def test_prune_drops_only_what_has_aged_out(tmp_path):
    """Age is the only rule: a recorded pid, alive or dead, is irrelevant,
    because no gozer process survives to wait on a ticket."""
    q = make_queue(tmp_path)
    fresh = q.enqueue(req("claude:fresh", pid=999))
    old = q.enqueue(req("claude:old", pid=1))
    path = q._ticket_path(old["ticket"])
    rec = json.load(open(path))
    rec["since"] = "2000-01-01T00:00:00Z"
    json.dump(rec, open(path, "w"))

    dropped = q.prune()

    assert dropped == [old["ticket"]]
    assert [e["who"] for e in q.entries()] == ["claude:fresh"]
    assert q.position(fresh["ticket"]) == 1


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
