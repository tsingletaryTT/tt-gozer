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

from gozer import procfd
from gozer import reset as reset_mod
from gozer.gatekeeper import Gatekeeper, parse_chip_request, utcnow
from gozer.topology import Chip, all_chips

DURATION_RE = re.compile(r"^(\d+)([smh]?)$")


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
                ticket: str | None = None):
        """Return a Grant, or a queue-ticket dict when nothing is available.

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

        with self.gk.critical_section():
            self.gk.prune_queue()

            # A ticket-holder outside its claim window must wait its turn, and a
            # newcomer must not jump an open window.
            if not self.gk.may_claim(ticket):
                return self._ticket_for(ticket, who, pid, min_chips, max_chips)

            units = self.gk.allocate(min_chips, max_chips, exact=exact, fresh=fresh)
            if units is None:
                return self._ticket_for(ticket, who, pid, min_chips, max_chips)

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
                    return self._ticket_for(ticket, who, pid, min_chips, max_chips)

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

            return Grant(
                lease_id=lease_id,
                units=units,
                chips=chips,
                bdfs=[c.bdf for c in chips],
                dev_indices=[c.dev_index for c in chips],
                requested=min_chips,
                expanded=len(chips) > min_chips,
                neighbours=self.gk.eth_neighbours(units),
            )

    def _ticket_for(self, ticket, who, pid, min_chips, max_chips) -> dict:
        """Reuse an existing ticket if the caller has one, else take a new one."""
        if ticket:
            for rec in self.gk.queue_entries():
                if rec.get("ticket") == ticket:
                    return rec
        return self.gk.enqueue({
            "who": who, "pid": pid,
            "min_chips": min_chips, "max_chips": max_chips,
        })

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
            return False, (
                "refusing to release: " +
                ", ".join(f"{unit} now belongs to lease {other}"
                          for unit, other in sorted(foreign.items())) +
                f" -- lease {lease_id} was already reaped or released; "
                "nothing was reset")

        messages = []
        reset_ok = False
        if unheld and not no_reset:
            # Our lock is gone but nobody else has taken these units yet.
            # Resetting now would fire at chips that are free for anyone to
            # grab, so skip it and just clean up our own bookkeeping.
            messages.append(
                f"not resetting: {', '.join(sorted(unheld))} no longer locked "
                f"by lease {lease_id} (already reaped?)")
        elif not no_reset:
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
