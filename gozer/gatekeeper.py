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


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        """
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
        try:
            yield
        finally:
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
