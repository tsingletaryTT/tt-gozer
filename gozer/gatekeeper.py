"""The gatekeeper: guards the gate.

Owns all lock state under GOZER_ROOT (default /tmp/tt-gozer):

    gate/<unit>.lock/lease.json   mkdir() is the atomic acquire primitive
    leases/<lease_id>.json        full lease record
    queue/<seq>-<ticket>.json     FIFO tickets
    mutex/.gatekeeper.lock/       short-lived global mutex

mkdir is the primitive because it is atomic on every POSIX filesystem, avoids
flock-inheritance surprises across subshells, and leaves an artifact a human can
inspect or remove by hand.

Multi-user sharing: `root`, `gate/`, `leases/` and `queue/` are all sticky
(0o1777) so any user may create entries but only the owner (or root) may
remove one — that is what stops one user from deleting another's lock. The
mutex lives in its own *non-sticky* `mutex/` (0o777) directory instead, for
reasons explained on `critical_section`.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from gozer import procfd
from gozer.topology import Board, Chip, all_chips, lease_grain, read_topology

DEFAULT_ROOT = "/tmp/tt-gozer"
MUTEX_SUBDIR = "mutex"
MUTEX_DIR = ".gatekeeper.lock"
MUTEX_STALE_SECONDS = 30
CLAIM_WINDOW_SECONDS = 90


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_chip_request(spec: str, total: int) -> tuple[int, int]:
    """Parse --chips into (minimum, maximum).

    "1" -> (1, 1)     exactly one
    "all" -> (total, total)
    "1-4" -> (1, 4)   elastic: at least 1, up to 4

    A range's upper bound is silently clamped to `total`: "1-10" against a
    4-chip machine yields (1, 4). An elastic request means "take what exists,"
    not "fail because I overestimated," so asking for more than the machine
    has is not an error.
    """
    spec = (spec or "").strip().lower()
    if spec == "all":
        return total, total
    if "-" in spec:
        lo_s, _, hi_s = spec.partition("-")
        if not lo_s.isdigit() or not hi_s.isdigit():
            raise ValueError(f"bad --chips range: {spec!r}")
        lo, hi = int(lo_s), int(hi_s)
        if lo < 1 or hi < lo:
            raise ValueError(f"bad --chips range: {spec!r}")
        return lo, min(hi, total)
    if spec.isdigit() and int(spec) >= 1:
        n = int(spec)
        return n, n
    raise ValueError(f"bad --chips value: {spec!r} (want N, all, or LO-HI)")


def _atomic_write_json(path: str, payload: dict) -> None:
    """Write via temp file + rename so a reader never sees a partial record."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@dataclass
class ChipState:
    chip: Chip
    state: str
    lease: dict | None
    pids: list[int]
    overstayed: bool = False


class Gatekeeper:
    def __init__(self, root: str | None = None, sysfs_root: str | None = None,
                 proc_root: str = "/proc"):
        self.root = root or os.environ.get("GOZER_ROOT") or DEFAULT_ROOT
        self.sysfs_root = sysfs_root
        self.proc_root = proc_root
        # Reentrancy depth for critical_section(): >0 means this process
        # already holds the mutex, so a nested call must not try to take
        # the filesystem lock again. See critical_section's docstring.
        self._critical_section_depth = 0
        self._ensure_dirs()

    # ---- layout -----------------------------------------------------------

    def _ensure_dirs(self) -> None:
        # 0o1777 (sticky) so several users can share the gate without being able
        # to remove each other's lock directories. os.makedirs' `mode` argument
        # is masked by the umask, so the sticky bit must be applied with an
        # explicit chmod after the fact - for every directory, not just the
        # root. The chmod is best-effort: a second user re-running this after
        # the first user already owns the directory cannot chmod it (they
        # don't own it), and that must not be fatal.
        first = not os.path.isdir(self.root)
        os.makedirs(self.root, exist_ok=True)
        if first:
            with contextlib.suppress(OSError):
                os.chmod(self.root, 0o1777)
        for sub in ("gate", "leases", "queue"):
            path = os.path.join(self.root, sub)
            os.makedirs(path, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o1777)
        # The mutex directory is deliberately NOT sticky - see critical_section.
        mutex_dir = os.path.join(self.root, MUTEX_SUBDIR)
        os.makedirs(mutex_dir, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(mutex_dir, 0o777)

    def _gate_dir(self, unit_key: str) -> str:
        return os.path.join(self.root, "gate", f"{unit_key}.lock")

    def _lease_path(self, lease_id: str) -> str:
        return os.path.join(self.root, "leases", f"{lease_id}.json")

    # ---- the atomic primitive ---------------------------------------------

    def claim_unit(self, unit_key: str, lease: dict) -> bool:
        """Atomically take the gate for one unit. False if someone else holds it."""
        d = self._gate_dir(unit_key)
        try:
            os.mkdir(d)
        except OSError as e:
            if e.errno == errno.EEXIST:
                return False
            raise
        # Between this mkdir succeeding and the write below landing, a
        # concurrent unit_lease()/held_units() call can observe the lock
        # directory without a lease.json inside it yet. That's expected:
        # _read_json returns None for a missing file, so callers see "not
        # populated yet" rather than crashing.
        _atomic_write_json(os.path.join(d, "lease.json"), lease)
        return True

    def release_unit(self, unit_key: str) -> None:
        d = self._gate_dir(unit_key)
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(d, "lease.json"))
        with contextlib.suppress(OSError):
            os.rmdir(d)

    def unit_lease(self, unit_key: str) -> dict | None:
        return _read_json(os.path.join(self._gate_dir(unit_key), "lease.json"))

    def update_unit_lease(self, unit_key: str, lease: dict) -> bool:
        """Rewrite a held unit's lease copy in place, without releasing it.

        Used by `renew`. Returns False if the unit is not currently held, so a
        caller can never accidentally create a lock this way. The isdir check
        and the write are not atomic together: release_unit() can race in
        between and remove the gate directory out from under us. When that
        happens the write raises ENOENT, which we translate into the same
        False the caller already expects - but only for that specific race.
        Any other I/O error (disk full, permission oddity, etc.) is a real
        failure and must still surface.
        """
        d = self._gate_dir(unit_key)
        if not os.path.isdir(d):
            return False
        try:
            _atomic_write_json(os.path.join(d, "lease.json"), lease)
        except OSError as e:
            if e.errno == errno.ENOENT and not os.path.isdir(d):
                return False
            raise
        return True

    def held_units(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        gate = os.path.join(self.root, "gate")
        for entry in sorted(os.listdir(gate)):
            if not entry.endswith(".lock"):
                continue
            lease = _read_json(os.path.join(gate, entry, "lease.json"))
            if lease is not None:
                out[entry[:-len(".lock")]] = lease
        return out

    # ---- lease records ----------------------------------------------------

    def new_lease_id(self) -> str:
        return secrets.token_hex(3)

    def write_lease(self, lease: dict) -> None:
        _atomic_write_json(self._lease_path(lease["lease_id"]), lease)

    def read_lease(self, lease_id: str) -> dict | None:
        return _read_json(self._lease_path(lease_id))

    def delete_lease(self, lease_id: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self._lease_path(lease_id))

    def all_leases(self) -> list[dict]:
        d = os.path.join(self.root, "leases")
        out = []
        for entry in sorted(os.listdir(d)):
            if entry.endswith(".json"):
                rec = _read_json(os.path.join(d, entry))
                if rec:
                    out.append(rec)
        return out

    # ---- global mutex -----------------------------------------------------

    @contextlib.contextmanager
    def critical_section(self, timeout: float = 10.0):
        """Serialise allocation. Held for milliseconds, never across I/O waits.

        This mutex is an optimization, not the correctness guarantee: the gate
        stays safe under a wrongly-cleared mutex because correctness rests on
        claim_unit's `mkdir` being atomic, plus the caller's rollback of any
        already-claimed units when a multi-unit acquire fails partway through.
        A racer that slips in after a wrongly-cleared mutex just loses a race
        the claim path already handles - it costs nothing worse than a retry.

        That's why the mutex directory lives under the non-sticky `mutex/`
        (0o777, no sticky bit) rather than directly under the sticky root:
        sticky semantics protect whichever directory *holds* an entry, so a
        non-sticky parent lets any user remove another user's crashed,
        stale `.gatekeeper.lock` and self-heal. A permanently wedged mutex -
        the alternative if we kept it sticky-protected - would instead block
        every user on the machine indefinitely. Trading "occasionally a lost
        race" for "never permanently wedged" is the right way round.

        Reentrant within a single process/instance: a nested call (e.g.
        Keymaster.acquire, Task 8, calling may_claim while already holding
        the section) increments a depth counter and returns without touching
        the filesystem lock at all. Only the outermost entry creates and
        removes the `mkdir` lock. Without this, a nested acquire would sit
        in the wait loop below until MUTEX_STALE_SECONDS/the timeout elapsed
        and then either self-heal by deleting its own live lock (corrupting
        the invariant that a held lock means "in use") or raise TimeoutError
        - both far worse than the bug reentrancy fixes. Cross-process
        exclusion is unaffected: two different processes still serialise on
        the same `mkdir` exactly as before; only same-instance, same-process
        nesting is now safe.
        """
        if self._critical_section_depth > 0:
            self._critical_section_depth += 1
            try:
                yield
            finally:
                # Restored even if the nested block raises, so an exception
                # can never leave the counter stuck above zero and silently
                # disable real locking for the rest of the process.
                self._critical_section_depth -= 1
            return

        path = os.path.join(self.root, MUTEX_SUBDIR, MUTEX_DIR)
        deadline = time.monotonic() + timeout
        # Set when a stale mutex is found but we lack permission to remove it
        # (cross-user case), so the eventual TimeoutError can say why, rather
        # than reporting a plain timeout indistinguishable from contention.
        undeletable: tuple[str, int] | None = None
        while True:
            try:
                os.mkdir(path)
                break
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                # A crashed holder must not wedge the gate forever.
                try:
                    st = os.stat(path)
                except OSError:
                    # Vanished between our mkdir and stat - another racer
                    # already cleaned it up or released normally. Retry now.
                    continue
                age = time.time() - st.st_mtime
                if age > MUTEX_STALE_SECONDS:
                    try:
                        os.rmdir(path)
                        undeletable = None
                        continue
                    except OSError:
                        # Do not swallow this: a same-user removal failure
                        # would be a bug, and a cross-user EPERM is exactly
                        # the case the TimeoutError below needs to explain.
                        undeletable = (path, st.st_uid)
                if time.monotonic() > deadline:
                    if undeletable is not None:
                        stale_path, owner_uid = undeletable
                        raise TimeoutError(
                            f"gatekeeper mutex at {stale_path} is stale "
                            f"(older than {MUTEX_STALE_SECONDS}s, owned by "
                            f"uid {owner_uid}) but this user does not have "
                            "permission to remove it; use sudo or the owning "
                            "account, or remove it by hand if no gozer is "
                            "running")
                    raise TimeoutError("gatekeeper mutex is stuck; "
                                       f"remove {path} if no gozer is running")
                time.sleep(0.02)
        self._critical_section_depth = 1
        try:
            yield
        finally:
            self._critical_section_depth = 0
            with contextlib.suppress(OSError):
                os.rmdir(path)

    # ---- topology (cached; sysfs is cheap but we read it once per run) -----

    @property
    def boards(self) -> list[Board]:
        if getattr(self, "_boards", None) is None:
            self._boards = read_topology(self.sysfs_root)
        return self._boards

    @property
    def grain(self) -> str:
        return lease_grain(self.boards)

    def unit_key_for(self, chip: Chip, grain: str | None = None) -> str:
        """Board serial at board grain, BDF at chip grain."""
        return chip.serial if (grain or self.grain) == "board" else chip.bdf

    def chips_in_unit(self, unit_key: str) -> list[Chip]:
        if self.grain == "board":
            for b in self.boards:
                if b.serial == unit_key:
                    return list(b.chips)
            return []
        return [c for c in all_chips(self.boards) if c.bdf == unit_key]

    # ---- reconciliation ---------------------------------------------------

    def reconcile(self, reap: bool = True) -> list[ChipState]:
        """Compare lease bookkeeping against kernel truth.

        The lease file says who and why; only an open fd proves a chip is in use.
        Reaping is deliberately conservative: a lease is only removed when its
        process is *gone* and no fd remains. A live process that has run past its
        advisory expect_done is reported OVERSTAYED and left strictly alone --
        never be greedy, make sure a process is completely done.
        """
        fd_map = procfd.holders(self.proc_root)
        held = self.held_units()
        now = utcnow()

        results: list[ChipState] = []
        reapable: list[tuple[str, dict]] = []

        for chip in all_chips(self.boards):
            unit = self.unit_key_for(chip)
            lease = held.get(unit)
            pids = fd_map.get(chip.dev_index, [])

            if lease is None:
                results.append(ChipState(chip, "BUSY-UNTRACKED" if pids else "FREE",
                                         None, pids))
                continue

            owner = lease.get("pid")
            owner_pgid = lease.get("pgid")
            overstayed = bool(lease.get("expect_done")) and lease["expect_done"] < now

            if pids:
                owned = any(p == owner or p == owner_pgid for p in pids)
                state = "HELD" if owned else "HELD-FOREIGN"
            elif owner is not None and procfd.pid_alive(owner, self.proc_root):
                state = "CLAIMED"
            else:
                state = "STALE"
                if (unit, lease) not in reapable:
                    reapable.append((unit, lease))

            results.append(ChipState(chip, state, lease, pids, overstayed))

        if reap and reapable:
            for unit, lease in reapable:
                self.release_unit(unit)
                if lease.get("lease_id"):
                    self.delete_lease(lease["lease_id"])
            # Re-derive so callers see FREE rather than the pre-reap STALE.
            return self.reconcile(reap=False)

        return results

    # ---- cleanliness bookkeeping ------------------------------------------

    def _clean_marker(self, unit_key: str) -> str:
        return os.path.join(self.root, "gate", f"{unit_key}.clean")

    def mark_clean(self, unit_key: str) -> None:
        """Record that this unit has been reset since its last release."""
        with open(self._clean_marker(unit_key), "w") as f:
            f.write(utcnow() + "\n")

    def clear_clean(self, unit_key: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self._clean_marker(unit_key))

    def is_clean(self, unit_key: str) -> bool:
        return os.path.exists(self._clean_marker(unit_key))

    # ---- selection --------------------------------------------------------

    def all_units(self) -> list[str]:
        if self.grain == "board":
            return [b.serial for b in self.boards]
        return [c.bdf for c in all_chips(self.boards)]

    def free_units(self) -> list[str]:
        """Units with no lease and no process holding any of their chips."""
        # reconcile() defaults to reap=True, and that default must stay. Do
        # not "simplify" this to reconcile(reap=False): a STALE lease (owning
        # pid dead, no fd open, chips genuinely idle) would then keep its
        # unit permanently out of the free set, because nothing else ever
        # clears a stale lease.json off disk. One crashed agent would then
        # wedge the whole box until a human ran `gozer reconcile` by hand --
        # exactly the failure mode this tool exists to avoid. Reaping a
        # lease whose owning process is gone is garbage collection, not
        # claiming; it is what lets allocation self-heal after a crash.
        by_chip = {s.chip.bdf: s for s in self.reconcile()}
        free = []
        for unit in self.all_units():
            chips = self.chips_in_unit(unit)
            if chips and all(by_chip[c.bdf].state == "FREE" for c in chips):
                free.append(unit)
        return free

    def _unit_sort_key(self, unit: str) -> tuple:
        """Prefer clean units, then the lowest device index, for determinism."""
        chips = self.chips_in_unit(unit)
        lowest = min((c.dev_index for c in chips), default=10**6)
        return (0 if self.is_clean(unit) else 1, lowest)

    def _resolve_exact(self, exact: str) -> str | None:
        """Accept a BDF, a device index, or a unit key; return the unit key."""
        for chip in all_chips(self.boards):
            if exact in (chip.bdf, str(chip.dev_index)):
                return self.unit_key_for(chip)
        return exact if exact in self.all_units() else None

    def allocate(self, min_chips: int, max_chips: int, exact: str | None = None,
                 fresh: bool = False) -> list[str] | None:
        """Choose unit keys satisfying the request, or None.

        Does not claim: it never calls claim_unit, so the returned keys are
        not locked and can race against another allocate() call. Claiming is
        the caller's job (Keymaster.acquire, Task 8), which is expected to
        run this under critical_section so selection and claiming happen as
        one atomic step from the outside.

        It does, however, mutate disk indirectly: free_units() calls
        reconcile() with its default reap=True, so any lease whose owning
        process is gone and which holds no open fd is garbage-collected
        (release_unit + delete_lease) as a side effect of asking "what's
        free?". That's deliberate housekeeping, not selection -- see the
        comment on free_units() -- but it does mean allocate() is not a pure
        read: it can delete another tenant's dead lease file.
        """
        free = self.free_units()
        if fresh:
            free = [u for u in free if self.is_clean(u)]

        if exact is not None:
            unit = self._resolve_exact(exact)
            if unit is None or unit not in free:
                return None
            return [unit]

        candidates = sorted(free, key=self._unit_sort_key)
        chosen: list[str] = []
        count = 0
        for unit in candidates:
            if count >= max_chips:
                break
            size = len(self.chips_in_unit(unit))
            # Never overshoot the maximum, unless the very first unit already
            # exceeds it -- that is the UMD board-expansion case, where the
            # smallest grantable thing is a whole board and we say so upstream.
            if count + size > max_chips and count > 0:
                continue
            chosen.append(unit)
            count += size

        return chosen if count >= min_chips else None

    def eth_neighbours(self, units: list[str]) -> dict[str, str]:
        """Leased chips sharing a board with the selection but not part of it.

        The p300c mesh is hardwired, so a reset on release may perturb these.
        Reported as a warning, never a block -- see the design spec.
        """
        selected = {c.bdf for u in units for c in self.chips_in_unit(u)}
        held = self.held_units()
        out: dict[str, str] = {}
        for board in self.boards:
            board_bdfs = {c.bdf for c in board.chips}
            if not (board_bdfs & selected):
                continue
            for chip in board.chips:
                if chip.bdf in selected:
                    continue
                lease = held.get(self.unit_key_for(chip))
                if lease:
                    out[chip.bdf] = lease.get("who", "unknown")
        return out

    # ---- queue ------------------------------------------------------------

    def _queue_dir(self) -> str:
        return os.path.join(self.root, "queue")

    def _next_seq(self) -> int:
        entries = [e for e in os.listdir(self._queue_dir()) if e.endswith(".json")]
        seqs = []
        for e in entries:
            head = e.split("-", 1)[0]
            if head.isdigit():
                seqs.append(int(head))
        return (max(seqs) + 1) if seqs else 1

    # Bound on retries when a freshly generated ticket collides with one
    # already in the queue. secrets.token_hex(2) draws from only 65,536
    # values, so a collision is unlikely but not negligible over a machine's
    # lifetime; _ticket_path matches on the first filename it finds, so an
    # undetected collision would silently operate on the wrong ticket.
    _TICKET_COLLISION_RETRIES = 20

    def enqueue(self, request: dict) -> dict:
        """Append a ticket. Sequence numbers are zero-padded so 10 sorts after 9."""
        with self.critical_section():
            seq = self._next_seq()
            existing = {r.get("ticket") for r in self.queue_entries()}
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
            path = os.path.join(self._queue_dir(), f"{seq:06d}-{ticket}.json")
            _atomic_write_json(path, record)
            return record

    def queue_entries(self) -> list[dict]:
        """FIFO order.

        A ticket can transiently have two files on disk (see _send_to_back,
        which writes the new record before removing the old one so a live
        ticket is never briefly absent). Dedupe by ticket, keeping the entry
        with the highest `seq` -- that is always the current, correct
        position; a lower-`seq` duplicate for the same ticket is leftover
        litter from an interrupted move, never the "true" one.
        """
        out = []
        for entry in sorted(os.listdir(self._queue_dir())):
            if not entry.endswith(".json"):
                continue
            rec = _read_json(os.path.join(self._queue_dir(), entry))
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

    def _ticket_path(self, ticket: str) -> str | None:
        for entry in os.listdir(self._queue_dir()):
            if entry.endswith(f"-{ticket}.json"):
                return os.path.join(self._queue_dir(), entry)
        return None

    def queue_position(self, ticket: str) -> int | None:
        for i, rec in enumerate(self.queue_entries(), start=1):
            if rec.get("ticket") == ticket:
                return i
        return None

    def dequeue(self, ticket: str) -> None:
        path = self._ticket_path(ticket)
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)
        if self._claim_window_ticket() == ticket:
            self._close_claim_window()

    def prune_queue(self) -> list[str]:
        """Drop tickets whose creating process has exited.

        Also sweeps up litter: _send_to_back writes a ticket's new record
        before removing its old one, so a crash mid-move can transiently
        leave two files for one ticket. queue_entries() already tolerates
        that (it dedupes, keeping the higher `seq`), but nothing else ever
        removes the stale lower-`seq` file from disk -- so do that here,
        rather than leaving it to accumulate forever.
        """
        with self.critical_section():
            by_ticket: dict[str, list[tuple[str, dict]]] = {}
            for entry in sorted(os.listdir(self._queue_dir())):
                if not entry.endswith(".json"):
                    continue
                path = os.path.join(self._queue_dir(), entry)
                rec = _read_json(path)
                if rec and rec.get("ticket"):
                    by_ticket.setdefault(rec["ticket"], []).append((path, rec))
            for entries in by_ticket.values():
                if len(entries) <= 1:
                    continue
                entries.sort(key=lambda pe: pe[1].get("seq", 0))
                for stale_path, _ in entries[:-1]:
                    with contextlib.suppress(OSError):
                        os.unlink(stale_path)

            dropped = []
            for rec in self.queue_entries():
                pid = rec.get("pid")
                if pid is not None and not procfd.pid_alive(pid, self.proc_root):
                    dropped.append(rec["ticket"])
                    self.dequeue(rec["ticket"])
            return dropped

    def _send_to_back(self, ticket: str) -> None:
        """Move a ticket to the back of the queue with a fresh sequence number.

        Writes the new record *before* removing the old one, and does both
        under critical_section. Writing first means the ticket can briefly
        exist twice on disk, but never zero times -- a crash or failed write
        between the two steps leaves a recoverable duplicate (queue_entries
        and prune_queue both know how to resolve it) rather than silently
        losing a live client's place in line, which is the one failure this
        whole mechanism exists to prevent.
        """
        with self.critical_section():
            path = self._ticket_path(ticket)
            rec = _read_json(path) if path else None
            if not rec:
                return
            seq = self._next_seq()
            rec = dict(rec)
            rec["seq"] = seq
            rec["requeued_at"] = utcnow()
            _atomic_write_json(
                os.path.join(self._queue_dir(), f"{seq:06d}-{ticket}.json"), rec)
            with contextlib.suppress(OSError):
                os.unlink(path)

    # ---- claim window -----------------------------------------------------

    def _window_path(self) -> str:
        return os.path.join(self._queue_dir(), ".claim-window")

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
        with self.critical_section():
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
