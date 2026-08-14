"""The gatekeeper: guards the gate.

Owns all lock state under GOZER_ROOT (default /tmp/tt-gozer):

    gate/<unit>.lock/lease.json   mkdir() is the atomic acquire primitive
    leases/<lease_id>.json        full lease record
    queue/<seq>-<ticket>.json     FIFO tickets
    .gatekeeper.lock/             short-lived global mutex

mkdir is the primitive because it is atomic on every POSIX filesystem, avoids
flock-inheritance surprises across subshells, and leaves an artifact a human can
inspect or remove by hand.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import time
from datetime import datetime, timezone

DEFAULT_ROOT = "/tmp/tt-gozer"
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
        # to remove each other's lock directories.
        first = not os.path.isdir(self.root)
        os.makedirs(self.root, exist_ok=True)
        if first:
            with contextlib.suppress(OSError):
                os.chmod(self.root, 0o1777)
        for sub in ("gate", "leases", "queue"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)

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
        caller can never accidentally create a lock this way.
        """
        d = self._gate_dir(unit_key)
        if not os.path.isdir(d):
            return False
        _atomic_write_json(os.path.join(d, "lease.json"), lease)
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
        """Serialise allocation. Held for milliseconds, never across I/O waits."""
        path = os.path.join(self.root, MUTEX_DIR)
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.mkdir(path)
                break
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                # A crashed holder must not wedge the gate forever.
                try:
                    age = time.time() - os.stat(path).st_mtime
                    if age > MUTEX_STALE_SECONDS:
                        os.rmdir(path)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("gatekeeper mutex is stuck; "
                                       f"remove {path} if no gozer is running")
                time.sleep(0.02)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(path)
