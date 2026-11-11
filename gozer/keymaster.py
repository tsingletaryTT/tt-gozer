# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""The keymaster: carries the key.

Turns a request ("I need one chip") into a lease, the TT_VISIBLE_DEVICES the
caller must honour, and -- for `gozer run` -- a supervised process whose lease
cannot leak, because release happens in a finally block and on signal.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from gozer import history, procfd
from gozer import queue as queue_mod
from gozer import reset as reset_mod
from gozer.gatekeeper import Gatekeeper, parse_chip_request, utcnow
from gozer.jsonstore import _elapsed_seconds
from gozer.topology import Chip, all_chips

DURATION_RE = re.compile(r"^(\d+)([smh]?)$")


class TicketNotFound(ValueError):
    """A `--ticket` was supplied that is not (or no longer) in the queue.

    A ValueError subclass so cli.main's existing catch-all still handles it
    if a future command forgets to; cli maps it to the "no such lease or
    ticket" exit code explicitly.
    """


def parse_duration(text: str) -> int:
    """'90s' -> 90, '45m' -> 2700, '2h' -> 7200, bare digits mean minutes."""
    m = DURATION_RE.match((text or "").strip().lower())
    if not m:
        raise ValueError(f"bad duration: {text!r} (want 90s, 45m, or 2h)")
    value, unit = int(m.group(1)), m.group(2) or "m"
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


@dataclass
class Grant:
    lease_id: str
    units: list[str]
    chips: list[Chip]
    bdfs: list[str]
    dev_indices: list[int]
    requested: int
    expanded: bool = False
    neighbours: dict[str, str] = field(default_factory=dict)


@dataclass
class ResetResult:
    """Outcome of Keymaster.reset. `status` is one of: "reset" (ok),
    "not-found", "refused" (nothing was done), "failed" (tt-smi ran and
    failed) and "changed-hands" (the reset ran, and afterwards the lease was
    found gone or taken by another lease)."""
    ok: bool
    status: str
    message: str


@dataclass
class ReleaseResult:
    """Outcome of Keymaster.release_result. `status` is one of: "released"
    (torn down; reset ran and succeeded, or was not asked for), "reset-failed"
    (torn down, but the reset ran and failed, so the units are NOT marked
    clean), "not-found" (no such lease), "refused"
    (nothing was done: a device is still open, or the units belong to another
    lease) and "changed-hands" (the reset ran, then the units were found to
    belong to another lease, whose lock was left alone).

    `ok` is True when the lease was torn down, "released" or "reset-failed".
    `units` and `recovery` are filled for "reset-failed" only; `recovery` is
    the exact commands that put those units through a reset.
    """
    ok: bool
    status: str
    message: str
    units: list = field(default_factory=list)
    recovery: str = ""
    # For "not-found": what history knows about the lease, if anything.
    reaped_at: str | None = None
    reaped_why: str | None = None


@dataclass
class ResetAllResult:
    """Outcome of Keymaster.reset_all. `status` is one of: "reset" (tt-smi
    succeeded and every chip reads normally), "reset-incomplete" (tt-smi
    succeeded but some chips still read all ones), "failed" (tt-smi failed) and
    "busy" (something on the box is in use, so nothing was run). `unreadable`
    lists the BDFs still unreadable afterwards."""
    ok: bool
    status: str
    message: str
    unreadable: list = field(default_factory=list)
    lease_id: str | None = None


# How long `gozer run`'s own release waits for a device to close before it
# refuses. A command that exits fast (a compile error, a crash) can leave a
# forked worker or a helper holding the device for a moment after the command
# itself is reaped. Those holders end within a second or two, and a refusal
# here leaves a stale lease and chips that were never reset. A holder that is
# still there after this long is treated as real work and the refusal stands.
# `gozer release` by hand does not wait: its caller chose the moment.
RELEASE_SETTLE_SECONDS = 10.0

# When `gozer run` looks for its command holding devices outside the grant:
# soon after start (most servers open their devices while starting up), then
# twice more for slow starters that load weights before opening the device.
FENCE_CHECK_SECONDS = (5, 30, 120)
RELEASE_SETTLE_POLL_SECONDS = 0.25


class Keymaster:
    def __init__(self, gatekeeper: Gatekeeper):
        self.gk = gatekeeper
        # Swappable so tests never shell out to a real tt-smi.
        self.reset_runner = subprocess.run
        # Swappable so tests never really wait (see RELEASE_SETTLE_SECONDS).
        self.sleep = time.sleep
        self.monotonic = time.monotonic

    # ---- acquire ----------------------------------------------------------

    def acquire(self, chips_spec: str, who: str, reason: str | None = None,
                exact: str | None = None, fresh: bool = False,
                expect: str | None = None, pid: int | None = None,
                ticket: str | None = None, no_queue: bool = False,
                owner_pid: int | None = None,
                include_degraded: bool = False):
        """Return a Grant, a queue-ticket dict, or None.

        `owner_pid` is the supervisor pid the caller asked to hold the lease
        (`gozer acquire --owner-pid`). It is written on the ticket, if one is
        taken, so that `gozer wait` can replay it; the grant itself uses `pid`.
        It is separate from `pid` because `gozer run` also passes a `pid`, and
        its own pid is gone by the time the ticket is replayed.

        None means "nothing was available and you asked not to be queued"
        (`--no-queue`). That decision has to be made *here*, before the
        enqueue: taking a ticket and then having the caller discard it leaves
        an orphan on disk that nobody is waiting on, and the head of the queue
        is exactly where an orphan does the most damage -- `release` opens a
        90-second exclusive claim window for it, which nobody ever claims, so
        every real acquirer stalls for 90 seconds per release.

        A caller that supplies no `pid` is producing a *detached* lease: one
        with no process gozer can supervise (the bare `gozer acquire` CLI
        path -- it prints its export line and exits immediately, so its own
        pid is meaningless a moment later). `detached` is recorded on the
        resulting lease and drives how Gatekeeper.reconcile decides CLAIMED
        vs STALE for leases with no fd open yet (see DETACHED_GRACE_SECONDS).
        `gozer run` legitimately owns a live, supervised process for the
        lease's whole lifetime, so it passes its own pid explicitly (see
        Keymaster.run) and gets ordinary process-death detection instead.
        """
        detached = pid is None
        pid = pid if pid is not None else os.getpid()
        total = len(all_chips(self.gk.boards))
        min_chips, max_chips = parse_chip_request(chips_spec, total)
        # Everything a later `gozer wait` needs to replay this exact request
        # on the caller's behalf. Recording only min/max chips (as this used
        # to) silently dropped --exact, --fresh, --reason and --expect, so a
        # queued agent that asked for one specific chip, or for reset silicon,
        # got whichever board happened to free first -- while the keymaster
        # skill actively teaches both flags.
        #
        # min_chips/max_chips stay alongside chips_spec because `gozer status`
        # and `gozer queue` render them, and because they are what a human
        # reading queue/*.json by hand wants to see.
        #
        # `detached` is recorded as True on *every* ticket, whatever the lease
        # would have been, because a ticket's supervision is a property of the
        # waiting and nothing waits: `gozer run` prints "queued: ticket X" and
        # returns 10 exactly like `gozer acquire` does, so its pid is dead
        # moments later too. Recording it honestly (rather than inheriting the
        # lease's `detached`) is what stops a `run` ticket being pruned as a
        # dead waiter one second after the user is told to `gozer wait` on it.
        # The field is informational -- the queue judges every ticket by age
        # regardless (queue.TICKET_MAX_AGE_SECONDS) -- but it keeps
        # queue/*.json honest for anyone reading it by hand.
        request = {
            "who": who, "pid": pid, "detached": True,
            "chips_spec": chips_spec,
            "min_chips": min_chips, "max_chips": max_chips,
            "exact": exact, "fresh": fresh,
            "reason": reason, "expect": expect,
            "owner_pid": owner_pid,
        }

        with self.gk.critical_section():
            self.gk.prune_queue()
            if ticket:
                # Every replay of a ticket is its waiter polling: keep it
                # alive and keep its reservation (queue.RESERVE_ACTIVE_SECONDS).
                self.gk.touch_ticket(
                    ticket, min_interval=queue_mod.TOUCH_MIN_INTERVAL_SECONDS)

            # A ticket-holder outside its claim window must wait its turn, and a
            # newcomer must not jump an open window.
            if not self.gk.may_claim(ticket):
                return self._queue_or_refuse(ticket, request, no_queue)

            free = self.gk.free_units(include_degraded=include_degraded)
            # Units held back for active tickets ahead of this caller: every
            # ticket for a newcomer, the earlier ones for a ticket-holder.
            # reset-all (include_degraded) is exempt: it never queues, needs
            # the whole box, and returns it reset moments later.
            reserved: dict[str, str] = {}
            # The claim-window holder was picked by release as the first
            # ticket the freed units fit; a ticket ahead that has polled since
            # must not take the units back from under it mid-window.
            holds_window = bool(ticket) and (
                self.gk.queue._claim_window_ticket() == ticket)
            if not include_degraded and not holds_window:
                own_seq = None
                if ticket:
                    own_seq = next((e.get("seq") for e in self.gk.queue_entries()
                                    if e.get("ticket") == ticket), None)
                # An unknown ticket is a newcomer: every reservation applies.
                # (It is refused as TicketNotFound if it does not fit anyway.)
                reserved, _ = self.gk.plan_queue(free=free,
                                                 before_seq=own_seq)
            units = self.gk.allocate(min_chips, max_chips, exact=exact, fresh=fresh,
                                     include_degraded=include_degraded,
                                     exclude=reserved, free=free)
            if units is None:
                return self._queue_or_refuse(ticket, request, no_queue)

            chips = [c for u in units for c in self.gk.chips_in_unit(u)]
            chips.sort(key=lambda c: c.dev_index)
            lease_id = self.gk.new_lease_id()
            lease = {
                "lease_id": lease_id,
                "chips": [c.bdf for c in chips],
                "dev_indices": [c.dev_index for c in chips],
                "units": units,
                "board_serial": chips[0].serial if chips else None,
                "who": who,
                "human": _current_user(),
                "host": socket.gethostname(),
                "pid": pid,
                "pgid": _pgid(pid),
                "detached": detached,
                "session": os.environ.get("CLAUDE_SESSION_ID", ""),
                "cwd": os.getcwd(),
                "reason": reason,
                "since": utcnow(),
                "expect_done": _deadline(expect),
                "reset_on_release": True,
                "state": "active",
            }

            for unit in units:
                if not self.gk.claim_unit(unit, lease):
                    # Lost a race inside the critical section: roll back cleanly.
                    # Only the units already claimed need release_unit; none of
                    # them have had clear_clean called yet (that now happens in
                    # its own loop below, only once every unit in the request
                    # has been won), so a clean unit that was never touched by
                    # this aborted request is not wrongly left marked dirty.
                    for done in units:
                        if done == unit:
                            break
                        # Ours by construction (claimed moments ago, in this
                        # same critical section) -- but name the lease_id
                        # anyway, so a rollback can never widen into deleting
                        # a lock that is not this request's.
                        self.gk.release_unit(done, expected_lease_id=lease_id)
                    return self._queue_or_refuse(ticket, request, no_queue)

            # Every unit in the request is claimed now -- only at this point is
            # it safe to clear each one's clean marker. Doing this inside the
            # claim loop above would mark an early-won unit dirty even if a
            # later unit in the same request lost its race and the whole
            # request rolled back, stranding a needless future reset on a
            # board no workload ever touched.
            for unit in units:
                self.gk.clear_clean(unit)

            self.gk.write_lease(lease)
            if ticket:
                self.gk.dequeue(ticket)

            grant = Grant(
                lease_id=lease_id,
                units=units,
                chips=chips,
                bdfs=[c.bdf for c in chips],
                dev_indices=[c.dev_index for c in chips],
                requested=min_chips,
                # "Expanded" means the grain forced more chips on the caller
                # than they could possibly have wanted -- the UMD
                # board-expansion note. Comparing against the *minimum* made
                # every satisfied elastic request look expanded: `--chips 1-3`
                # granted one 2-chip board would print "asked for 1 -- UMD expands
                # TT_VISIBLE_DEVICES to the whole board", which is simply
                # false; 2 is inside what was asked for. The maximum is the
                # boundary that matters.
                expanded=len(chips) > max_chips,
                neighbours=self.gk.eth_neighbours(units),
            )
            history.log(self.gk.history_root, "granted", lease_id=lease_id, who=who,
                       reason=reason, chips=grant.bdfs,
                       dev_indices=grant.dev_indices, units=units,
                       detached=detached, pid=pid, expanded=grant.expanded,
                       requested=min_chips)
            return grant

    def _queue_or_refuse(self, ticket: str | None, request: dict,
                         no_queue: bool) -> dict | None:
        """Take (or reuse) a ticket -- unless the caller refused to queue.

        The refusal has to happen before the enqueue, not after: a ticket
        created and then abandoned by its caller is an orphan nobody waits
        on. See acquire's docstring for what an orphan at the head of the
        queue costs.
        """
        if no_queue:
            return None
        return self._ticket_for(ticket, request)

    def _ticket_for(self, ticket: str | None, request: dict) -> dict:
        """Reuse the caller's existing ticket, or take a new one.

        A caller who supplied a ticket that is not in the queue gets an error,
        never a replacement. Silently minting a new ticket under the same call
        was how `gozer wait` lost people's place in line: the caller went on
        waiting on a ticket id that no longer existed while a fresh, unknown
        ticket sat at the back of the queue as litter.
        """
        if ticket:
            for rec in self.gk.queue_entries():
                if rec.get("ticket") == ticket:
                    return rec
            raise TicketNotFound(
                f"ticket {ticket} is not in the queue -- it was cancelled, "
                "granted, or abandoned long enough to be pruned; acquire "
                "again to take a new one")
        rec = self.gk.enqueue(request)
        # Logged only for a brand-new ticket, not a reused one (the `if
        # ticket:` branch above): reuse is `wait` replaying an existing
        # queued request, which was already logged once when it was first
        # enqueued.
        history.log(self.gk.history_root, "queued", ticket=rec["ticket"],
                   who=request["who"], min_chips=request["min_chips"],
                   max_chips=request["max_chips"])
        return rec

    # ---- release ----------------------------------------------------------

    def _units_held_elsewhere(self, units: list[str],
                              lease_id: str) -> dict[str, str]:
        """Units whose gate lock now carries a *different* lease than ours.

        A unit with no lock at all is deliberately not in here: that means
        nobody holds it, so there is no other tenant to harm and our own
        bookkeeping can still be cleaned up. Only a lock naming someone else
        is the dangerous case -- see release().
        """
        out: dict[str, str] = {}
        for unit in units:
            current = self.gk.unit_lease(unit)
            if current is not None and current.get("lease_id") != lease_id:
                out[unit] = current.get("lease_id") or "unknown"
        return out

    def release(self, lease_id: str, no_reset: bool = False,
                force: bool = False, settle: float = 0.0) -> tuple[bool, str]:
        """`release_result` as the (ok, message) pair older callers expect."""
        r = self.release_result(lease_id, no_reset=no_reset, force=force,
                                settle=settle)
        return r.ok, r.message

    def release_result(self, lease_id: str, no_reset: bool = False,
                       force: bool = False,
                       settle: float = 0.0) -> ReleaseResult:
        """Give the chips back: verify, reset, unlock, notify the queue.

        The ordering here is the whole point, and it is not obvious.

        A lease record is not proof that the gate still holds it. Stale
        leases get released by hand (the gatekeeper skill tells a human or a
        successor agent to do exactly that), and `reconcile` reaps them
        automatically -- after which another agent's `acquire` may already own
        the unit. Meanwhile the /proc scan below takes hundreds of
        milliseconds and `tt-smi -r` takes tens of seconds. Without
        revalidation the sequence "read lease, scan /proc, reset" happily runs
        a reset on someone else's chips mid-startup, then deletes their lock
        and opens a claim window on it.

        So the gate is re-read under the mutex immediately before the reset,
        and again before the teardown -- but the mutex is deliberately NOT
        held across the reset itself, which would block every other user of
        the box for the tens of seconds tt-smi takes. The residual window
        (between the second check and the actual unlink) is microseconds of
        local file I/O, and release_unit's own lease_id guard covers it.

        The chips to reset and the device indices to scan are derived from the
        lease's units through the current topology. The record's own `chips`
        and `dev_indices` lists are never used for either. The gate checks
        look at `units` only, so a record whose lists reach past its units
        would otherwise reset another tenant's chips with every check
        passing, and a record missing a list would crash before the lease was
        cleaned up. `reset` closes the same hole by refusing; release instead
        proceeds with the derived chips, notes the disagreement and logs it,
        because it must stay able to clean up a damaged record. A unit the
        topology does not know gives no chips, so the reset is skipped and
        the unit is not marked clean. A malformed chip id (`ResetError`) is
        treated as a failed reset.
        """
        lease = self.gk.read_lease(lease_id)
        if lease is None:
            return self._missing_lease(lease_id)
        units = lease.get("units", [])
        derived = [c for u in units for c in self.gk.chips_in_unit(u)]
        derived_bdfs = [c.bdf for c in derived]
        derived_devs = [c.dev_index for c in derived]
        messages = []
        recorded_chips = lease.get("chips")
        recorded_devs = lease.get("dev_indices")
        # A missing list is only reported when it matters: the chips list is
        # compared as a set, the device list likewise.
        record_mismatch = (
            (recorded_chips is not None
             and sorted(map(str, recorded_chips)) != sorted(derived_bdfs))
            or (recorded_devs is not None
                and sorted(map(str, recorded_devs)) != sorted(map(str, derived_devs))))
        if record_mismatch:
            messages.append(
                f"lease record lists chips {list(recorded_chips or [])} but its "
                f"units have {derived_bdfs}; using the units")
            history.log(self.gk.history_root, "release_record_mismatch",
                       lease_id=lease_id, who=lease.get("who"),
                       recorded_chips=recorded_chips, derived_chips=derived_bdfs,
                       recorded_dev_indices=recorded_devs,
                       derived_dev_indices=derived_devs)

        fd_map = procfd.holders(self.gk.proc_root)
        still_open = [d for d in derived_devs if fd_map.get(d)]
        waited = 0.0
        if still_open and not force and settle > 0:
            # Give a just-exited command's leftovers a bounded moment to let
            # go. Every pass re-reads /proc, so a holder that is still alive
            # at the end is seen as alive and the refusal below still fires.
            start = self.monotonic()
            while still_open and self.monotonic() - start < settle:
                self.sleep(RELEASE_SETTLE_POLL_SECONDS)
                fd_map = procfd.holders(self.gk.proc_root)
                still_open = [d for d in derived_devs if fd_map.get(d)]
            waited = self.monotonic() - start
            if not still_open:
                messages.append(
                    f"devices were still open for {waited:.1f} s after the "
                    "command exited; released once they closed")
        if still_open and not force:
            history.log(self.gk.history_root, "refused", action="release",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="device still open", dev_indices=still_open,
                       waited_s=round(waited, 2))
            return ReleaseResult(False, "refused", (
                f"chips {still_open} still open by "
                f"{sorted({p for d in still_open for p in fd_map[d]})}"
                + (f" after waiting {waited:.1f} s" if waited else "")
                + " -- let the work finish, or pass --force"))

        # First revalidation: are these units still ours, right now, with
        # nobody able to claim them while we look?
        board_wide: list[str] = []      # partner units borrowed for this reset
        with self.gk.critical_section():
            foreign = self._units_held_elsewhere(units, lease_id)
            unheld = [u for u in units
                      if self.gk.unit_lease(u) is None]
            if not foreign and not unheld and derived_bdfs and not no_reset:
                board_wide = self._lock_free_partners(units, lease)
        if foreign:
            history.log(self.gk.history_root, "refused", action="release",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="units now belong to another lease",
                       foreign=foreign)
            return ReleaseResult(False, "refused", (
                "refusing to release: " +
                ", ".join(f"{unit} now belongs to lease {other}"
                          for unit, other in sorted(foreign.items())) +
                f" -- lease {lease_id} was already reaped or released; "
                "nothing was reset"))

        reset_ran = False
        reset_ok = False
        partner_bdfs = [c.bdf for u in board_wide
                        for c in self.gk.chips_in_unit(u)]
        reset_bdfs = derived_bdfs + partner_bdfs
        try:
            return self._release_after_lock(
                lease, lease_id, units, derived_bdfs, reset_bdfs,
                board_wide, unheld, no_reset, messages)
        finally:
            # Whatever happened -- including an interrupt during the reset --
            # a borrowed partner must not stay locked. Normally
            # _release_after_lock has already settled it and this is a no-op
            # (release_unit only drops a lock still carrying our id).
            if board_wide:
                with self.gk.critical_section():
                    for unit in board_wide:
                        self.gk.release_unit(unit, expected_lease_id=lease_id)

    def _release_after_lock(self, lease, lease_id, units, derived_bdfs,
                            reset_bdfs, board_wide, unheld, no_reset,
                            messages) -> ReleaseResult:
        """release_result from the reset onward (split out so the borrowed
        partner units can be settled in a finally around it)."""
        reset_ran = False
        reset_ok = False
        partner_bdfs = [b for b in reset_bdfs if b not in derived_bdfs]
        if not derived_bdfs and not no_reset:
            messages.append(
                f"not resetting: lease {lease_id} has no chips "
                "(its units are empty or unknown to the topology)")
        elif unheld and not no_reset:
            # Our lock is gone but nobody else has taken these units yet.
            # Resetting now would fire at chips that are free for anyone to
            # grab, so skip it and just clean up our own bookkeeping.
            messages.append(
                f"not resetting: {', '.join(sorted(unheld))} no longer locked "
                f"by lease {lease_id} (already reaped?)")
        elif not no_reset:
            reset_ran = True
            partial = self._partial_boards(reset_bdfs)
            if partner_bdfs:
                messages.append(
                    "board partner free: resetting the whole board "
                    f"({', '.join(partner_bdfs)} too) so its on-board link "
                    "comes back up")
            if partial:
                messages.append(
                    "board partner in use: resetting only this die; "
                    f"board {', '.join(partial)} needs a "
                    "whole-board reset before a mesh over both dies")
            try:
                reset_ok, out = reset_mod.reset_chips(reset_bdfs,
                                                      runner=self.reset_runner)
            except reset_mod.ResetError as e:
                # A chip id that is not a PCI address. Count it as a failed
                # reset so the lease is still torn down.
                reset_ok, out = False, f"reset refused: {e}"
            messages.append(out or ("reset ok" if reset_ok else "reset failed"))
            if not reset_ok:
                messages.append("reset failed -- unit released but NOT marked clean")

        # Second revalidation: the reset above took real time, so re-check
        # before deleting any lock or handing the chips to the queue.
        with self.gk.critical_section():
            # The partner units were only borrowed for the reset: settle them
            # first, whatever happens to our own units below.
            for unit in board_wide:
                # Clean only if the lock is still ours: a unit that was
                # reaped and granted on mid-reset belongs to its new tenant.
                still_ours = ((self.gk.unit_lease(unit) or {}).get("lease_id")
                              == lease_id)
                if reset_ok and still_ours:
                    self.gk.mark_clean(unit)
                self.gk.release_unit(unit, expected_lease_id=lease_id)
            split_marked: list[str] = []
            if reset_ran and reset_ok:
                _, split_marked = self.gk.settle_split(reset_bdfs)
            foreign = self._units_held_elsewhere(units, lease_id)
            if foreign:
                history.log(self.gk.history_root, "refused", action="release",
                           lease_id=lease_id, who=lease.get("who"),
                           reason="units now belong to another lease "
                                  "(discovered after the reset)",
                           foreign=foreign)
                return ReleaseResult(False, "changed-hands", (
                    "; ".join(messages + [
                        "aborted before teardown: " +
                        ", ".join(f"{unit} now belongs to lease {other}"
                                  for unit, other in sorted(foreign.items())) +
                        " -- leaving the new tenant's lock alone"])))

            if reset_ok:
                for unit in units:
                    self.gk.mark_clean(unit)
            for unit in units:
                self.gk.release_unit(unit, expected_lease_id=lease_id)
            self.gk.delete_lease(lease_id)

            # Hand the freed chips to the first waiter they actually fit.
            # Prune first so a ticket whose waiter is demonstrably gone cannot
            # be handed a 90-second exclusive window nobody will ever claim.
            # The head used to get the window unconditionally: a head that
            # wanted more than was free blocked everyone for 90 s, then was
            # sent to the back of the queue. A head that does not fit now
            # keeps its place and holds the freed units instead (plan_queue).
            self.gk.prune_queue()
            _, fits = self.gk.plan_queue()
            if fits:
                who = next((e["who"] for e in self.gk.queue_entries()
                            if e["ticket"] == fits[0]), fits[0])
                self.gk.open_claim_window(fits[0])
                messages.append(f"claim window opened for {who}")

        since = lease.get("since")
        duration_s = _elapsed_seconds(since, utcnow()) if since else None
        history.log(self.gk.history_root, "released", lease_id=lease_id,
                   who=lease.get("who"), chips=derived_bdfs,
                   duration_s=duration_s, reset_ran=reset_ran,
                   reset_ok=reset_ok if reset_ran else None,
                   **({"board_reset": sorted(board_wide)} if board_wide else {}),
                   **({"split": sorted(split_marked)} if split_marked else {}))
        # A reset that failed, or was skipped because the units gave no chips,
        # leaves the lease torn down and the silicon unreset. That is not a
        # clean release, whatever the exit used to say: the units are not
        # marked clean, so --fresh refuses them, and the caller is told how to
        # put them through a reset.
        needs_reset = reset_ran and not reset_ok
        if split_marked and not needs_reset:
            for serial in self._heal_split_boards(split_marked, lease):
                messages.append(f"board {serial}: every die is free now, so it was "
                                "reset whole and is no longer split")
        if needs_reset:
            recovery = self._recovery_commands(units, lease_id)
            messages.append("recover with: " + recovery)
            return ReleaseResult(True, "reset-failed",
                                 "; ".join(m for m in messages if m),
                                 units=list(units), recovery=recovery)
        return ReleaseResult(True, "released",
                             "; ".join(m for m in messages if m))

    def _heal_split_boards(self, serials: list[str], lease: dict) -> list[str]:
        """Reset whole any of these split boards whose dies are all free now.

        Two sibling dies released at the same moment each see the other still
        leased, so each resets only its own die and the board ends split with
        nobody left to heal it. The release that finishes last finds every die
        free and does one more, whole-board reset. Returns the serials healed;
        a board with any lease or open device is left as it is."""
        lid = lease.get("lease_id")
        me = os.getpid()
        borrowed = {**lease, "pid": me, "pgid": _pgid(me), "detached": False,
                    "since": utcnow(), "renewed_at": None,
                    "borrowed_for_reset": lid}
        healed = []
        for serial in serials:
            board = next((b for b in self.gk.boards if b.serial == serial), None)
            if board is None or len(board.chips) < 2:
                continue
            unit_keys = []
            for c in board.chips:
                key = self.gk.unit_key_for(c)
                if key not in unit_keys:
                    unit_keys.append(key)
            locked: list[str] = []
            try:
                with self.gk.critical_section():
                    fd_map = procfd.holders(self.gk.proc_root)
                    if any(self.gk.unit_lease(u) is not None for u in unit_keys) or \
                            any(fd_map.get(c.dev_index) for c in board.chips):
                        continue
                    for u in unit_keys:
                        if not self.gk.claim_unit(u, borrowed):
                            break
                        locked.append(u)
                    if len(locked) != len(unit_keys):
                        continue
                bdfs = [c.bdf for c in board.chips]
                try:
                    ok, _ = reset_mod.reset_chips(bdfs, runner=self.reset_runner)
                except reset_mod.ResetError:
                    ok = False
                with self.gk.critical_section():
                    if ok:
                        for u in unit_keys:
                            if (self.gk.unit_lease(u) or {}).get("lease_id") == lid:
                                self.gk.mark_clean(u)
                        self.gk.settle_split(bdfs)
                        healed.append(serial)
                history.log(self.gk.history_root, "board_heal", lease_id=lid,
                           board=serial, chips=bdfs, reset_ok=ok)
            finally:
                if locked:
                    with self.gk.critical_section():
                        for u in locked:
                            self.gk.release_unit(u, expected_lease_id=lid)
        return healed

    def _partial_boards(self, reset_bdfs) -> list[str]:
        """Boards of two or more dies of which only some are in reset_bdfs."""
        reset = set(reset_bdfs)
        return sorted(b.serial for b in self.gk.boards
                      if len(b.chips) > 1
                      and {c.bdf for c in b.chips} & reset
                      and not {c.bdf for c in b.chips} <= reset)

    def _lock_free_partners(self, units: list[str], lease: dict) -> list[str]:
        """At chip grain, borrow the free board partners of `units` so the
        whole board can be reset. Call under critical_section.

        Decided per board: a board's partner dies are borrowed only if every
        one of them has no lease (or one left by an interrupted release of
        this same lease) and no open device. A board whose partner is busy
        gets only our die reset, and settle_split marks it afterwards.

        The borrowed lock carries our lease_id (so release_unit's guard
        applies) but is owned by this process, not by the lease's original
        owner: a detached lease past its grace window, or an owner that has
        exited, would otherwise make the borrowed lock STALE the moment it
        was written, and a concurrent acquire could reap it and grant the die
        while it is being reset."""
        partners = self.gk.partner_units(units)
        if not partners:
            return []
        lid = lease.get("lease_id")
        me = os.getpid()
        borrowed = {**lease, "pid": me, "pgid": _pgid(me), "detached": False,
                    "since": utcnow(), "renewed_at": None,
                    "borrowed_for_reset": lid}
        fd_map = procfd.holders(self.gk.proc_root)
        by_board: dict[str, list[str]] = {}
        for p in partners:
            board = self.gk.board_of(p)
            if board is not None:
                by_board.setdefault(board.serial, []).append(p)
        locked: list[str] = []
        for serial, members in by_board.items():
            def ours_or_free(unit):
                cur = self.gk.unit_lease(unit)
                return cur is None or cur.get("lease_id") == lid
            if not all(ours_or_free(p)
                       and not any(fd_map.get(c.dev_index)
                                   for c in self.gk.chips_in_unit(p))
                       for p in members):
                continue
            got: list[str] = []
            for p in members:
                cur = self.gk.unit_lease(p)
                if (cur is not None and cur.get("lease_id") == lid) or \
                        self.gk.claim_unit(p, borrowed):
                    got.append(p)
                    continue
                for done in got:
                    self.gk.release_unit(done, expected_lease_id=lid)
                got = []
                break
            locked.extend(got)
        return locked

    def _missing_lease(self, lease_id: str) -> ReleaseResult:
        """"not found", plus what history says happened to the lease: reaped
        (and when and why), or already released. A lease with no record at all
        keeps the bare message."""
        events = [e for e in history.read(self.gk.history_root)
                  if e.get("lease_id") == lease_id
                  and e.get("event") in ("reaped", "released")]
        last = events[-1] if events else None
        if last is None:
            return ReleaseResult(False, "not-found", f"lease {lease_id} not found")
        if last["event"] == "reaped":
            why = last.get("why") or "unknown reason"
            return ReleaseResult(
                False, "not-found",
                f"lease {lease_id} not found: it was reaped at {last['ts']} "
                f"({why}); nothing to release",
                reaped_at=last["ts"], reaped_why=why)
        return ReleaseResult(
            False, "not-found",
            f"lease {lease_id} not found: already released at {last['ts']}")

    @staticmethod
    def _recovery_commands(units, lease_id: str) -> str:
        """The acquire-then-release pair that resets units a release left
        un-reset: a plain acquire grants them, and releasing that lease runs
        the reset. See the gatekeeper skill's recovery recipe."""
        return " ; ".join(
            f"gozer acquire --exact {u} --who recovery "
            f"--reason 'reset after failed release of {lease_id}' "
            "&& gozer release <lease printed above>"
            for u in units)

    def reset_all(self, who: str, reason: str | None = None) -> ResetAllResult:
        """A host-wide `tt-smi -r all`, under a lease on every unit.

        The lease is what makes it safe: it is only granted when every chip is
        free, so the reset cannot land on anyone's running work, and nobody can
        be granted a chip while it runs. DEGRADED units are included, since
        repairing them is the point. A chip that reads all ones is not in any
        unit, so its device nodes are checked for open fds here instead.

        It never queues. A caller that cannot get the whole box gets "busy"
        and the reason, and nothing is run.

        Afterwards the topology is read again. Units are marked clean only if
        tt-smi succeeded, and never while their board is still short a chip.
        The lease is released without a second reset.
        """
        fd_map = procfd.holders(self.gk.proc_root)
        dead_before = list(self.gk.unreadable)
        dead_open = {c.dev_index: sorted(fd_map[c.dev_index])
                     for c in dead_before if fd_map.get(c.dev_index)}
        if dead_open:
            return ResetAllResult(False, "busy", (
                "not resetting: an unreadable chip's device is open by "
                + ", ".join(f"chip {d} pid {p}" for d, p in sorted(dead_open.items()))))

        grant = self.acquire("all", who=who,
                             reason=reason or "reset-all (host-wide reset)",
                             pid=os.getpid(), no_queue=True,
                             include_degraded=True)
        if not isinstance(grant, Grant):
            blockers = [f"chip {s.chip.dev_index} {s.state}"
                        + (f" ({s.lease['who']} pid {s.lease['pid']})"
                           if s.lease else "")
                        for s in self.gk.reconcile(reap=False)
                        if s.state not in ("FREE", "DEGRADED")]
            history.log(self.gk.history_root, "refused", action="reset_all",
                       who=who, reason="box not free", blockers=blockers)
            return ResetAllResult(False, "busy", (
                "not resetting: the whole box must be free. "
                + ("; ".join(blockers) if blockers
                   else "other requests are queued ahead")))

        ok, out = reset_mod.reset_all(runner=self.reset_runner)

        # Re-read sysfs: a chip that came back changes which chips make up each
        # board, and the lease record still lists the chips from before.
        self.gk._topo = None
        try:
            dead_after = [c.bdf for c in self.gk.unreadable]
            all_dead = False
        except Exception:  # noqa: BLE001 - every chip still dead: no topology
            dead_after, all_dead = [c.bdf for c in dead_before], True

        if not all_dead:
            lease = self.gk.read_lease(grant.lease_id)
            if lease is not None:
                chips = sorted((c for u in lease["units"]
                                for c in self.gk.chips_in_unit(u)),
                               key=lambda c: c.dev_index)
                lease["chips"] = [c.bdf for c in chips]
                lease["dev_indices"] = [c.dev_index for c in chips]
                self.gk.write_lease(lease)
            if ok:
                for unit in grant.units:
                    chips = self.gk.chips_in_unit(unit)
                    if chips and not any(self.gk.short_board_of(c) for c in chips):
                        self.gk.mark_clean(unit)
                # Every die was reset together, so every on-board link is back.
                self.gk.settle_split(
                    [c.bdf for u in grant.units for c in self.gk.chips_in_unit(u)])

        released = self.release_result(grant.lease_id, no_reset=True)

        if not ok:
            status, message = "failed", f"tt-smi -r all failed: {out}"
        elif dead_after:
            status = "reset-incomplete"
            message = (f"reset ran, but {', '.join(dead_after)} still read all "
                       "ones. gozer has no further reset to try: a human must "
                       "power-cycle the box or reseat the card")
        else:
            status, message = "reset", "reset ok: every chip reads normally"
        if not released.ok:
            message += f"; release: {released.message}"
        history.log(self.gk.history_root, "reset_all", lease_id=grant.lease_id,
                   who=who, status=status, unreadable=dead_after,
                   chips=grant.bdfs)
        return ResetAllResult(status == "reset", status, message,
                              unreadable=dead_after, lease_id=grant.lease_id)

    def reset(self, lease_id: str, force: bool = False) -> ResetResult:
        """Reset a held lease's chips and keep the lease.

        Used by a supervisor that stops one model server, runs something else
        on the same board, and starts a server again, without ever letting go
        of the board. Releasing in between would let another agent take it.

        The structure follows `release`: refuse while a device is open; re-read
        the gate under the mutex before the reset so it never fires at chips
        another tenant now holds; drop the mutex for the reset itself (tt-smi
        takes tens of seconds); re-read the gate once more afterwards. It does
        not mark the unit clean, touch the queue, or open a claim window,
        because the lease continues and `release` resets again.
        """
        lease = self.gk.read_lease(lease_id)
        if lease is None:
            return ResetResult(False, "not-found", f"lease {lease_id} not found")
        units = lease.get("units", [])
        chips = lease.get("chips", [])
        if not units or not chips:
            # A record with nothing to check or nothing to reset cannot be
            # verified against the gate, so firing a reset from it would be a
            # guess. Refuse; `release` still cleans such a record up.
            history.log(self.gk.history_root, "refused", action="reset",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="lease record has no units or chips")
            return ResetResult(False, "refused", (
                f"lease {lease_id} has no units or chips recorded; "
                "nothing was reset"))

        # The gate checks below look at `units` only, but tt-smi runs on
        # `chips` and the open-device scan reads `dev_indices`. Both come from
        # the record. A record whose lists reach past its units would reset
        # another tenant's chips (or skip the scan) while every gate check
        # passed. So derive the expected chips and indices from the units and
        # refuse on any difference. This runs before the scan, so the scan only
        # ever reads verified indices, and --force does not skip it. A unit the
        # topology does not know gives an empty expected set, which is also a
        # difference.
        expected = [c for u in units for c in self.gk.chips_in_unit(u)]
        want = {"chips": {c.bdf for c in expected},
                "dev_indices": {c.dev_index for c in expected}}
        have_lists = {"chips": list(chips),
                      "dev_indices": list(lease.get("dev_indices", []))}
        have = {name: set(v) for name, v in have_lists.items()}
        # Sets hide repeats, and tt-smi would be handed "a,b,a". A repeated
        # entry is a record that disagrees with its units, so name it.
        dupes = {name: sorted({x for x in v if v.count(x) > 1}, key=str)
                 for name, v in have_lists.items()}
        bad = [name for name in want
               if want[name] != have[name] or dupes[name]]
        if bad:
            history.log(self.gk.history_root, "refused", action="reset",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="lease record disagrees with its units",
                       mismatched=bad)
            return ResetResult(False, "refused", (
                f"lease {lease_id} disagrees with its units: " +
                "; ".join(
                    f"{name} are {sorted(have[name])} but the units "
                    f"have {sorted(want[name])}"
                    + (f" (duplicate entries: {dupes[name]})" if dupes[name] else "")
                    for name in bad) +
                "; nothing was reset"))

        fd_map = procfd.holders(self.gk.proc_root)
        still_open = [d for d in lease.get("dev_indices", []) if fd_map.get(d)]
        if still_open and not force:
            history.log(self.gk.history_root, "refused", action="reset",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="device still open", dev_indices=still_open)
            return ResetResult(False, "refused", (
                f"chips {still_open} still open by "
                f"{sorted({p for d in still_open for p in fd_map[d]})} -- "
                "stop the workload first, or pass --force"))

        with self.gk.critical_section():
            foreign = self._units_held_elsewhere(units, lease_id)
            unheld = [u for u in units if self.gk.unit_lease(u) is None]
        if foreign:
            history.log(self.gk.history_root, "refused", action="reset",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="units now belong to another lease", foreign=foreign)
            return ResetResult(False, "refused", (
                "refusing to reset: " +
                ", ".join(f"{unit} now belongs to lease {other}"
                          for unit, other in sorted(foreign.items())) +
                f" -- lease {lease_id} was already reaped or released; nothing was reset"))
        if unheld:
            # Our lock is gone and nobody holds the unit. A reset now would land
            # on whoever takes these chips next, so do nothing.
            history.log(self.gk.history_root, "refused", action="reset",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="unit no longer locked by this lease", unheld=unheld)
            return ResetResult(False, "refused", (
                f"refusing to reset: {', '.join(sorted(unheld))} no longer locked "
                f"by lease {lease_id} (already reaped?); nothing was reset"))

        try:
            reset_ok, out = reset_mod.reset_chips(chips, runner=self.reset_runner)
        except reset_mod.ResetError as e:
            # A chip id that is not a PCI address can only come from topology
            # itself (a dangling sysfs device link gives the basename "device").
            # reset_command refuses before running anything, so report that as
            # a refusal. Letting the exception through would end in a traceback.
            bad_ids = [c for c in chips if not reset_mod.BDF_RE.match(c)]
            history.log(self.gk.history_root, "refused", action="reset",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="malformed chip id", chips=bad_ids)
            return ResetResult(False, "refused", (
                f"refusing to reset: chip id {bad_ids or chips} is not a PCI "
                f"address ({e}); nothing was reset"))
        message = out or ("reset ok" if reset_ok else "reset failed")

        with self.gk.critical_section():
            foreign = self._units_held_elsewhere(units, lease_id)
            unheld = [u for u in units if self.gk.unit_lease(u) is None]
            # A reset that covered a whole board brings its on-board link back;
            # one die of a shared board on its own takes it down.
            if reset_ok:
                _, marked = self.gk.settle_split(chips)
                for serial in marked:
                    message += (f"; board {serial} needs a whole-board reset "
                                "before a mesh over both dies")
        history.log(self.gk.history_root, "reset", lease_id=lease_id,
                   who=lease.get("who"), chips=chips, reset_ok=reset_ok)
        if foreign:
            return ResetResult(False, "changed-hands", "; ".join([message,
                "discovered after the reset: " +
                ", ".join(f"{unit} now belongs to lease {other}"
                          for unit, other in sorted(foreign.items())) +
                " -- leaving the new tenant's lock alone"]))
        if unheld:
            return ResetResult(False, "changed-hands", "; ".join([message,
                f"discovered after the reset: lease {lease_id} was reaped while "
                "the reset ran"]))
        return ResetResult(reset_ok, "reset" if reset_ok else "failed", message)

    # ---- env and run ------------------------------------------------------

    def env_for(self, grant_or_lease) -> dict[str, str]:
        bdfs = (grant_or_lease.bdfs if isinstance(grant_or_lease, Grant)
                else grant_or_lease["chips"])
        return {"TT_VISIBLE_DEVICES": ",".join(bdfs)}

    def run(self, argv: list[str], *, _before_unblock=None, **acquire_kwargs) -> int:
        """Acquire, run, always release -- including on signal.

        This is the form scripts and skills should prefer: the lease is bound to
        the process, so it cannot leak if an agent is interrupted mid-session.

        SIGINT/SIGTERM are *blocked* -- not just left with their default
        disposition -- from before acquire() until the forwarding handlers
        below actually exist. A signal delivered while blocked is not lost:
        the kernel holds it pending and delivers it the instant we unblock,
        by which time the handlers are installed and the try/finally that
        releases the lease is reachable. Without this, a signal arriving in
        the acquire()-to-handler-install window would hit SIGTERM/SIGINT's
        default disposition -- process death with no Python running -- and
        the finally below would never execute, leaking the lease. Blocking
        briefly defers Ctrl-C during acquire(); that's bounded, because
        critical_section() has its own timeout and cannot hang forever.

        The handlers cannot be installed any earlier than this (e.g. before
        acquire()) either: _forward raises when there is no live child to
        forward to, and installing it before Popen() has even run would mean
        a signal during acquire() raises out of an acquire() that has not
        finished building its state, rather than being held pending until
        there is a well-defined try/finally around it.

        `_before_unblock` is a private test-only seam: a zero-argument
        callable invoked right after the handlers are installed but while
        SIGINT/SIGTERM are still blocked, so a signal it sends to this
        process is held pending by the kernel rather than delivered on the
        spot. Production callers never pass this. It exists because "a
        signal is already pending at the moment we unblock, before Popen
        has run" is a sub-millisecond race that a test cannot otherwise hit
        reliably -- and that race is exactly where the unblock-placement bug
        this seam regression-tests was found twice.
        """
        # run() supervises a real, live child for the lease's whole lifetime
        # (this very process blocks in proc.wait() below), so -- unless a
        # caller already passed one explicitly (tests do, with pid=1) -- it
        # is exactly the case Keymaster.acquire's `detached` distinction
        # means for a *non*-detached lease: record this process's own pid so
        # reconcile can use ordinary process-death detection instead of the
        # detached grace window.
        acquire_kwargs.setdefault("pid", os.getpid())
        sigs = (signal.SIGINT, signal.SIGTERM)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
        try:
            grant = self.acquire(**acquire_kwargs)
            if not isinstance(grant, Grant):
                print(f"queued: ticket {grant['ticket']}", file=sys.stderr)
                return 10

            env = dict(os.environ)
            env.update(self.env_for(grant))
            proc = None

            def _forward(signum, _frame):
                # Must never silently swallow a signal. If there is no live
                # child to forward it to -- Popen has not happened yet, or
                # the child has already exited -- raise instead of quietly
                # returning, so control unwinds through the try/finally
                # below and the lease is released. Returning here would
                # leave the process looking alive with no way to stop it:
                # exactly the "gozer run is unkillable" failure mode this
                # exists to prevent.
                if proc is not None and proc.poll() is None:
                    proc.send_signal(signum)
                else:
                    raise KeyboardInterrupt(
                        f"signal {signum} received with no live child to "
                        "forward it to")

            previous_handlers = {s: signal.getsignal(s) for s in sigs}
            for s in sigs:
                signal.signal(s, _forward)
            if _before_unblock is not None:
                _before_unblock()
            try:
                # Unblock as the FIRST statement inside this try, not the
                # statement before it. If a signal was pending, unblocking
                # delivers it immediately and _forward runs right here --
                # before proc is ever assigned, so it takes the no-live
                # -child branch and raises. That raise must land inside
                # this try so its finally (handler restore + release) is
                # what catches it. Unblocking one statement earlier, outside
                # this try, was the exact bug: the very same pending-signal
                # delivery would raise from a place with no finally in
                # scope, leaking the lease and leaving _forward (closing
                # over a permanently-None proc) installed as the process's
                # SIGINT/SIGTERM handler forever after.
                signal.pthread_sigmask(signal.SIG_UNBLOCK, sigs)
                try:
                    proc = subprocess.Popen(argv, env=env)
                except OSError as e:
                    # Popen itself failed (e.g. a bad executable). The lease
                    # is still torn down by the finally below; report this
                    # as an int, not a raised exception, so run()'s -> int
                    # contract holds on this path too.
                    print(f"gozer run: failed to start {argv!r}: {e}",
                          file=sys.stderr)
                    return 127
                return self._wait_checking_fence(proc, grant)
            finally:
                for s, handler in previous_handlers.items():
                    signal.signal(s, handler)
                # release()'s return value carries *why* the reset did or
                # didn't succeed (e.g. the reset binary not being on PATH) --
                # `gozer release` surfaces this via cmd_release's _emit, but
                # run() used to discard it entirely, so a reset failure during
                # auto-cleanup was invisible anywhere except the bare
                # reset_ok=false in history.jsonl. Print it here so both paths
                # report the same thing.
                _, msg = self.release(grant.lease_id,
                                      settle=RELEASE_SETTLE_SECONDS)
                if msg:
                    print(f"gozer run: {msg}", file=sys.stderr)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


    def _wait_checking_fence(self, proc, grant: Grant) -> int:
        """proc.wait(), checking at FENCE_CHECK_SECONDS after start that the
        command opened no device outside its grant. A breach is warned about
        on stderr and logged once per device; the command is left running --
        gozer advises, it does not kill."""
        checks = list(FENCE_CHECK_SECONDS)
        start = self.monotonic()
        warned: set[int] = set()
        while True:
            timeout = None
            if checks:
                timeout = max(0.0, start + checks[0] - self.monotonic())
            try:
                return proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                checks.pop(0)
                try:
                    found = self.gk.fence_violations(lease_id=grant.lease_id)
                except Exception:  # noqa: BLE001 - a check must never kill run
                    continue
                for v in found:
                    if v["dev_index"] in warned:
                        continue
                    warned.add(v["dev_index"])
                    held = (f", which lease {v['held_by']} ({v['held_by_who']}) holds"
                            if v["held_by"] else "")
                    print(f"gozer run: WARNING: pid {v['pid']} opened device "
                          f"{v['dev_index']} ({v['bdf']}){held} -- outside this "
                          f"lease's grant {grant.bdfs}. Check the command's "
                          "device settings (does it honour TT_VISIBLE_DEVICES?)",
                          file=sys.stderr)
                    history.log(self.gk.history_root, "fence_violation",
                               lease_id=grant.lease_id, who=v["who"],
                               pid=v["pid"], dev_index=v["dev_index"],
                               bdf=v["bdf"], held_by=v["held_by"])


def _current_user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001 - identity is nice-to-have, never fatal
        return os.environ.get("USER", "unknown")


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _deadline(expect: str | None) -> str | None:
    if not expect:
        return None
    end = datetime.now(timezone.utc) + timedelta(seconds=parse_duration(expect))
    return end.strftime("%Y-%m-%dT%H:%M:%SZ")
