"""The FIFO ticket queue: who is waiting for chips, and in what order.

Lives under GOZER_ROOT's `queue/` directory:

    queue/<seq>-<ticket>.json     FIFO tickets
    queue/.claim-window           marker naming the ticket currently entitled
                                   to claim freed chips

TicketQueue shares nothing with the rest of the gatekeeper but the directory
it operates in and the mutex it serialises under -- both are handed in by the
caller (see the constructor), rather than this module reaching back into a
Gatekeeper instance. That keeps the dependency one-directional: gatekeeper.py
composes and delegates to TicketQueue; TicketQueue never imports gatekeeper.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time

from gozer.jsonstore import (_atomic_write_json, _elapsed_seconds, _read_json,
                             utcnow)

CLAIM_WINDOW_SECONDS = 90

# How long a ticket lives without being claimed. Age is the *only* rule, for
# every ticket, because no gozer process ever survives to wait on one.
#
# There is no blocking run mode: `gozer acquire` prints its ticket and exits,
# and `gozer run` prints `queued: ticket X` and returns 10 just the same. So
# whatever pid a ticket records -- the one-shot CLI invocation, or the `run`
# process that supervises a lease it never got -- is dead moments later, and
# judging a ticket by pid liveness drops it at the very next prune. That is
# exactly what happened: `gozer wait X` came back "no longer queued" about a
# second after the ticket was issued, and the caller's place in line was
# silently lost. It is the same defect the detached *lease* fixed (see
# gatekeeper.DETACHED_GRACE_SECONDS), and the `run` path kept it alive for
# one more round because that ticket's pid looked supervised.
#
# Ticket supervision is a property of the *waiting*, not of the eventual
# lease -- and nothing waits. If a blocking run mode is ever added, that mode
# (and only that mode) can reintroduce a liveness check for its own tickets.
#
# An hour is deliberately generous. A queued agent is *expected* to go do
# other work -- that is the entire point of the ticket -- and `gozer wait`
# only blocks for 8 minutes at a time, so a patient agent may legitimately be
# away for several wait-and-work cycles. Dropping a ticket too early loses a
# real waiter's place in line; dropping it too late costs at most one
# 90-second claim window that nobody takes, after which the claim-window
# expiry already sends the ticket to the back of the queue.
#
# Known limitation (see the project CLAUDE.md): `since` is stamped at enqueue
# and never refreshed, so an agent polling correctly behind a multi-hour
# lease still loses its place at the one-hour mark.
TICKET_MAX_AGE_SECONDS = 3600


class TicketQueue:
    # Bound on retries when a freshly generated ticket collides with one
    # already in the queue. secrets.token_hex(2) draws from only 65,536
    # values, so a collision is unlikely but not negligible over a machine's
    # lifetime; _ticket_path matches on the first filename it finds, so an
    # undetected collision would silently operate on the wrong ticket.
    _TICKET_COLLISION_RETRIES = 20

    def __init__(self, queue_dir: str, critical_section):
        """
        queue_dir: the directory holding ticket files (Gatekeeper's
            `<root>/queue`; ensured to exist by Gatekeeper._ensure_dirs, which
            also sets the sticky bit -- but this constructor makes the
            directory itself if it is missing, so TicketQueue also works when
            built standalone, e.g. in tests, without a Gatekeeper around it).
        critical_section: a zero-argument callable returning a context
            manager, serialising mutation. Gatekeeper passes its own
            (reentrant) `critical_section` bound method, so the queue's
            locking is the same lock the rest of the gatekeeper uses.

        No `proc_root`: nothing in the queue reads /proc any more. It used to
        be here for prune()'s pid-liveness check, which is gone (see
        TICKET_MAX_AGE_SECONDS) -- and a parameter kept past its last reader
        is the same defect as a flag that is accepted and never read.
        """
        self.queue_dir = queue_dir
        self._critical_section = critical_section
        os.makedirs(self.queue_dir, exist_ok=True)

    # ---- internal path helpers --------------------------------------------

    def _next_seq(self) -> int:
        entries = [e for e in os.listdir(self.queue_dir) if e.endswith(".json")]
        seqs = []
        for e in entries:
            head = e.split("-", 1)[0]
            if head.isdigit():
                seqs.append(int(head))
        return (max(seqs) + 1) if seqs else 1

    def _ticket_path(self, ticket: str) -> str | None:
        for entry in os.listdir(self.queue_dir):
            if entry.endswith(f"-{ticket}.json"):
                return os.path.join(self.queue_dir, entry)
        return None

    # ---- queue --------------------------------------------------------------

    def enqueue(self, request: dict) -> dict:
        """Append a ticket. Sequence numbers are zero-padded so 10 sorts after 9."""
        with self._critical_section():
            seq = self._next_seq()
            existing = {r.get("ticket") for r in self.entries()}
            ticket = None
            for _ in range(self._TICKET_COLLISION_RETRIES):
                candidate = secrets.token_hex(2)
                if candidate not in existing:
                    ticket = candidate
                    break
            if ticket is None:
                raise RuntimeError(
                    "gozer: could not generate a unique queue ticket after "
                    f"{self._TICKET_COLLISION_RETRIES} attempts")
            record = dict(request)
            record.update({"ticket": ticket, "seq": seq, "since": utcnow()})
            path = os.path.join(self.queue_dir, f"{seq:06d}-{ticket}.json")
            _atomic_write_json(path, record)
            return record

    def entries(self) -> list[dict]:
        """FIFO order.

        A ticket can transiently have two files on disk (see _send_to_back,
        which writes the new record before removing the old one so a live
        ticket is never briefly absent). Dedupe by ticket, keeping the entry
        with the highest `seq` -- that is always the current, correct
        position; a lower-`seq` duplicate for the same ticket is leftover
        litter from an interrupted move, never the "true" one.
        """
        out = []
        for entry in sorted(os.listdir(self.queue_dir)):
            if not entry.endswith(".json"):
                continue
            rec = _read_json(os.path.join(self.queue_dir, entry))
            if rec:
                out.append(rec)
        best: dict[str, dict] = {}
        for rec in out:
            ticket = rec.get("ticket")
            if ticket is None:
                continue
            current = best.get(ticket)
            if current is None or rec.get("seq", 0) > current.get("seq", 0):
                best[ticket] = rec
        # This integer sort on the `seq` field IS the FIFO invariant. The
        # zero-padded filename (e.g. "000012-abcd.json") is cosmetic - it
        # only keeps `ls` output human-sorted - and is not what keeps the
        # queue ordered; do not remove this sort believing padding suffices.
        return sorted(best.values(), key=lambda r: r.get("seq", 0))

    def position(self, ticket: str) -> int | None:
        for i, rec in enumerate(self.entries(), start=1):
            if rec.get("ticket") == ticket:
                return i
        return None

    def dequeue(self, ticket: str) -> None:
        """Remove a ticket and close its claim window, if it holds one.

        Wrapped in critical_section so this is safe to call standalone, with
        no surrounding lock held -- Task 9's cmd_cancel does exactly that.
        prune() already held the section around its own call site, so this
        adds only a reentrant (free, since Gatekeeper's mutex is reentrant)
        nested acquire there, not a new lock.
        """
        with self._critical_section():
            path = self._ticket_path(ticket)
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            if self._claim_window_ticket() == ticket:
                self._close_claim_window()

    def _is_abandoned(self, rec: dict, now: str) -> bool:
        """Has this ticket been left behind? Judged by age, and only by age.

        Deliberately does *not* consult the ticket's `pid`, or its `detached`
        flag. No gozer process survives to wait on a ticket -- neither
        `acquire` nor `run` blocks -- so every recorded pid is dead moments
        after the ticket is written, and a liveness check deletes tickets
        that are working exactly as intended. See TICKET_MAX_AGE_SECONDS for
        the full reasoning and for what a future blocking mode would need.
        """
        since = rec.get("since")
        # A ticket with no `since` at all is malformed, not fresh: treat it
        # as maximally old rather than trusting it forever.
        elapsed = _elapsed_seconds(since, now) if since else float("inf")
        return elapsed > TICKET_MAX_AGE_SECONDS

    def prune(self) -> list[str]:
        """Drop tickets that have aged out (see _is_abandoned).

        Also sweeps up litter: _send_to_back writes a ticket's new record
        before removing its old one, so a crash mid-move can transiently
        leave two files for one ticket. entries() already tolerates that (it
        dedupes, keeping the higher `seq`), but nothing else ever removes the
        stale lower-`seq` file from disk -- so do that here, rather than
        leaving it to accumulate forever.
        """
        with self._critical_section():
            by_ticket: dict[str, list[tuple[str, dict]]] = {}
            for entry in sorted(os.listdir(self.queue_dir)):
                if not entry.endswith(".json"):
                    continue
                path = os.path.join(self.queue_dir, entry)
                rec = _read_json(path)
                if rec and rec.get("ticket"):
                    by_ticket.setdefault(rec["ticket"], []).append((path, rec))
            for ents in by_ticket.values():
                if len(ents) <= 1:
                    continue
                ents.sort(key=lambda pe: pe[1].get("seq", 0))
                for stale_path, _ in ents[:-1]:
                    with contextlib.suppress(OSError):
                        os.unlink(stale_path)

            dropped = []
            now = utcnow()
            for rec in self.entries():
                if self._is_abandoned(rec, now):
                    dropped.append(rec["ticket"])
                    self.dequeue(rec["ticket"])
            return dropped

    def _send_to_back(self, ticket: str) -> None:
        """Move a ticket to the back of the queue with a fresh sequence number.

        Writes the new record *before* removing the old one, and does both
        under critical_section. Writing first means the ticket can briefly
        exist twice on disk, but never zero times -- a crash or failed write
        between the two steps leaves a recoverable duplicate (entries() and
        prune() both know how to resolve it) rather than silently losing a
        live client's place in line, which is the one failure this whole
        mechanism exists to prevent.
        """
        with self._critical_section():
            path = self._ticket_path(ticket)
            rec = _read_json(path) if path else None
            if not rec:
                return
            seq = self._next_seq()
            rec = dict(rec)
            rec["seq"] = seq
            rec["requeued_at"] = utcnow()
            _atomic_write_json(
                os.path.join(self.queue_dir, f"{seq:06d}-{ticket}.json"), rec)
            with contextlib.suppress(OSError):
                os.unlink(path)

    # ---- claim window -----------------------------------------------------

    def _window_path(self) -> str:
        return os.path.join(self.queue_dir, ".claim-window")

    def _claim_window_ticket(self) -> str | None:
        try:
            with open(self._window_path()) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def _close_claim_window(self) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self._window_path())

    def open_claim_window(self, ticket: str) -> None:
        """Give this ticket an exclusive window to claim the freed chips."""
        with open(self._window_path(), "w") as f:
            f.write(ticket + "\n")

    def expire_and_get_claim_holder(self) -> str | None:
        """The entitled ticket, or None.

        Named for the side effect, not just the read: an expired window is
        closed AND has its ticket sent to the back of the queue (never just
        closed alone -- that would let the same dead ticket re-win the very
        next check). Both mutations happen under critical_section so two
        concurrent callers (every waiter polls this via may_claim) cannot
        each observe the same expired window and each requeue the ticket
        under a different sequence number, which would leave two live
        entries for one ticket.
        """
        ticket = self._claim_window_ticket()
        if ticket is None:
            return None
        with self._critical_section():
            # Re-check inside the lock: another caller may have already
            # expired (and requeued, or simply closed via dequeue) this
            # exact window while we were waiting for the mutex.
            ticket = self._claim_window_ticket()
            if ticket is None:
                return None
            try:
                age = time.time() - os.stat(self._window_path()).st_mtime
            except OSError:
                return None
            if age > CLAIM_WINDOW_SECONDS:
                self._close_claim_window()
                self._send_to_back(ticket)
                return None
            return ticket

    def may_claim(self, ticket: str | None) -> bool:
        """True when no window is open, or this ticket owns it."""
        holder = self.expire_and_get_claim_holder()
        return holder is None or holder == ticket
