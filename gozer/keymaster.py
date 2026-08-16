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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from gozer import history, procfd
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


class Keymaster:
    def __init__(self, gatekeeper: Gatekeeper):
        self.gk = gatekeeper
        # Swappable so tests never shell out to a real tt-smi.
        self.reset_runner = subprocess.run

    # ---- acquire ----------------------------------------------------------

    def acquire(self, chips_spec: str, who: str, reason: str | None = None,
                exact: str | None = None, fresh: bool = False,
                expect: str | None = None, pid: int | None = None,
                ticket: str | None = None, no_queue: bool = False):
        """Return a Grant, a queue-ticket dict, or None.

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
        }

        with self.gk.critical_section():
            self.gk.prune_queue()

            # A ticket-holder outside its claim window must wait its turn, and a
            # newcomer must not jump an open window.
            if not self.gk.may_claim(ticket):
                return self._queue_or_refuse(ticket, request, no_queue)

            units = self.gk.allocate(min_chips, max_chips, exact=exact, fresh=fresh)
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
            history.log(self.gk.root, "granted", lease_id=lease_id, who=who,
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
        history.log(self.gk.root, "queued", ticket=rec["ticket"],
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
                force: bool = False) -> tuple[bool, str]:
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
        """
        lease = self.gk.read_lease(lease_id)
        if lease is None:
            return False, f"lease {lease_id} not found"
        units = lease.get("units", [])

        fd_map = procfd.holders(self.gk.proc_root)
        still_open = [d for d in lease.get("dev_indices", []) if fd_map.get(d)]
        if still_open and not force:
            history.log(self.gk.root, "refused", action="release",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="device still open", dev_indices=still_open)
            return False, (
                f"chips {still_open} still open by "
                f"{sorted({p for d in still_open for p in fd_map[d]})} -- "
                "let the work finish, or pass --force")

        # First revalidation: are these units still ours, right now, with
        # nobody able to claim them while we look?
        with self.gk.critical_section():
            foreign = self._units_held_elsewhere(units, lease_id)
            unheld = [u for u in units
                      if self.gk.unit_lease(u) is None]
        if foreign:
            history.log(self.gk.root, "refused", action="release",
                       lease_id=lease_id, who=lease.get("who"),
                       reason="units now belong to another lease",
                       foreign=foreign)
            return False, (
                "refusing to release: " +
                ", ".join(f"{unit} now belongs to lease {other}"
                          for unit, other in sorted(foreign.items())) +
                f" -- lease {lease_id} was already reaped or released; "
                "nothing was reset")

        messages = []
        reset_ran = False
        reset_ok = False
        if unheld and not no_reset:
            # Our lock is gone but nobody else has taken these units yet.
            # Resetting now would fire at chips that are free for anyone to
            # grab, so skip it and just clean up our own bookkeeping.
            messages.append(
                f"not resetting: {', '.join(sorted(unheld))} no longer locked "
                f"by lease {lease_id} (already reaped?)")
        elif not no_reset:
            reset_ran = True
            reset_ok, out = reset_mod.reset_chips(lease["chips"],
                                                  runner=self.reset_runner)
            messages.append(out or ("reset ok" if reset_ok else "reset failed"))
            if not reset_ok:
                messages.append("reset failed -- unit released but NOT marked clean")

        # Second revalidation: the reset above took real time, so re-check
        # before deleting any lock or handing the chips to the queue.
        with self.gk.critical_section():
            foreign = self._units_held_elsewhere(units, lease_id)
            if foreign:
                history.log(self.gk.root, "refused", action="release",
                           lease_id=lease_id, who=lease.get("who"),
                           reason="units now belong to another lease "
                                  "(discovered after the reset)",
                           foreign=foreign)
                return False, (
                    "; ".join(messages + [
                        "aborted before teardown: " +
                        ", ".join(f"{unit} now belongs to lease {other}"
                                  for unit, other in sorted(foreign.items())) +
                        " -- leaving the new tenant's lock alone"]))

            if reset_ok:
                for unit in units:
                    self.gk.mark_clean(unit)
            for unit in units:
                self.gk.release_unit(unit, expected_lease_id=lease_id)
            self.gk.delete_lease(lease_id)

            # Hand the freed chips to whoever is next in line. Prune first so
            # a ticket whose waiter is demonstrably gone cannot be handed a
            # 90-second exclusive window nobody will ever claim.
            self.gk.prune_queue()
            head = self.gk.queue_entries()
            if head:
                self.gk.open_claim_window(head[0]["ticket"])
                messages.append(f"claim window opened for {head[0]['who']}")

        since = lease.get("since")
        duration_s = _elapsed_seconds(since, utcnow()) if since else None
        history.log(self.gk.root, "released", lease_id=lease_id,
                   who=lease.get("who"), chips=lease.get("chips", []),
                   duration_s=duration_s, reset_ran=reset_ran,
                   reset_ok=reset_ok if reset_ran else None)
        return True, "; ".join(m for m in messages if m)

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
                return proc.wait()
            finally:
                for s, handler in previous_handlers.items():
                    signal.signal(s, handler)
                self.release(grant.lease_id)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


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
