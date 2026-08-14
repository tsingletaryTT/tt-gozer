# tt-gozer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `gozer`, a cooperative chip-leasing CLI so multiple agents can share one Tenstorrent box without colliding — the gatekeeper guards the gate, the keymaster carries the key, and Gozer is your workload arriving on the chips.

**Architecture:** A stdlib-only Python 3 package with five focused modules — `topology` (read sysfs), `gatekeeper` (state, allocation, reconciliation, queue), `keymaster` (lease lifecycle, env, process supervision), `reset` (`tt-smi -r`), `cli` (parsing and rendering). Lock state lives in `/tmp/tt-gozer/` using `mkdir` as the atomic primitive. Kernel truth (`/proc/*/fd`) decides free-vs-busy; lease files only record who and why.

**Tech Stack:** Python 3.12 (stdlib only — no third-party imports in `gozer/`), pytest 8.4.2 for tests, `tt-smi` shelled out for reset only.

**Spec:** `docs/superpowers/specs/2026-08-14-tt-gozer-design.md` — read it before starting. Every task below argues from it.

## Global Constraints

These apply to **every** task. Violating any is a task failure.

- **`gozer/` imports stdlib only.** No pytest, no requests, no third-party anything. The CLI must run under `/usr/bin/python3` (3.12.3) with no venv. Tests may use pytest.
- **Never open `/dev/tenstorrent/*`.** Not for status, not for topology, not for reconciliation. Reading a device node can disturb a running workload. Topology comes from `/sys/class/tenstorrent/`, busy-state from `/proc/*/fd` symlink targets (`os.readlink`, never `open()`).
- **Never pass `reset_m3` / `--m3` to tt-smi.** That is the one genuinely board-level reset path. Only plain `tt-smi -r <bdf>` is permitted.
- **Leases key on PCI BDF strings** (e.g. `0000:03:00.0`), never on `/dev/tenstorrent/N` indices. Indices are display-only; the vendor re-resolves them from BDF after post-reset hotplug.
- **`TT_VISIBLE_DEVICES` is emitted as comma-separated BDFs**, never integers.
- **Three env overrides must be honoured everywhere** so tests need no hardware:
  - `GOZER_ROOT` — state directory (default `/tmp/tt-gozer`)
  - `GOZER_SYSFS_ROOT` — sysfs class tree (default `/sys/class/tenstorrent`)
  - `GOZER_RESET_CMD` — reset executable (default `tt-smi`)
- **Exit codes are fixed:** `0` success · `10` queued · `11` wait timed out, still queued · `12` unavailable, queueing disabled · `13` lease not found or not owned · `14` topology unreadable.
- **Timestamps are UTC ISO-8601 with `Z`** (`datetime.now(timezone.utc)`), never local time.
- **Commit after every task.** Conventional-commit prefixes (`feat:`, `test:`, `docs:`, `chore:`).

## File Structure

| File | Responsibility |
|------|----------------|
| `bin/gozer` | Executable shim: put repo root on `sys.path`, call `gozer.cli.main()`. |
| `gozer/__init__.py` | Version constant only. |
| `gozer/topology.py` | Read sysfs → `Chip`/`Board` objects, derive lease grain. No state, no writes. |
| `gozer/procfd.py` | Scan `/proc/*/fd` → which pids hold which device index. Pure read. |
| `gozer/gatekeeper.py` | State dir, atomic lock/unlock, lease records, reconciliation, allocation, queue. |
| `gozer/reset.py` | Build and run the `tt-smi -r` command for exact BDFs. |
| `gozer/keymaster.py` | Acquire/release orchestration, env emission, `run` supervision. |
| `gozer/cli.py` | argparse surface, human + `--json` rendering, exit codes. |
| `tests/conftest.py` | Fixture sysfs tree, temp `GOZER_ROOT`, fake-proc helpers. |
| `tests/test_*.py` | One test module per source module. |
| `install.sh` | Symlink CLI into `~/.local/bin`, skills into `~/.claude/skills`. |
| `skills/gozer-keymaster/SKILL.md` | Lifecycle skill. |
| `skills/gozer-gatekeeper/SKILL.md` | Triage skill. |
| `README.md` | Protocol + state format for non-Claude consumers. |
| `CLAUDE.md` | Project log per the global convention. |

`procfd.py` is split out from `gatekeeper.py` deliberately: it is the one piece whose
behaviour differs by privilege level (same-user vs `--sudo`), and isolating it keeps that
caveat testable and in one place.

---

### Task 1: Topology from sysfs

**Files:**
- Create: `gozer/__init__.py`, `gozer/topology.py`, `bin/gozer`
- Test: `tests/conftest.py`, `tests/test_topology.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Chip` dataclass: `bdf: str`, `dev_index: int`, `serial: str`, `asic_id: str`, `card: str`
  - `Board` dataclass: `serial: str`, `chips: list[Chip]`, `card: str`
  - `read_topology(sysfs_root: str | None = None) -> list[Board]` — sorted by serial; chips within a board sorted by `dev_index`. Raises `TopologyError` if the tree is missing or unreadable.
  - `lease_grain(boards: list[Board]) -> str` — returns `"board"` or `"chip"`.
  - `TopologyError(Exception)`
  - `all_chips(boards) -> list[Chip]` — flat, sorted by `dev_index`.

- [ ] **Step 1: Write the fixture builder and failing tests**

Create `tests/conftest.py`:

```python
"""Shared fixtures. Everything here fakes hardware so tests need no real box."""
import os
import pytest


def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(value + "\n")


def build_sysfs(root, chips):
    """Build a fake /sys/class/tenstorrent tree.

    chips: list of dicts with keys dev_index, bdf, serial, asic_id, card.
    Mirrors the real layout: /sys/class/tenstorrent/tenstorrent!<N>/tt_*
    plus the PCI backlink used to map BDF -> device index.
    """
    for c in chips:
        base = os.path.join(root, "class", "tenstorrent", f"tenstorrent!{c['dev_index']}")
        _write(os.path.join(base, "tt_serial"), c["serial"])
        _write(os.path.join(base, "tt_asic_id"), c["asic_id"])
        _write(os.path.join(base, "tt_card_type"), c["card"])
        _write(os.path.join(base, "tt_heartbeat"), "12345")
        # The device symlink target encodes the BDF, exactly as the kernel does.
        pci = os.path.join(root, "bus", "pci", "devices", c["bdf"])
        os.makedirs(pci, exist_ok=True)
        link = os.path.join(base, "device")
        if not os.path.lexists(link):
            os.symlink(pci, link)
    return os.path.join(root, "class", "tenstorrent")


# NOTE: the two board serials are deliberately ordered *opposite* to the device
# indices they hold, so that order-dependent tests can distinguish "sorted by
# device index" from "sorted by serial." Renumbering them into agreement would
# make several tests pass vacuously. See tests/conftest.py for the full note.
QUIETBOX = [
    {"dev_index": 0, "bdf": "0000:01:00.0", "serial": "0000000000000002",
     "asic_id": "1111111111111111", "card": "p300c"},
    {"dev_index": 1, "bdf": "0000:02:00.0", "serial": "0000000000000002",
     "asic_id": "2222222222222222", "card": "p300c"},
    {"dev_index": 2, "bdf": "0000:03:00.0", "serial": "0000000000000001",
     "asic_id": "3333333333333333", "card": "p300c"},
    {"dev_index": 3, "bdf": "0000:04:00.0", "serial": "0000000000000001",
     "asic_id": "4444444444444444", "card": "p300c"},
]

SINGLE_CHIP_BOARDS = [
    {"dev_index": 0, "bdf": "0000:01:00.0", "serial": "AAA", "asic_id": "A1", "card": "p150a"},
    {"dev_index": 1, "bdf": "0000:02:00.0", "serial": "BBB", "asic_id": "B1", "card": "p150a"},
]

GALAXY_LIKE = [
    {"dev_index": i, "bdf": f"0000:0{i+1}:00.0", "serial": "GGG",
     "asic_id": f"G{i}", "card": "galaxy"}
    for i in range(4)
]


@pytest.fixture
def sysfs(tmp_path):
    """Returns a builder bound to a temp dir; call with a chip list."""
    def _build(chips):
        return build_sysfs(str(tmp_path / "sys"), chips)
    return _build
```

Create `tests/test_topology.py`:

```python
import pytest
from gozer.topology import read_topology, lease_grain, all_chips, TopologyError
from conftest import QUIETBOX, SINGLE_CHIP_BOARDS, GALAXY_LIKE


def test_reads_quietbox_as_two_boards_of_two(sysfs):
    boards = read_topology(sysfs(QUIETBOX))
    assert [b.serial for b in boards] == ["0000000000000001", "0000000000000002"]
    assert all(len(b.chips) == 2 for b in boards)


def test_maps_bdf_and_asic_id(sysfs):
    chips = all_chips(read_topology(sysfs(QUIETBOX)))
    assert [c.bdf for c in chips] == [
        "0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "0000:04:00.0"]
    assert chips[0].asic_id == "1111111111111111"
    assert chips[0].card == "p300c"


def test_grain_is_board_for_two_chip_boards(sysfs):
    # A p300c holds 2 chips; UMD expands TT_VISIBLE_DEVICES to the whole board.
    assert lease_grain(read_topology(sysfs(QUIETBOX))) == "board"


def test_grain_is_chip_for_single_chip_boards(sysfs):
    assert lease_grain(read_topology(sysfs(SINGLE_CHIP_BOARDS))) == "chip"


def test_grain_is_chip_when_a_board_holds_more_than_two(sysfs):
    # UMD skips board expansion for >2 chips per board, so we can lease per chip.
    assert lease_grain(read_topology(sysfs(GALAXY_LIKE))) == "chip"


def test_missing_tree_raises(tmp_path):
    with pytest.raises(TopologyError):
        read_topology(str(tmp_path / "nope"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_topology.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gozer.topology'`

- [ ] **Step 3: Write the implementation**

Create `gozer/__init__.py`:

```python
"""tt-gozer — cooperative Tenstorrent chip leasing.

The keymaster must meet the gatekeeper for the coming of Gozer.
"""

__version__ = "0.1.0"
```

Create `gozer/topology.py`:

```python
"""Read Tenstorrent chip topology from sysfs.

Deliberately never opens /dev/tenstorrent/* — reading a device node can disturb
a running workload, and everything we need is exposed as sysfs attributes:

    /sys/class/tenstorrent/tenstorrent!<N>/tt_serial     board serial (groups chips)
                                          /tt_asic_id    durable per-ASIC identity
                                          /tt_card_type  e.g. "p300c"
                                          /tt_heartbeat  changing => firmware alive
                                          /device        symlink to the PCI device (BDF)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_SYSFS_ROOT = "/sys/class/tenstorrent"


class TopologyError(Exception):
    """Raised when the sysfs tree is absent or unreadable."""


@dataclass(frozen=True)
class Chip:
    bdf: str
    dev_index: int
    serial: str
    asic_id: str
    card: str


@dataclass(frozen=True)
class Board:
    serial: str
    card: str
    chips: list[Chip] = field(default_factory=list)


def _read_attr(base: str, name: str) -> str:
    try:
        with open(os.path.join(base, name)) as f:
            return f.read().strip()
    except OSError:
        return ""


def _bdf_from_device_link(base: str) -> str:
    """The `device` symlink points at the PCI device dir; its basename is the BDF."""
    try:
        return os.path.basename(os.path.realpath(os.path.join(base, "device")))
    except OSError:
        return ""


def read_topology(sysfs_root: str | None = None) -> list[Board]:
    """Enumerate chips and group them into boards by tt_serial.

    Boards are sorted by serial, chips within a board by device index, so output
    is deterministic and tests can assert on order.
    """
    root = sysfs_root or os.environ.get("GOZER_SYSFS_ROOT") or DEFAULT_SYSFS_ROOT
    if not os.path.isdir(root):
        raise TopologyError(f"no Tenstorrent sysfs tree at {root}")

    chips: list[Chip] = []
    for entry in sorted(os.listdir(root)):
        if not entry.startswith("tenstorrent!"):
            continue
        try:
            dev_index = int(entry.split("!", 1)[1])
        except (IndexError, ValueError):
            continue
        base = os.path.join(root, entry)
        bdf = _bdf_from_device_link(base)
        if not bdf:
            continue
        chips.append(Chip(
            bdf=bdf,
            dev_index=dev_index,
            serial=_read_attr(base, "tt_serial"),
            asic_id=_read_attr(base, "tt_asic_id"),
            card=_read_attr(base, "tt_card_type"),
        ))

    if not chips:
        raise TopologyError(f"no Tenstorrent devices found under {root}")

    by_serial: dict[str, list[Chip]] = {}
    for c in chips:
        by_serial.setdefault(c.serial, []).append(c)

    return [
        Board(serial=serial,
              card=members[0].card,
              chips=sorted(members, key=lambda c: c.dev_index))
        for serial, members in sorted(by_serial.items())
    ]


def all_chips(boards: list[Board]) -> list[Chip]:
    return sorted((c for b in boards for c in b.chips), key=lambda c: c.dev_index)


def lease_grain(boards: list[Board]) -> str:
    """Return "board" or "chip", mirroring UMD's own expansion predicate.

    umd/device/cluster_descriptor.cpp: create_constrained_cluster_descriptor
    expands TT_VISIBLE_DEVICES to every chip on the same board *unless* some
    board holds more than 2 chips. So whenever expansion is active we cannot
    hand out a partial board, and the board is our lease unit.
    """
    if any(len(b.chips) > 2 for b in boards):
        return "chip"
    if all(len(b.chips) == 1 for b in boards):
        return "chip"
    return "board"
```

Create `bin/gozer`:

```python
#!/usr/bin/env python3
"""Entry point shim: run gozer straight from a checkout, no install needed."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from gozer.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Add pytest config so `conftest` imports resolve**

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
pythonpath = . tests
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_topology.py -v`
Expected: PASS, 6 passed

- [ ] **Step 6: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/__init__.py gozer/topology.py bin/gozer pytest.ini tests/
git commit -m "feat: read chip topology from sysfs without touching device nodes

Groups chips into boards by tt_serial and derives the lease grain from UMD's
own expansion predicate: board grain when boards hold exactly 2 chips (a p300c,
where TT_VISIBLE_DEVICES widens to the whole board), chip grain otherwise."
```

---

### Task 2: fd truth from /proc

**Files:**
- Create: `gozer/procfd.py`
- Test: `tests/test_procfd.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `holders(proc_root: str = "/proc") -> dict[int, list[int]]` — maps device index → sorted list of pids holding it open.
  - `pid_alive(pid: int, proc_root: str = "/proc") -> bool`
  - `process_name(pid: int, proc_root: str = "/proc") -> str` — `comm`, or `""` if gone.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_procfd.py`:

```python
import os
from gozer.procfd import holders, pid_alive, process_name


def fake_proc(tmp_path, pids):
    """Build a fake /proc. pids: {pid: (comm, [device_index, ...])}."""
    root = tmp_path / "proc"
    for pid, (comm, devs) in pids.items():
        d = root / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        for i, dev in enumerate(devs):
            os.symlink(f"/dev/tenstorrent/{dev}", d / "fd" / str(i + 3))
    return str(root)


def test_finds_pids_holding_devices(tmp_path):
    p = fake_proc(tmp_path, {861926: ("python", [0, 1, 2, 3])})
    assert holders(p) == {0: [861926], 1: [861926], 2: [861926], 3: [861926]}


def test_multiple_holders_of_one_device_are_all_listed(tmp_path):
    p = fake_proc(tmp_path, {100: ("a", [2]), 200: ("b", [2])})
    assert holders(p) == {2: [100, 200]}


def test_ignores_non_tenstorrent_fds_and_dead_links(tmp_path):
    root = tmp_path / "proc" / "500" / "fd"
    root.mkdir(parents=True)
    (tmp_path / "proc" / "500" / "comm").write_text("sh\n")
    os.symlink("/dev/null", root / "3")
    os.symlink("/nonexistent/thing", root / "4")
    assert holders(str(tmp_path / "proc")) == {}


def test_ignores_unreadable_pid_dirs(tmp_path):
    # Another user's process: /proc/<pid>/fd exists but is not listable.
    # We must skip it, not crash. This is the documented same-user limitation.
    p = fake_proc(tmp_path, {100: ("a", [1])})
    os.chmod(os.path.join(p, "100", "fd"), 0o000)
    try:
        assert holders(p) == {}
    finally:
        os.chmod(os.path.join(p, "100", "fd"), 0o755)


def test_pid_alive_and_name(tmp_path):
    p = fake_proc(tmp_path, {777: ("vllm", [0])})
    assert pid_alive(777, p) is True
    assert pid_alive(888, p) is False
    assert process_name(777, p) == "vllm"
    assert process_name(888, p) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_procfd.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gozer.procfd'`

- [ ] **Step 3: Write the implementation**

Create `gozer/procfd.py`:

```python
"""Kernel ground truth: which processes actually hold a Tenstorrent device open.

This is the authority for free-vs-busy. A lease file records *who and why*; only
an open file descriptor proves a chip is *in use*.

Limitation, by design and documented in the README: /proc/<pid>/fd is readable
only by the owning user, so this sees your own processes. On this box every
agent runs as the same user, which is the common case. Cross-user visibility
degrades to the world-readable lease files, or `--sudo` for full truth.

We use os.readlink on the fd symlinks and never open() them, so nothing here can
disturb a running workload.
"""

from __future__ import annotations

import os

DEV_PREFIX = "/dev/tenstorrent/"


def holders(proc_root: str = "/proc") -> dict[int, list[int]]:
    """Map device index -> sorted pids holding it open."""
    found: dict[int, set[int]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return {}

    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = os.path.join(proc_root, entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            # Not ours, or the process exited mid-scan. Skip, never crash.
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if not target.startswith(DEV_PREFIX):
                continue
            suffix = target[len(DEV_PREFIX):]
            if suffix.isdigit():
                found.setdefault(int(suffix), set()).add(pid)

    return {dev: sorted(pids) for dev, pids in sorted(found.items())}


def pid_alive(pid: int, proc_root: str = "/proc") -> bool:
    return os.path.isdir(os.path.join(proc_root, str(pid)))


def process_name(pid: int, proc_root: str = "/proc") -> str:
    try:
        with open(os.path.join(proc_root, str(pid), "comm")) as f:
            return f.read().strip()
    except OSError:
        return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_procfd.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/procfd.py tests/test_procfd.py
git commit -m "feat: read fd ground truth from /proc without opening device nodes

Uses os.readlink on /proc/*/fd symlinks so scanning can never disturb a live
workload. Unreadable pid dirs are skipped rather than fatal: that is the
same-user limitation, documented rather than papered over."
```

---

### Task 3: Gatekeeper state — atomic lock and lease records

**Files:**
- Create: `gozer/gatekeeper.py`
- Test: `tests/test_gatekeeper_state.py`

**Interfaces:**
- Consumes: `gozer.topology` (`Board`, `Chip`).
- Produces:
  - `Gatekeeper(root: str | None = None, sysfs_root: str | None = None, proc_root: str = "/proc")`
  - `Gatekeeper.root: str`
  - `Gatekeeper.new_lease_id() -> str` — 6 hex chars.
  - `Gatekeeper.write_lease(lease: dict) -> None`
  - `Gatekeeper.read_lease(lease_id: str) -> dict | None`
  - `Gatekeeper.all_leases() -> list[dict]`
  - `Gatekeeper.claim_unit(unit_key: str, lease: dict) -> bool` — atomic; `False` if already held.
  - `Gatekeeper.release_unit(unit_key: str) -> None`
  - `Gatekeeper.unit_lease(unit_key: str) -> dict | None`
  - `Gatekeeper.update_unit_lease(unit_key: str, lease: dict) -> bool` — rewrite a held unit's lease in place; `False` if not held.
  - `Gatekeeper.critical_section()` — context manager, the global mutex.
  - `utcnow() -> str` module function returning ISO-8601 `Z`.

`unit_key` is the board serial when grain is `board`, and the BDF when grain is `chip`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gatekeeper_state.py`:

```python
import json
import os
import multiprocessing
import pytest
from gozer.gatekeeper import Gatekeeper, utcnow


@pytest.fixture
def gk(tmp_path, sysfs):
    from conftest import QUIETBOX
    return Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX))


def test_creates_state_dirs(gk):
    for sub in ("gate", "leases", "queue"):
        assert os.path.isdir(os.path.join(gk.root, sub))


def test_claim_is_exclusive(gk):
    assert gk.claim_unit("BOARD-A", {"lease_id": "aaa"}) is True
    assert gk.claim_unit("BOARD-A", {"lease_id": "bbb"}) is False


def test_release_allows_reclaim(gk):
    gk.claim_unit("BOARD-A", {"lease_id": "aaa"})
    gk.release_unit("BOARD-A")
    assert gk.claim_unit("BOARD-A", {"lease_id": "bbb"}) is True


def test_unit_lease_round_trips(gk):
    gk.claim_unit("BOARD-A", {"lease_id": "aaa", "who": "claude:test"})
    assert gk.unit_lease("BOARD-A")["who"] == "claude:test"
    assert gk.unit_lease("BOARD-B") is None


def test_update_unit_lease_rewrites_in_place(gk):
    gk.claim_unit("BOARD-A", {"lease_id": "aaa", "expect_done": None})
    assert gk.update_unit_lease("BOARD-A", {"lease_id": "aaa", "expect_done": "later"})
    assert gk.unit_lease("BOARD-A")["expect_done"] == "later"


def test_update_unit_lease_refuses_to_create_a_lock(gk):
    assert gk.update_unit_lease("NEVER-HELD", {"lease_id": "aaa"}) is False
    assert gk.unit_lease("NEVER-HELD") is None


def test_lease_records_round_trip(gk):
    lease = {"lease_id": "2f9a1c", "who": "claude:x", "chips": ["0000:01:00.0"]}
    gk.write_lease(lease)
    assert gk.read_lease("2f9a1c") == lease
    assert gk.read_lease("nope") is None
    assert [l["lease_id"] for l in gk.all_leases()] == ["2f9a1c"]


def test_lease_ids_are_unique_and_short(gk):
    ids = {gk.new_lease_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 6 for i in ids)


def test_utcnow_is_zulu():
    assert utcnow().endswith("Z")


def _racer(args):
    root, sysfs_root, n = args
    gk = Gatekeeper(root=root, sysfs_root=sysfs_root)
    return gk.claim_unit("CONTESTED", {"lease_id": f"r{n}"})


def test_concurrent_claims_yield_exactly_one_winner(tmp_path, sysfs):
    from conftest import QUIETBOX
    root = str(tmp_path / "state")
    sysfs_root = sysfs(QUIETBOX)
    Gatekeeper(root=root, sysfs_root=sysfs_root)  # create dirs first
    with multiprocessing.Pool(8) as pool:
        results = pool.map(_racer, [(root, sysfs_root, n) for n in range(8)])
    assert sum(results) == 1


def test_critical_section_is_reentrant_across_calls(gk):
    with gk.critical_section():
        pass
    with gk.critical_section():
        pass  # must not deadlock on a leftover mutex dir
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_gatekeeper_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gozer.gatekeeper'`

- [ ] **Step 3: Write the implementation**

Create `gozer/gatekeeper.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_gatekeeper_state.py -v`
Expected: PASS, 11 passed

- [ ] **Step 5: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/gatekeeper.py tests/test_gatekeeper_state.py
git commit -m "feat: gatekeeper state dir with mkdir as the atomic claim primitive

Sticky 1777 root so several users can share the gate. Lease records are written
via temp+rename so a concurrent reader never sees a partial JSON. The global
mutex self-heals after 30s in case a holder crashed mid-allocation; a
multiprocess race test asserts exactly one winner out of eight claimants."
```

---

### Task 4: Reconciliation — the six states

**Files:**
- Modify: `gozer/gatekeeper.py` (add reconciliation)
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `gozer.procfd.holders/pid_alive/process_name`, `gozer.topology`.
- Produces:
  - `ChipState` dataclass: `chip: Chip`, `state: str`, `lease: dict | None`, `pids: list[int]`, `overstayed: bool`
  - States, exactly these strings: `"HELD"`, `"HELD-FOREIGN"`, `"CLAIMED"`, `"STALE"`, `"BUSY-UNTRACKED"`, `"FREE"`
  - `Gatekeeper.reconcile(reap: bool = True) -> list[ChipState]`
  - `Gatekeeper.unit_key_for(chip: Chip, grain: str) -> str`
  - `Gatekeeper.boards` / `Gatekeeper.grain` — cached topology properties.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reconcile.py`:

```python
import os
import pytest
from gozer.gatekeeper import Gatekeeper, utcnow
from conftest import QUIETBOX


def fake_proc(tmp_path, pids):
    root = tmp_path / "proc"
    for pid, devs in pids.items():
        d = root / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text("python\n")
        for i, dev in enumerate(devs):
            os.symlink(f"/dev/tenstorrent/{dev}", d / "fd" / str(i + 3))
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def make(tmp_path, sysfs, pids=None):
    proc = fake_proc(tmp_path, pids or {})
    return Gatekeeper(root=str(tmp_path / "state"),
                      sysfs_root=sysfs(QUIETBOX), proc_root=proc)


def states(gk):
    return {s.chip.dev_index: s.state for s in gk.reconcile()}


def lease_for(gk, chips, pid, lease_id="aaa", expect_done=None):
    return {
        "lease_id": lease_id, "who": "claude:test", "pid": pid, "pgid": pid,
        "chips": [c for c in chips], "since": utcnow(),
        "expect_done": expect_done, "state": "active",
    }


def test_free_when_no_lease_and_no_fd(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert states(gk) == {0: "FREE", 1: "FREE", 2: "FREE", 3: "FREE"}


def test_busy_untracked_when_fd_but_no_lease(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, pids={861926: [0, 1, 2, 3]})
    assert states(gk) == {i: "BUSY-UNTRACKED" for i in range(4)}


def test_held_when_lease_pid_holds_the_fd(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, pids={4242: [0, 1]})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    s = states(gk)
    assert s[0] == "HELD" and s[1] == "HELD"
    assert s[2] == "FREE"


def test_held_foreign_when_another_pid_holds_it(tmp_path, sysfs):
    # Lease says pid 4242, but pid 9999 is the one with the device open.
    gk = make(tmp_path, sysfs, pids={4242: [], 9999: [0, 1]})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    s = states(gk)
    assert s[0] == "HELD-FOREIGN" and s[1] == "HELD-FOREIGN"


def test_claimed_when_pid_alive_but_no_fd_yet(tmp_path, sysfs):
    # Setup phase: leased, process running, device not opened yet.
    gk = make(tmp_path, sysfs, pids={4242: []})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    s = states(gk)
    assert s[0] == "CLAIMED" and s[1] == "CLAIMED"


def test_stale_is_reaped_when_pid_dead_and_no_fd(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)  # pid 4242 does not exist in the fake proc
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    result = {s.chip.dev_index: s.state for s in gk.reconcile(reap=True)}
    assert result[0] == "FREE" and result[1] == "FREE"
    assert gk.unit_lease("0000000000000002") is None
    assert gk.read_lease("aaa") is None


def test_stale_is_reported_but_not_reaped_when_reap_false(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242)
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    result = {s.chip.dev_index: s.state for s in gk.reconcile(reap=False)}
    assert result[0] == "STALE"
    assert gk.unit_lease("0000000000000002") is not None


def test_live_pid_past_expect_done_is_overstayed_not_reaped(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, pids={4242: [0, 1]})
    lease = lease_for(gk, ["0000:01:00.0", "0000:02:00.0"], pid=4242,
                      expect_done="2000-01-01T00:00:00Z")
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)
    by_idx = {s.chip.dev_index: s for s in gk.reconcile()}
    assert by_idx[0].state == "HELD"
    assert by_idx[0].overstayed is True
    assert gk.unit_lease("0000000000000002") is not None


def test_grain_and_unit_key(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.grain == "board"
    chip = gk.boards[0].chips[0]
    assert gk.unit_key_for(chip, "board") == chip.serial
    assert gk.unit_key_for(chip, "chip") == chip.bdf
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_reconcile.py -v`
Expected: FAIL — `AttributeError: 'Gatekeeper' object has no attribute 'reconcile'`

- [ ] **Step 3: Add reconciliation to `gozer/gatekeeper.py`**

Add these imports at the top of the file, after the existing ones:

```python
from dataclasses import dataclass

from gozer import procfd
from gozer.topology import Board, Chip, all_chips, lease_grain, read_topology
```

Add this dataclass above `class Gatekeeper`:

```python
@dataclass
class ChipState:
    chip: Chip
    state: str
    lease: dict | None
    pids: list[int]
    overstayed: bool = False
```

Add these members inside `class Gatekeeper`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_reconcile.py -v`
Expected: PASS, 9 passed

- [ ] **Step 5: Run the whole suite to check nothing regressed**

Run: `cd ~/code/tt-gozer && python3 -m pytest -v`
Expected: PASS — every test green, no failures or errors

- [ ] **Step 6: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/gatekeeper.py tests/test_reconcile.py
git commit -m "feat: reconcile lease bookkeeping against kernel fd truth

Six states: HELD, HELD-FOREIGN, CLAIMED, STALE, BUSY-UNTRACKED, FREE. Reaping
requires the owning pid to be gone AND no fd to remain, so a live process is
never yanked. A live lease past its advisory expect_done reports OVERSTAYED and
is left alone for a human to judge."
```

---

### Task 5: Allocation

**Files:**
- Modify: `gozer/gatekeeper.py` (add allocation)
- Test: `tests/test_allocate.py`

**Interfaces:**
- Consumes: Task 4's `reconcile`, `grain`, `unit_key_for`, `chips_in_unit`.
- Produces:
  - `parse_chip_request(spec: str, total: int) -> tuple[int, int]` — module function; returns `(minimum, maximum)`. Accepts `"1"`, `"all"`, `"1-4"`. Raises `ValueError` on anything else.
  - `Gatekeeper.free_units() -> list[str]`
  - `Gatekeeper.allocate(min_chips, max_chips, exact=None, fresh=False) -> list[str] | None` — returns unit keys to claim, or `None` if the minimum cannot be met. Does **not** claim; pure selection.
  - `Gatekeeper.eth_neighbours(units: list[str]) -> dict[str, str]` — chip BDF → holder `who`, for leased chips sharing a board with the selection but not in it.
  - `Gatekeeper.mark_clean(unit_key) / is_clean(unit_key) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_allocate.py`:

```python
import pytest
from gozer.gatekeeper import Gatekeeper, parse_chip_request, utcnow
from conftest import QUIETBOX, GALAXY_LIKE


def fake_proc(tmp_path, live_pids=(1,)):
    """A /proc where the given pids exist and hold nothing open.

    pid 1 must be alive here: several tests below plant a lease owned by pid 1,
    and free_units() reconciles first. Against an empty/absent proc tree that
    pid reads as dead, reconcile reaps the lease as STALE, and the very
    contention those tests are asserting silently disappears.
    """
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid in live_pids:
        (root / str(pid) / "fd").mkdir(parents=True, exist_ok=True)
        (root / str(pid) / "comm").write_text("python\n")
    return str(root)


def make(tmp_path, sysfs, chips=QUIETBOX):
    return Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(chips),
                      proc_root=fake_proc(tmp_path))


@pytest.mark.parametrize("spec,total,expected", [
    ("1", 4, (1, 1)),
    ("2", 4, (2, 2)),
    ("all", 4, (4, 4)),
    ("1-4", 4, (1, 4)),
    ("2-3", 4, (2, 3)),
])
def test_parse_chip_request(spec, total, expected):
    assert parse_chip_request(spec, total) == expected


@pytest.mark.parametrize("spec", ["", "zero", "3-1", "-2", "1-", "0"])
def test_parse_chip_request_rejects_garbage(spec):
    with pytest.raises(ValueError):
        parse_chip_request(spec, 4)


def test_allocates_one_board_for_one_chip_at_board_grain(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    units = gk.allocate(1, 1)
    assert len(units) == 1
    # Asking for 1 yields 2 chips, because UMD expands to the whole p300c board.
    assert len(gk.chips_in_unit(units[0])) == 2


def test_allocates_all_boards_for_all(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    units = gk.allocate(4, 4)
    assert len(units) == 2


def test_returns_none_when_minimum_cannot_be_met(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    gk.claim_unit("0000000000000002", {"lease_id": "x", "pid": 1})
    gk.claim_unit("0000000000000001", {"lease_id": "y", "pid": 1})
    assert gk.allocate(1, 1) is None


def test_elastic_takes_what_is_available_above_the_minimum(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    gk.claim_unit("0000000000000002", {"lease_id": "x", "pid": 1})
    units = gk.allocate(1, 4)  # only one board left
    assert units == ["0000000000000001"]


def test_prefers_the_lower_indexed_board_for_determinism(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.allocate(1, 1) == ["0000000000000002"]  # holds dev 0,1


def test_exact_selects_a_named_chip_or_board(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.allocate(1, 1, exact="0000:03:00.0") == ["0000000000000001"]
    assert gk.allocate(1, 1, exact="2") == ["0000000000000001"]


def test_exact_returns_none_when_that_unit_is_taken(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    gk.claim_unit("0000000000000001", {"lease_id": "x", "pid": 1})
    assert gk.allocate(1, 1, exact="0000:03:00.0") is None


def test_fresh_requires_a_clean_unit(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.allocate(1, 1, fresh=True) is None
    gk.mark_clean("0000000000000002")
    assert gk.allocate(1, 1, fresh=True) == ["0000000000000002"]


def test_chip_grain_allocates_individual_chips(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, chips=GALAXY_LIKE)
    assert gk.grain == "chip"
    units = gk.allocate(1, 1)
    assert len(units) == 1
    assert len(gk.chips_in_unit(units[0])) == 1


def test_eth_neighbours_reports_other_tenants_on_shared_boards(tmp_path, sysfs):
    # At chip grain, two tenants can share one board's eth mesh.
    gk = make(tmp_path, sysfs, chips=GALAXY_LIKE)
    gk.claim_unit("0000:01:00.0", {"lease_id": "x", "pid": 1, "who": "claude:other"})
    neighbours = gk.eth_neighbours(["0000:02:00.0"])
    assert neighbours == {"0000:01:00.0": "claude:other"}


def test_no_eth_neighbours_when_whole_board_is_yours(tmp_path, sysfs):
    gk = make(tmp_path, sysfs)
    assert gk.eth_neighbours(["0000000000000001"]) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_allocate.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_chip_request'`

- [ ] **Step 3: Add allocation to `gozer/gatekeeper.py`**

Add this module-level function after `utcnow()`:

```python
def parse_chip_request(spec: str, total: int) -> tuple[int, int]:
    """Parse --chips into (minimum, maximum).

    "1" -> (1, 1)     exactly one
    "all" -> (total, total)
    "1-4" -> (1, 4)   elastic: at least 1, up to 4
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
```

Add these members inside `class Gatekeeper`:

```python
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
        """Choose unit keys satisfying the request, or None. Does not claim."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_allocate.py -v`
Expected: PASS, 22 passed

- [ ] **Step 5: Run the whole suite**

Run: `cd ~/code/tt-gozer && python3 -m pytest -q`
Expected: PASS — every test green, no failures or errors

- [ ] **Step 6: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/gatekeeper.py tests/test_allocate.py
git commit -m "feat: allocation with elastic requests and eth-neighbour warnings

Selection prefers clean units then lowest device index so tests are
deterministic. At board grain a one-chip request grants two chips, because UMD
widens TT_VISIBLE_DEVICES to the whole p300c board -- callers surface that.
eth_neighbours reports other tenants sharing a hardwired mesh."
```

---

### Task 6: Queue

**Files:**
- Modify: `gozer/gatekeeper.py` (add queue)
- Test: `tests/test_queue.py`

**Interfaces:**
- Consumes: Task 5's allocation.
- Produces:
  - `Gatekeeper.enqueue(request: dict) -> dict` — request needs `who`, `min_chips`, `max_chips`, `pid`; returns the ticket record with `ticket`, `seq`, `since`.
  - `Gatekeeper.queue_entries() -> list[dict]` — FIFO order.
  - `Gatekeeper.queue_position(ticket: str) -> int | None` — 1-based.
  - `Gatekeeper.dequeue(ticket: str) -> None`
  - `Gatekeeper.prune_queue() -> list[str]` — drop tickets whose pid is gone; returns dropped tickets.
  - `Gatekeeper.open_claim_window(ticket: str) -> None`
  - `Gatekeeper.claim_window_holder() -> str | None` — the ticket currently entitled, honouring the 90s expiry.
  - `Gatekeeper.may_claim(ticket: str) -> bool`
  - `CLAIM_WINDOW_SECONDS = 90` module constant.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue.py`:

```python
import os
import time
import pytest
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
    # Zero-padded sequence numbers, so 10 must not sort before 9.
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


def test_prune_drops_tickets_whose_process_is_gone(tmp_path, sysfs):
    gk = make(tmp_path, sysfs, live_pids=(1,))
    alive = gk.enqueue(req("claude:alive", pid=1))
    dead = gk.enqueue(req("claude:dead", pid=999))
    dropped = gk.prune_queue()
    assert dropped == [dead["ticket"]]
    assert [e["who"] for e in gk.queue_entries()] == ["claude:alive"]
    assert gk.queue_position(alive["ticket"]) == 1


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
    assert gk.claim_window_holder() is None
    assert [e["who"] for e in gk.queue_entries()] == ["claude:b", "claude:a"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_queue.py -v`
Expected: FAIL — `ImportError: cannot import name 'CLAIM_WINDOW_SECONDS'`

- [ ] **Step 3: Add the queue to `gozer/gatekeeper.py`**

Add this constant next to the other module constants:

```python
CLAIM_WINDOW_SECONDS = 90
```

Add these members inside `class Gatekeeper`:

```python
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

    def enqueue(self, request: dict) -> dict:
        """Append a ticket. Sequence numbers are zero-padded so 10 sorts after 9."""
        with self.critical_section():
            seq = self._next_seq()
            ticket = secrets.token_hex(2)
            record = dict(request)
            record.update({"ticket": ticket, "seq": seq, "since": utcnow()})
            path = os.path.join(self._queue_dir(), f"{seq:06d}-{ticket}.json")
            _atomic_write_json(path, record)
            return record

    def queue_entries(self) -> list[dict]:
        out = []
        for entry in sorted(os.listdir(self._queue_dir())):
            if not entry.endswith(".json"):
                continue
            rec = _read_json(os.path.join(self._queue_dir(), entry))
            if rec:
                out.append(rec)
        return sorted(out, key=lambda r: r.get("seq", 0))

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
        """Drop tickets whose creating process has exited."""
        dropped = []
        for rec in self.queue_entries():
            pid = rec.get("pid")
            if pid is not None and not procfd.pid_alive(pid, self.proc_root):
                dropped.append(rec["ticket"])
                self.dequeue(rec["ticket"])
        return dropped

    def _send_to_back(self, ticket: str) -> None:
        path = self._ticket_path(ticket)
        rec = _read_json(path) if path else None
        if not rec:
            return
        with contextlib.suppress(OSError):
            os.unlink(path)
        seq = self._next_seq()
        rec["seq"] = seq
        rec["requeued_at"] = utcnow()
        _atomic_write_json(
            os.path.join(self._queue_dir(), f"{seq:06d}-{ticket}.json"), rec)

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

    def claim_window_holder(self) -> str | None:
        """The entitled ticket, or None. An expired window sends its ticket
        to the back of the queue so a dead waiter cannot block everyone."""
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
        holder = self.claim_window_holder()
        return holder is None or holder == ticket
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_queue.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/gatekeeper.py tests/test_queue.py
git commit -m "feat: FIFO queue with a 90s exclusive claim window

Sequence numbers are zero-padded so the eleventh ticket does not sort ahead of
the ninth. An expired window sends its ticket to the back rather than closing
silently, so neither a lurker stealing the slot nor a dead waiter blocking it
can wedge the queue."
```

---

### Task 6b: Extract the queue into its own module

*Added by controller ruling during execution, on an informed reviewer recommendation.*
`gozer/gatekeeper.py` reached 619 lines spanning nine concerns — layout, the mkdir
primitive, lease records, the global mutex, topology caching, reconciliation, cleanliness
bookkeeping, selection, and the queue. The queue shares nothing with the rest but the state
root and the mutex, which makes it a clean seam. Task 8 (keymaster) and Task 9 (CLI) add
more surface, so this is the moment to split.

**Files:**
- Create: `gozer/queue.py`
- Modify: `gozer/gatekeeper.py` (remove the queue section, compose and delegate)
- Modify: `tests/test_queue.py` (only if an import must change — see below)
- Create: `tests/test_queue_module.py`

## Hard requirement: the public API must not change

Tasks 8 and 9 are already written against `Gatekeeper`'s current method names. Every one of
these must keep working, with identical behaviour and signatures, called on a `Gatekeeper`:

`enqueue`, `queue_entries`, `queue_position`, `dequeue`, `prune_queue`,
`open_claim_window`, `expire_and_get_claim_holder`, `may_claim`

`from gozer.gatekeeper import CLAIM_WINDOW_SECONDS` must also keep working — `tests/test_queue.py`
imports it from there. Re-export it rather than breaking the import.

**The regression proof for this task is that `tests/test_queue.py` passes unchanged.** If you
find yourself editing its assertions, stop — that means behaviour moved, which is a failure
of this task, not a test that needs updating. Changing only an `import` line is acceptable;
changing an assertion is not.

## The extraction

`gozer/queue.py` holds a `TicketQueue` class owning everything queue-shaped:

- Constructor takes what it needs and nothing more: the queue directory path, a
  `critical_section` callable (returning a context manager), and `proc_root` for liveness
  checks. Do **not** pass it the whole `Gatekeeper` — that would recreate the coupling this
  task exists to remove.
- Move across: `enqueue`, `entries`, `position`, `dequeue`, `prune`, `open_claim_window`,
  `expire_and_get_claim_holder`, `may_claim`, the `_next_seq` / `_ticket_path` /
  `_send_to_back` / window helpers, and the constants `CLAIM_WINDOW_SECONDS` and the ticket
  collision retry limit.
- Keep the JSON helpers shared rather than duplicated. `_atomic_write_json` and `_read_json`
  currently live in `gatekeeper.py`; move them to a place both modules can import (a small
  `gozer/jsonstore.py`, or import them from `gatekeeper` if that does not create a cycle —
  it will, since `gatekeeper` imports `queue`, so prefer the shared module). Verbatim
  duplication of those two functions across modules is not acceptable.

`Gatekeeper` then composes it in `__init__` and delegates. Delegation should be thin and
obvious — a one-line forward per method, no logic. Keep the docstrings with the
implementation in `queue.py`; the delegating methods can carry a one-line "see
TicketQueue.x".

Pass the mutex in as `self.critical_section`, so the queue's locking is the same lock the
rest of the gatekeeper uses. It is reentrant now, which is what makes this safe.

## Fix to land with the split

**`dequeue` mutates without the mutex when called standalone.** It unlinks the ticket file
and may close the claim window. Inside `prune` it happens to be covered, but Task 9's
`cmd_cancel` will call it directly with no lock held, so `gozer cancel` would mutate the
queue unlocked — the same defect class as the `_send_to_back` finding, missed because that
finding scoped only `_send_to_back` and the expiring branch. Wrap `dequeue`'s unlink and
window-close in `critical_section`. This is free now that the mutex is reentrant, and safe
from both call sites.

Add a test asserting `dequeue` is safe to call standalone, i.e. outside any surrounding
critical section, and still removes the ticket and closes a window belonging to it.

## New tests

`tests/test_queue_module.py` exercises `TicketQueue` directly, constructed without a
`Gatekeeper`, to prove the decoupling is real:

- A `TicketQueue` built with a temp directory, a trivial no-op `critical_section` (a
  `contextlib.nullcontext` factory is fine), and a fake `proc_root` supports the full
  enqueue → position → dequeue cycle.
- It requires no `Gatekeeper` import to work. If `queue.py` needs to import `gatekeeper`,
  the extraction is wrong — assert this structurally by importing only `gozer.queue` in
  this test module.

## Steps

- [ ] **Step 1:** Read `gozer/gatekeeper.py`'s queue section and `tests/test_queue.py` so you
  know exactly what behaviour must survive.
- [ ] **Step 2:** Write `tests/test_queue_module.py` against the not-yet-existing
  `gozer.queue`, plus the standalone-`dequeue` test. Run them; confirm they fail for the
  right reason (no module / unlocked mutation).
- [ ] **Step 3:** Create `gozer/queue.py` with `TicketQueue`, move the shared JSON helpers to
  a module both can import, and land the `dequeue` locking fix.
- [ ] **Step 4:** Remove the queue section from `gatekeeper.py`, compose `TicketQueue`, add the
  thin delegating methods, and re-export `CLAIM_WINDOW_SECONDS`.
- [ ] **Step 5:** Run `python3 -m pytest tests/test_queue.py -v` — it must pass **unchanged**
  apart from imports. Then `tests/test_queue_module.py`, then the full suite
  (`python3 -m pytest -q`), which was 78/78 before this task.
- [ ] **Step 6:** Report the line count of `gozer/gatekeeper.py` before and after, so the
  controller can confirm the split achieved its purpose.
- [ ] **Step 7:** Commit:

```bash
git add gozer/queue.py gozer/gatekeeper.py gozer/jsonstore.py tests/
git commit -m "refactor: extract the ticket queue into its own module

gatekeeper.py had reached 619 lines across nine concerns. The queue shares
nothing with the rest but the state root and the reentrant mutex, so it splits
cleanly. Gatekeeper composes a TicketQueue and delegates; the public method
names are unchanged, so test_queue.py passes without touching an assertion.

Also wraps dequeue's mutation in the mutex. It was covered incidentally inside
prune, but cmd_cancel will call it directly with no lock held — the same defect
class as the _send_to_back finding, missed because that finding scoped only
_send_to_back and the expiring branch."
```

---

### Task 7: Reset

**Files:**
- Create: `gozer/reset.py`
- Test: `tests/test_reset.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `reset_command(bdfs: list[str], cmd: str | None = None) -> list[str]`
  - `reset_chips(bdfs, cmd=None, runner=subprocess.run) -> tuple[bool, str]` — `(ok, output)`
  - `ResetError(Exception)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reset.py`:

```python
import pytest
from gozer.reset import reset_command, reset_chips, ResetError


def test_builds_a_plain_per_bdf_reset():
    assert reset_command(["0000:03:00.0", "0000:04:00.0"]) == [
        "tt-smi", "-r", "0000:03:00.0,0000:04:00.0"]


def test_honours_the_reset_cmd_override():
    assert reset_command(["0000:01:00.0"], cmd="/fake/tt-smi")[0] == "/fake/tt-smi"


def test_never_emits_a_board_level_m3_reset():
    # ASIC_DMC_RESET / reset_m3 is the one genuinely board-wide path. Banned.
    argv = reset_command(["0000:01:00.0", "0000:02:00.0"])
    joined = " ".join(argv)
    assert "m3" not in joined.lower()
    assert "--all" not in joined and "all" not in argv


def test_refuses_an_empty_bdf_list():
    with pytest.raises(ResetError):
        reset_command([])


def test_rejects_anything_that_is_not_a_bdf():
    # Guards against an integer index sneaking in, which tt-smi would read as a
    # UMD logical id -- a different device namespace.
    with pytest.raises(ResetError):
        reset_command(["2"])


def test_reset_chips_reports_success_and_failure():
    class Ok:
        returncode, stdout, stderr = 0, "reset done", ""

    class Bad:
        returncode, stdout, stderr = 1, "", "boom"

    ok, out = reset_chips(["0000:01:00.0"], runner=lambda *a, **k: Ok())
    assert ok is True and "reset done" in out

    ok, out = reset_chips(["0000:01:00.0"], runner=lambda *a, **k: Bad())
    assert ok is False and "boom" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_reset.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gozer.reset'`

- [ ] **Step 3: Write the implementation**

Create `gozer/reset.py`:

```python
"""Reset exactly the chips we leased, and nothing else.

`tt-smi -r <bdf>[,<bdf>]` is per-ASIC. Verified against the vendor source:
ChipReset.full_lds_reset iterates only the interfaces it was handed, each ASIC
sits behind its own PCIe root port so the secondary bus reset is scoped to one
endpoint, and blackhole.c's ASIC_RESET is two config-space writes to a single
pci_dev.

The one genuinely board-wide path is reset_m3 / ASIC_DMC_RESET -- tt-umd's own
docstring calls it "a M3 board level reset". tt-smi's CLI does not expose it, and
we must never reach for it. The BDF-only guard below also stops a bare integer
slipping through, which tt-smi would interpret as a UMD logical id: a different
namespace that could reset the wrong device.
"""

from __future__ import annotations

import os
import re
import subprocess

BDF_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$")
DEFAULT_RESET_CMD = "tt-smi"


class ResetError(Exception):
    """Raised when a reset request is malformed. Never raised for a failed reset."""


def reset_command(bdfs: list[str], cmd: str | None = None) -> list[str]:
    if not bdfs:
        raise ResetError("refusing to reset an empty device list")
    for b in bdfs:
        if not BDF_RE.match(b):
            raise ResetError(
                f"not a PCI BDF: {b!r} -- gozer resets by BDF only, never by index")
    exe = cmd or os.environ.get("GOZER_RESET_CMD") or DEFAULT_RESET_CMD
    return [exe, "-r", ",".join(bdfs)]


def reset_chips(bdfs: list[str], cmd: str | None = None,
                runner=subprocess.run) -> tuple[bool, str]:
    """Run the reset. Returns (ok, combined output) rather than raising, so a
    release can report a failed reset without losing the lease teardown."""
    argv = reset_command(bdfs, cmd)
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001 - a failed reset must not crash release
        return False, f"{' '.join(argv)}: {e}"
    output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    return proc.returncode == 0, output.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_reset.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/reset.py tests/test_reset.py
git commit -m "feat: per-ASIC reset by BDF, never the board-level M3 path

Rejects bare integers so a UMD logical id cannot be mistaken for a BDF and reset
the wrong device. A failed reset returns (False, output) instead of raising, so
release can still tear the lease down and report honestly."
```

---

### Task 8: Keymaster

**Files:**
- Create: `gozer/keymaster.py`
- Test: `tests/test_keymaster.py`

**Interfaces:**
- Consumes: `Gatekeeper`, `gozer.reset`, `gozer.topology`.
- Produces:
  - `Grant` dataclass: `lease_id: str`, `units: list[str]`, `chips: list[Chip]`, `bdfs: list[str]`, `dev_indices: list[int]`, `expanded: bool`, `requested: int`, `neighbours: dict[str, str]`
  - `Keymaster(gatekeeper: Gatekeeper)`
  - `.acquire(chips_spec, who, reason=None, exact=None, fresh=False, expect=None, pid=None, ticket=None) -> Grant | dict` — a `Grant` on success, or the ticket record when queued.
  - `.release(lease_id, no_reset=False, force=False) -> tuple[bool, str]`
  - `.env_for(grant_or_lease) -> dict[str, str]`
  - `.run(argv, **acquire_kwargs) -> int` — acquire, run, always release.
  - `parse_duration(s: str) -> int` — seconds; accepts `45m`, `2h`, `90s`, bare digits = minutes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_keymaster.py`:

```python
import os
import signal
import subprocess
import sys
import pytest
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster, Grant, parse_duration
from conftest import QUIETBOX, GALAXY_LIKE


def fake_proc(tmp_path, pids=(1,)):
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        (root / str(pid) / "fd").mkdir(parents=True)
        (root / str(pid) / "comm").write_text("python\n")
    return str(root)


def make(tmp_path, sysfs, chips=QUIETBOX):
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(chips),
                    proc_root=fake_proc(tmp_path))
    return Keymaster(gk), gk


@pytest.mark.parametrize("text,secs", [
    ("90s", 90), ("45m", 2700), ("2h", 7200), ("30", 1800)])
def test_parse_duration(text, secs):
    assert parse_duration(text) == secs


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_acquire_grants_and_records_the_lease(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", reason="unit test", pid=1)
    assert isinstance(grant, Grant)
    assert len(grant.bdfs) == 2          # board grain: one chip request -> two chips
    assert grant.expanded is True
    assert grant.requested == 1
    lease = gk.read_lease(grant.lease_id)
    assert lease["who"] == "claude:test"
    assert lease["reason"] == "unit test"
    assert lease["chips"] == grant.bdfs


def test_acquire_marks_the_unit_held(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    assert gk.unit_lease(grant.units[0]) is not None


def test_second_acquire_takes_the_other_board(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    b = km.acquire("1", who="claude:b", pid=1)
    assert set(a.bdfs).isdisjoint(b.bdfs)


def test_third_acquire_is_queued_with_a_ticket(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    queued = km.acquire("1", who="claude:c", pid=1)
    assert not isinstance(queued, Grant)
    assert queued["ticket"]
    assert gk.queue_position(queued["ticket"]) == 1


def test_no_expansion_note_at_chip_grain(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs, chips=GALAXY_LIKE)
    grant = km.acquire("1", who="claude:test", pid=1)
    assert len(grant.bdfs) == 1
    assert grant.expanded is False


def test_env_emits_comma_separated_bdfs(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    env = km.env_for(grant)
    assert env["TT_VISIBLE_DEVICES"] == ",".join(grant.bdfs)
    assert ":" in env["TT_VISIBLE_DEVICES"]   # BDFs, never bare integers


def test_release_frees_the_unit_and_resets(tmp_path, sysfs):
    calls = []
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: calls.append(argv) or _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    ok, msg = km.release(grant.lease_id)
    assert ok is True
    assert gk.unit_lease(grant.units[0]) is None
    assert gk.read_lease(grant.lease_id) is None
    assert calls and calls[0][1] == "-r"
    assert set(calls[0][2].split(",")) == set(grant.bdfs)


def test_release_marks_the_unit_clean_for_fresh_requests(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    km.release(grant.lease_id)
    assert gk.is_clean(grant.units[0]) is True


def test_release_with_no_reset_skips_the_reset(tmp_path, sysfs):
    calls = []
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: calls.append(argv) or _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    km.release(grant.lease_id, no_reset=True)
    assert calls == []
    assert gk.is_clean(grant.units[0]) is False


def test_release_of_unknown_lease_reports_failure(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    ok, msg = km.release("nosuch")
    assert ok is False and "not found" in msg.lower()


def test_release_refuses_while_an_fd_is_open(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    grant = km.acquire("1", who="claude:test", pid=1)
    # pid 1 now holds one of the leased devices open.
    fd_dir = os.path.join(gk.proc_root, "1", "fd")
    os.symlink(f"/dev/tenstorrent/{grant.dev_indices[0]}", os.path.join(fd_dir, "9"))
    ok, msg = km.release(grant.lease_id)
    assert ok is False and "still open" in msg.lower()
    assert gk.unit_lease(grant.units[0]) is not None


def test_release_force_overrides_an_open_fd(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    grant = km.acquire("1", who="claude:test", pid=1)
    fd_dir = os.path.join(gk.proc_root, "1", "fd")
    os.symlink(f"/dev/tenstorrent/{grant.dev_indices[0]}", os.path.join(fd_dir, "9"))
    ok, msg = km.release(grant.lease_id, force=True)
    assert ok is True
    assert gk.unit_lease(grant.units[0]) is None


class _Ok:
    returncode, stdout, stderr = 0, "", ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_keymaster.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gozer.keymaster'`

- [ ] **Step 3: Write the implementation**

Create `gozer/keymaster.py`:

```python
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
        """Return a Grant, or a queue-ticket dict when nothing is available."""
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
                    for done in units:
                        if done == unit:
                            break
                        self.gk.release_unit(done)
                    return self._ticket_for(ticket, who, pid, min_chips, max_chips)
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

    def release(self, lease_id: str, no_reset: bool = False,
                force: bool = False) -> tuple[bool, str]:
        lease = self.gk.read_lease(lease_id)
        if lease is None:
            return False, f"lease {lease_id} not found"

        from gozer import procfd
        fd_map = procfd.holders(self.gk.proc_root)
        still_open = [d for d in lease.get("dev_indices", []) if fd_map.get(d)]
        if still_open and not force:
            return False, (
                f"chips {still_open} still open by "
                f"{sorted({p for d in still_open for p in fd_map[d]})} -- "
                "let the work finish, or pass --force")

        messages = []
        if not no_reset:
            ok, out = reset_mod.reset_chips(lease["chips"], runner=self.reset_runner)
            messages.append(out or ("reset ok" if ok else "reset failed"))
            if ok:
                for unit in lease.get("units", []):
                    self.gk.mark_clean(unit)
            else:
                messages.append("reset failed -- unit released but NOT marked clean")

        for unit in lease.get("units", []):
            self.gk.release_unit(unit)
        self.gk.delete_lease(lease_id)

        # Hand the freed chips to whoever is next in line.
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

    def run(self, argv: list[str], **acquire_kwargs) -> int:
        """Acquire, run, always release -- including on signal.

        This is the form scripts and skills should prefer: the lease is bound to
        the process, so it cannot leak if an agent is interrupted mid-session.
        """
        grant = self.acquire(**acquire_kwargs)
        if not isinstance(grant, Grant):
            print(f"queued: ticket {grant['ticket']}", file=sys.stderr)
            return 10

        env = dict(os.environ)
        env.update(self.env_for(grant))
        proc = None

        def _forward(signum, _frame):
            if proc and proc.poll() is None:
                proc.send_signal(signum)

        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        for s in previous:
            signal.signal(s, _forward)
        try:
            proc = subprocess.Popen(argv, env=env)
            return proc.wait()
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
            self.release(grant.lease_id)


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_keymaster.py -v`
Expected: PASS, 17 passed

- [ ] **Step 5: Add the `run` lifecycle tests**

Append to `tests/test_keymaster.py`:

```python
def test_run_releases_on_normal_exit(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    rc = km.run([sys.executable, "-c", "pass"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc == 0
    assert gk.all_leases() == []


def test_run_releases_on_nonzero_exit(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    rc = km.run([sys.executable, "-c", "raise SystemExit(3)"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc == 3
    assert gk.all_leases() == []


def test_run_exports_visible_devices_to_the_child(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    out = tmp_path / "seen.txt"
    code = f"import os; open({str(out)!r}, 'w').write(os.environ['TT_VISIBLE_DEVICES'])"
    km.run([sys.executable, "-c", code], chips_spec="1", who="claude:test", pid=1)
    assert ":" in out.read_text()


def test_run_releases_when_the_child_is_killed(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Ok()
    rc = km.run([sys.executable, "-c",
                 "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc != 0
    assert gk.all_leases() == []


def test_run_returns_10_when_queued(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:hog", pid=1)
    rc = km.run([sys.executable, "-c", "pass"],
                chips_spec="1", who="claude:test", pid=1)
    assert rc == 10
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_keymaster.py -v`
Expected: PASS, 22 passed

- [ ] **Step 7: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/keymaster.py tests/test_keymaster.py
git commit -m "feat: keymaster acquires leases, emits BDF env, supervises run

acquire() rolls back cleanly if it loses a race mid-claim, and honours an open
claim window so a newcomer cannot jump the queue head. release() refuses while
an fd is still open unless forced, and only marks a unit clean when the reset
actually succeeded. run() releases in a finally block and forwards SIGINT/SIGTERM
to the child, so an interrupted agent cannot leak a lease."
```

---

### Task 9: CLI

**Files:**
- Create: `gozer/cli.py`
- Modify: `gozer/procfd.py` (add `sudo_available`, `use_sudo` support — see Step 0)
- Modify: `gozer/gatekeeper.py` (add the `use_sudo` attribute — see Step 0)
- Test: `tests/test_cli.py`, `tests/test_procfd.py` (append the sudo tests from Step 0)

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `main(argv: list[str]) -> int`
  - `procfd.sudo_available() -> bool`
  - `procfd.holders(proc_root=..., use_sudo=False)` — the extended signature
  - `Gatekeeper.use_sudo: bool` (defaults `False`)

Subcommands: `status`, `topology`, `acquire` (alias `summon`), `wait`, `queue`, `cancel`,
`renew`, `release` (alias `banish`), `reconcile`, `adopt`, `env`, `run`.

- [ ] **Step 0: Make `--sudo` real**

*Added by controller ruling during execution.* The design spec promises `gozer reconcile
--sudo` recovers cross-user fd truth, and `procfd`'s docstring points at it. Without this
step `--sudo` would be accepted by argparse and silently ignored — a flag that lies is
worse than no flag.

The scan must use `sudo -n` (non-interactive) **only**. An agent running `gozer` must never
be left hanging on an invisible password prompt.

Append to `gozer/procfd.py`:

```python
import shlex
import subprocess

SUDO_SCAN_TIMEOUT = 10


def sudo_available() -> bool:
    """True when passwordless sudo works right now.

    `sudo -n true` never prompts: it fails immediately if credentials would be
    required. That matters because gozer runs under agents with no terminal to
    type a password into.
    """
    try:
        proc = subprocess.run(["sudo", "-n", "true"], capture_output=True,
                              timeout=SUDO_SCAN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _sudo_holders(proc_root: str, runner=subprocess.run) -> dict[int, list[int]]:
    """Re-run this module's own scan under sudo to see other users' processes.

    /proc/<pid>/fd is readable only by its owner, so an unprivileged scan sees
    only our own processes. Rather than shelling out to lsof (not always
    installed) we re-execute this file with the same interpreter under sudo and
    read back its JSON.
    """
    argv = ["sudo", "-n", sys.executable, os.path.abspath(__file__),
            "--scan", proc_root]
    try:
        proc = runner(argv, capture_output=True, text=True,
                      timeout=SUDO_SCAN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        raw = json.loads(proc.stdout)
    except ValueError:
        return {}
    return {int(k): sorted(v) for k, v in raw.items()}
```

Change the `holders` signature and add the elevated path. The unprivileged scan always
runs, so a failed or unavailable sudo degrades to same-user truth rather than to nothing:

```python
def holders(proc_root: str = "/proc", use_sudo: bool = False,
            runner=None) -> dict[int, list[int]]:
    """Map device index -> sorted pids holding it open.

    With use_sudo, merge in an elevated scan so other users' processes are
    visible too. The unprivileged scan still runs first, so an unavailable
    sudo degrades to same-user truth instead of to an empty result.
    """
    found: dict[int, set[int]] = {}
    ...existing scan body, unchanged, filling `found`...

    result = {dev: sorted(pids) for dev, pids in sorted(found.items())}

    if use_sudo:
        for dev, pids in _sudo_holders(
                proc_root, **({"runner": runner} if runner else {})).items():
            result[dev] = sorted(set(result.get(dev, [])) | set(pids))
    return dict(sorted(result.items()))
```

Add the module's `--scan` self-invocation at the bottom of `gozer/procfd.py`, and the
`json`/`sys` imports it needs at the top:

```python
if __name__ == "__main__":
    # Invoked as `sudo python3 procfd.py --scan /proc` by _sudo_holders above.
    # Prints the same mapping as holders(), as JSON, for the parent to merge.
    import sys as _sys
    if len(_sys.argv) == 3 and _sys.argv[1] == "--scan":
        print(json.dumps({str(k): v for k, v in holders(_sys.argv[2]).items()}))
    else:
        _sys.exit(2)
```

In `gozer/gatekeeper.py`, add `self.use_sudo = False` to `__init__`, and change the one
call in `reconcile` from `procfd.holders(self.proc_root)` to
`procfd.holders(self.proc_root, use_sudo=self.use_sudo)`.

Append these tests to `tests/test_procfd.py`:

```python
def test_sudo_available_is_false_when_sudo_refuses(monkeypatch):
    import subprocess as sp
    from gozer import procfd

    class Refused:
        returncode = 1

    monkeypatch.setattr(sp, "run", lambda *a, **k: Refused())
    assert procfd.sudo_available() is False


def test_sudo_available_is_false_when_sudo_is_missing(monkeypatch):
    import subprocess as sp
    from gozer import procfd

    def boom(*a, **k):
        raise FileNotFoundError("no sudo here")

    monkeypatch.setattr(sp, "run", boom)
    assert procfd.sudo_available() is False


def test_sudo_scan_uses_non_interactive_flag(tmp_path):
    """An agent has no terminal; sudo must never be able to prompt."""
    from gozer import procfd
    seen = {}

    class Ok:
        returncode, stdout, stderr = 0, "{}", ""

    def capture(argv, **kw):
        seen["argv"] = argv
        return Ok()

    procfd.holders(str(tmp_path), use_sudo=True, runner=capture)
    assert seen["argv"][:2] == ["sudo", "-n"]


def test_sudo_results_merge_with_unprivileged_scan(tmp_path):
    from gozer import procfd
    p = fake_proc(tmp_path, {100: ("mine", [1])})

    class Ok:
        returncode = 0
        stdout = '{"1": [200], "2": [300]}'
        stderr = ""

    merged = procfd.holders(p, use_sudo=True, runner=lambda *a, **k: Ok())
    assert merged == {1: [100, 200], 2: [300]}


def test_failed_sudo_degrades_to_same_user_truth(tmp_path):
    """Losing the elevated scan must not lose the scan we could do."""
    from gozer import procfd
    p = fake_proc(tmp_path, {100: ("mine", [1])})

    class Failed:
        returncode, stdout, stderr = 1, "", "sudo: a password is required"

    assert procfd.holders(p, use_sudo=True,
                          runner=lambda *a, **k: Failed()) == {1: [100]}
```

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_procfd.py -v`
Expected: PASS — the pre-existing procfd tests plus these 5.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
import json
import os
import pytest
from gozer.cli import main
from conftest import QUIETBOX


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    proc = tmp_path / "proc" / "1" / "fd"
    proc.mkdir(parents=True)
    (tmp_path / "proc" / "1" / "comm").write_text("python\n")
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return tmp_path


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def test_status_json_lists_every_chip_free(env, capsys):
    code, out = run(["status", "--json"], capsys)
    assert code == 0
    data = json.loads(out)
    assert len(data["chips"]) == 4
    assert {c["state"] for c in data["chips"]} == {"FREE"}


def test_status_human_output_names_boards_and_chips(env, capsys):
    code, out = run(["status"], capsys)
    assert code == 0
    assert "0000000000000001" in out
    assert "chip 0" in out and "FREE" in out


def test_topology_reports_grain(env, capsys):
    code, out = run(["topology", "--json"], capsys)
    assert json.loads(out)["grain"] == "board"


def test_acquire_prints_the_export_line_and_exits_zero(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test"], capsys)
    assert code == 0
    assert "export TT_VISIBLE_DEVICES=" in out
    assert "0000:" in out


def test_acquire_notes_the_board_expansion(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test"], capsys)
    assert "expand" in out.lower() or "whole" in out.lower()


def test_acquire_json_is_machine_readable(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "x", "--json"], capsys)
    data = json.loads(out)
    assert data["granted"] is True
    assert data["env"]["TT_VISIBLE_DEVICES"].count(",") == 1


def test_third_acquire_exits_10_with_a_ticket(env, capsys):
    run(["acquire", "--chips", "1", "--who", "a"], capsys)
    run(["acquire", "--chips", "1", "--who", "b"], capsys)
    code, out = run(["acquire", "--chips", "1", "--who", "c", "--json"], capsys)
    assert code == 10
    assert json.loads(out)["ticket"]


def test_summon_is_an_alias_for_acquire(env, capsys):
    code, out = run(["summon", "--chips", "1", "--who", "claude:test"], capsys)
    assert code == 0 and "export TT_VISIBLE_DEVICES=" in out


def test_release_frees_the_board(env, capsys):
    _, out = run(["acquire", "--chips", "1", "--who", "x", "--json"], capsys)
    lease = json.loads(out)["lease_id"]
    code, _ = run(["release", lease], capsys)
    assert code == 0
    _, status = run(["status", "--json"], capsys)
    assert {c["state"] for c in json.loads(status)["chips"]} == {"FREE"}


def test_banish_is_an_alias_for_release(env, capsys):
    _, out = run(["acquire", "--chips", "1", "--who", "x", "--json"], capsys)
    lease = json.loads(out)["lease_id"]
    assert run(["banish", lease], capsys)[0] == 0


def test_release_of_unknown_lease_exits_13(env, capsys):
    code, _ = run(["release", "nosuch"], capsys)
    assert code == 13


def test_env_prints_export_lines_for_a_lease(env, capsys):
    _, out = run(["acquire", "--chips", "1", "--who", "x", "--json"], capsys)
    lease = json.loads(out)["lease_id"]
    code, text = run(["env", lease], capsys)
    assert code == 0 and text.startswith("export TT_VISIBLE_DEVICES=")


def test_queue_lists_waiting_requests(env, capsys):
    run(["acquire", "--chips", "all", "--who", "hog"], capsys)
    run(["acquire", "--chips", "1", "--who", "waiter"], capsys)
    code, out = run(["queue", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["queue"][0]["who"] == "waiter"


def test_cancel_removes_a_ticket(env, capsys):
    run(["acquire", "--chips", "all", "--who", "hog"], capsys)
    _, out = run(["acquire", "--chips", "1", "--who", "w", "--json"], capsys)
    ticket = json.loads(out)["ticket"]
    assert run(["cancel", ticket], capsys)[0] == 0
    _, q = run(["queue", "--json"], capsys)
    assert json.loads(q)["queue"] == []


def test_adopt_wraps_a_lease_around_untracked_work(env, capsys, tmp_path):
    # Simulate a manually started server holding chips 0 and 1.
    fd = tmp_path / "proc" / "1" / "fd"
    os.symlink("/dev/tenstorrent/0", fd / "3")
    os.symlink("/dev/tenstorrent/1", fd / "4")
    _, before = run(["status", "--json"], capsys)
    assert "BUSY-UNTRACKED" in before
    code, _ = run(["adopt", "0", "--who", "manual:vllm"], capsys)
    assert code == 0
    _, after = run(["status", "--json"], capsys)
    states = {c["dev_index"]: c["state"] for c in json.loads(after)["chips"]}
    assert states[0] == "HELD" and states[1] == "HELD"


def test_wait_times_out_and_exits_11(env, capsys):
    run(["acquire", "--chips", "all", "--who", "hog"], capsys)
    _, out = run(["acquire", "--chips", "1", "--who", "w", "--json"], capsys)
    ticket = json.loads(out)["ticket"]
    code, _ = run(["wait", ticket, "--timeout", "1s"], capsys)
    assert code == 11


def test_unreadable_topology_exits_14(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", str(tmp_path / "nothing"))
    code, _ = run(["status"], capsys)
    assert code == 14


def test_reconcile_sudo_flag_reaches_the_scan(env, capsys, monkeypatch):
    """--sudo must actually change behaviour, not just be accepted."""
    from gozer import procfd
    seen = {}
    real = procfd.holders

    def spy(proc_root="/proc", use_sudo=False, **kw):
        seen["use_sudo"] = use_sudo
        return real(proc_root)

    monkeypatch.setattr(procfd, "holders", spy)
    code, _ = run(["reconcile", "--sudo"], capsys)
    assert code == 0
    assert seen["use_sudo"] is True


def test_reconcile_without_sudo_does_not_elevate(env, capsys, monkeypatch):
    from gozer import procfd
    seen = {}
    real = procfd.holders

    def spy(proc_root="/proc", use_sudo=False, **kw):
        seen["use_sudo"] = use_sudo
        return real(proc_root)

    monkeypatch.setattr(procfd, "holders", spy)
    run(["reconcile"], capsys)
    assert seen["use_sudo"] is False


def test_status_never_opens_a_device_node(env, capsys, monkeypatch):
    """Hard guard on the core safety promise of this tool."""
    real_open = open
    opened = []

    def watching_open(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", watching_open)
    run(["status"], capsys)
    assert not any(p.startswith("/dev/tenstorrent") for p in opened)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gozer.cli'`

- [ ] **Step 3: Write the implementation**

Create `gozer/cli.py`:

```python
"""Command-line surface for gozer.

Verbs stay plain and guessable because agents and scripts have to discover them.
The metaphor gets exactly two aliases: `summon` for acquire, `banish` for release.
"""

from __future__ import annotations

import argparse
import json as jsonlib
import os
import sys
import time

from gozer import __version__, procfd
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Grant, Keymaster, parse_duration
from gozer.topology import TopologyError

EXIT_OK = 0
EXIT_QUEUED = 10
EXIT_WAIT_TIMEOUT = 11
EXIT_UNAVAILABLE = 12
EXIT_NO_LEASE = 13
EXIT_NO_TOPOLOGY = 14


def _make(args) -> tuple[Gatekeeper, Keymaster]:
    gk = Gatekeeper(
        root=os.environ.get("GOZER_ROOT"),
        sysfs_root=os.environ.get("GOZER_SYSFS_ROOT"),
        proc_root=os.environ.get("GOZER_PROC_ROOT", "/proc"),
    )
    return gk, Keymaster(gk)


def _emit(payload: dict, human: str, as_json: bool) -> None:
    print(jsonlib.dumps(payload, indent=2, sort_keys=True) if as_json else human)


# ---- commands -------------------------------------------------------------

def cmd_status(args) -> int:
    gk, _ = _make(args)
    states = gk.reconcile()
    payload = {
        "grain": gk.grain,
        "chips": [
            {"dev_index": s.chip.dev_index, "bdf": s.chip.bdf,
             "board": s.chip.serial, "card": s.chip.card, "state": s.state,
             "who": (s.lease or {}).get("who"), "pid": (s.lease or {}).get("pid"),
             "reason": (s.lease or {}).get("reason"),
             "pids_holding": s.pids, "overstayed": s.overstayed}
            for s in states
        ],
        "queue": gk.queue_entries(),
    }
    lines = [f"grain: {gk.grain}   ({len(gk.boards)} boards, {len(states)} chips)"]
    current_board = None
    for s in states:
        if s.chip.serial != current_board:
            current_board = s.chip.serial
            lines.append(f"board {current_board}  ({s.chip.card})")
        who = (s.lease or {}).get("who", "")
        pid = (s.lease or {}).get("pid", "")
        extra = f"  {who} pid {pid}" if who else ""
        if s.state == "BUSY-UNTRACKED":
            extra = f"  pid {','.join(map(str, s.pids))} — no lease; try: gozer adopt"
        if s.overstayed:
            extra += "  OVERSTAYED"
        lines.append(f"  chip {s.chip.dev_index}  {s.chip.bdf}  {s.state:<15}{extra}")
    q = gk.queue_entries()
    if q:
        lines.append("queue:")
        for i, e in enumerate(q, 1):
            lines.append(f"  {i}) {e['who']}  wants {e['min_chips']} chip(s)"
                         f"  since {e['since']}")
    _emit(payload, "\n".join(lines), args.json)
    return EXIT_OK


def cmd_topology(args) -> int:
    gk, _ = _make(args)
    payload = {
        "grain": gk.grain,
        "boards": [
            {"serial": b.serial, "card": b.card,
             "chips": [{"dev_index": c.dev_index, "bdf": c.bdf,
                        "asic_id": c.asic_id} for c in b.chips]}
            for b in gk.boards
        ],
    }
    lines = [f"grain: {gk.grain}"]
    for b in gk.boards:
        lines.append(f"board {b.serial}  ({b.card})")
        for c in b.chips:
            lines.append(f"  chip {c.dev_index}  {c.bdf}  asic {c.asic_id}")
    _emit(payload, "\n".join(lines), args.json)
    return EXIT_OK


def _render_grant(grant: Grant, gk) -> str:
    idx = ",".join(str(i) for i in grant.dev_indices)
    lines = [f"granted: chips {idx}  (units {', '.join(grant.units)})"]
    if grant.expanded:
        lines.append(f"  note: asked for {grant.requested} — UMD expands "
                     "TT_VISIBLE_DEVICES to the whole board")
    lines.append(f"  export TT_VISIBLE_DEVICES={','.join(grant.bdfs)}")
    lines.append(f"  lease {grant.lease_id}   release with: "
                 f"gozer release {grant.lease_id}")
    for bdf, who in grant.neighbours.items():
        lines.append(f"  ! live eth neighbour {bdf} held by {who} — "
                     "your reset on release may perturb it")
    return "\n".join(lines)


def cmd_acquire(args) -> int:
    gk, km = _make(args)
    result = km.acquire(args.chips, who=args.who, reason=args.reason,
                        exact=args.exact, fresh=args.fresh, expect=args.expect,
                        ticket=args.ticket)
    if isinstance(result, Grant):
        payload = {
            "granted": True, "lease_id": result.lease_id, "units": result.units,
            "chips": result.bdfs, "dev_indices": result.dev_indices,
            "expanded": result.expanded, "requested": result.requested,
            "neighbours": result.neighbours,
            "env": km.env_for(result),
        }
        _emit(payload, _render_grant(result, gk), args.json)
        return EXIT_OK

    if args.no_queue:
        _emit({"granted": False, "queued": False}, "unavailable: no chips free",
              args.json)
        return EXIT_UNAVAILABLE

    pos = gk.queue_position(result["ticket"])
    ahead = [e["who"] for e in gk.queue_entries()[:max(pos - 1, 0)]]
    payload = {"granted": False, "queued": True, "ticket": result["ticket"],
               "position": pos, "ahead": ahead}
    human = (f"queued: ticket {result['ticket']}  position {pos}\n"
             f"  ahead: {', '.join(ahead) if ahead else '(none)'}\n"
             f"  do other work, then: gozer wait {result['ticket']}")
    _emit(payload, human, args.json)
    return EXIT_QUEUED


def cmd_wait(args) -> int:
    gk, km = _make(args)
    deadline = time.monotonic() + parse_duration(args.timeout)
    while time.monotonic() < deadline:
        entry = next((e for e in gk.queue_entries()
                      if e["ticket"] == args.ticket), None)
        if entry is None:
            _emit({"granted": False, "ticket": args.ticket, "reason": "gone"},
                  f"ticket {args.ticket} is no longer queued", args.json)
            return EXIT_NO_LEASE
        if gk.may_claim(args.ticket):
            result = km.acquire(f"{entry['min_chips']}-{entry['max_chips']}",
                                who=entry["who"], ticket=args.ticket)
            if isinstance(result, Grant):
                payload = {"granted": True, "lease_id": result.lease_id,
                           "chips": result.bdfs, "env": km.env_for(result)}
                _emit(payload, _render_grant(result, gk), args.json)
                return EXIT_OK
        time.sleep(1)

    pos = gk.queue_position(args.ticket)
    _emit({"granted": False, "ticket": args.ticket, "position": pos},
          f"still queued at position {pos}; run "
          f"`gozer wait {args.ticket}` again", args.json)
    return EXIT_WAIT_TIMEOUT


def cmd_queue(args) -> int:
    gk, _ = _make(args)
    entries = gk.queue_entries()
    human = "\n".join(
        f"{i}) {e['who']}  wants {e['min_chips']} chip(s)  ticket {e['ticket']}"
        for i, e in enumerate(entries, 1)) or "(queue empty)"
    _emit({"queue": entries}, human, args.json)
    return EXIT_OK


def cmd_cancel(args) -> int:
    gk, _ = _make(args)
    if gk.queue_position(args.ticket) is None:
        _emit({"cancelled": False}, f"no such ticket: {args.ticket}", args.json)
        return EXIT_NO_LEASE
    gk.dequeue(args.ticket)
    _emit({"cancelled": True}, f"cancelled {args.ticket}", args.json)
    return EXIT_OK


def cmd_renew(args) -> int:
    gk, _ = _make(args)
    lease = gk.read_lease(args.lease)
    if lease is None:
        _emit({"renewed": False}, f"lease {args.lease} not found", args.json)
        return EXIT_NO_LEASE
    from gozer.keymaster import _deadline
    lease["expect_done"] = _deadline(args.expect)
    gk.write_lease(lease)
    for unit in lease.get("units", []):
        gk.update_unit_lease(unit, lease)
    _emit({"renewed": True, "expect_done": lease["expect_done"]},
          f"lease {args.lease} now expected done {lease['expect_done']}", args.json)
    return EXIT_OK


def cmd_release(args) -> int:
    _, km = _make(args)
    ok, msg = km.release(args.lease, no_reset=args.no_reset, force=args.force)
    _emit({"released": ok, "message": msg}, msg, args.json)
    if ok:
        return EXIT_OK
    return EXIT_NO_LEASE if "not found" in msg else EXIT_UNAVAILABLE


def cmd_reconcile(args) -> int:
    gk, _ = _make(args)
    if args.sudo:
        gk.use_sudo = True
    states = gk.reconcile(reap=True)
    payload = {"chips": [{"dev_index": s.chip.dev_index, "state": s.state}
                         for s in states],
               "sudo": bool(args.sudo)}
    human = "\n".join(f"chip {s.chip.dev_index}  {s.state}" for s in states)
    if args.sudo and not procfd.sudo_available():
        human += ("\n! --sudo requested but passwordless sudo is unavailable; "
                  "results cover your own processes only")
    _emit(payload, human, args.json)
    return EXIT_OK


def cmd_adopt(args) -> int:
    gk, km = _make(args)
    from gozer import procfd
    from gozer.gatekeeper import utcnow
    fd_map = procfd.holders(gk.proc_root)
    target = gk._resolve_exact(args.target)
    if target is None:
        _emit({"adopted": False}, f"unknown chip or board: {args.target}", args.json)
        return EXIT_NO_LEASE

    chips = gk.chips_in_unit(target)
    pids = sorted({p for c in chips for p in fd_map.get(c.dev_index, [])})
    if not pids:
        _emit({"adopted": False}, f"{args.target} has no process holding it",
              args.json)
        return EXIT_UNAVAILABLE

    lease_id = gk.new_lease_id()
    lease = {
        "lease_id": lease_id, "chips": [c.bdf for c in chips],
        "dev_indices": [c.dev_index for c in chips], "units": [target],
        "who": args.who, "pid": pids[0], "pgid": pids[0],
        "reason": args.reason or "adopted pre-existing work",
        "since": utcnow(), "expect_done": None,
        "reset_on_release": True, "state": "adopted",
    }
    if not gk.claim_unit(target, lease):
        _emit({"adopted": False}, f"{target} is already leased", args.json)
        return EXIT_UNAVAILABLE
    gk.write_lease(lease)
    _emit({"adopted": True, "lease_id": lease_id, "pids": pids},
          f"adopted {target} for {args.who} (pid {pids[0]}), lease {lease_id}",
          args.json)
    return EXIT_OK


def cmd_env(args) -> int:
    gk, km = _make(args)
    lease = gk.read_lease(args.lease)
    if lease is None:
        _emit({"error": "not found"}, f"lease {args.lease} not found", args.json)
        return EXIT_NO_LEASE
    env = km.env_for(lease)
    _emit(env, "\n".join(f"export {k}={v}" for k, v in env.items()), args.json)
    return EXIT_OK


def cmd_run(args) -> int:
    _, km = _make(args)
    if not args.command:
        print("nothing to run: put the command after --", file=sys.stderr)
        return EXIT_UNAVAILABLE
    return km.run(args.command, chips_spec=args.chips, who=args.who,
                  reason=args.reason, exact=args.exact, fresh=args.fresh,
                  expect=args.expect)


# ---- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gozer",
        description="Cooperative Tenstorrent chip leasing. The keymaster must "
                    "meet the gatekeeper for the coming of Gozer.")
    p.add_argument("--version", action="version", version=f"gozer {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text, aliases=()):
        sp = sub.add_parser(name, help=help_text, aliases=list(aliases))
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.set_defaults(func=fn)
        return sp

    add("status", cmd_status, "who holds what, and who is waiting")
    t = add("topology", cmd_topology, "chips, boards, and the lease grain")
    t.add_argument("--refresh", action="store_true", help="ignore the cache")

    for name, aliases in (("acquire", ("summon",)),):
        a = add(name, cmd_acquire, "take a lease on chips", aliases=aliases)
        a.add_argument("--chips", default="1", help="N, all, or LO-HI")
        a.add_argument("--who", required=True, help="e.g. claude:ttm-optimize")
        a.add_argument("--reason", default=None)
        a.add_argument("--exact", default=None, help="a BDF, device index, or unit")
        a.add_argument("--fresh", action="store_true", help="require reset chips")
        a.add_argument("--expect", default=None, help="advisory duration, e.g. 45m")
        a.add_argument("--ticket", default=None, help="claim against a queue ticket")
        a.add_argument("--no-queue", action="store_true",
                       help="fail instead of queueing")

    w = add("wait", cmd_wait, "block until a ticket is granted")
    w.add_argument("ticket")
    w.add_argument("--timeout", default="8m",
                   help="bounded so it fits inside an agent tool call")

    add("queue", cmd_queue, "list waiting requests")
    c = add("cancel", cmd_cancel, "drop a queue ticket")
    c.add_argument("ticket")

    r = add("renew", cmd_renew, "extend a lease's advisory deadline")
    r.add_argument("lease")
    r.add_argument("--expect", required=True)

    for name, aliases in (("release", ("banish",)),):
        rel = add(name, cmd_release, "give chips back (resets them)", aliases=aliases)
        rel.add_argument("lease")
        rel.add_argument("--no-reset", action="store_true")
        rel.add_argument("--force", action="store_true",
                         help="release even while a device is open")

    rec = add("reconcile", cmd_reconcile, "re-derive state from kernel truth")
    rec.add_argument("--sudo", action="store_true",
                     help="use sudo for cross-user fd visibility")

    ad = add("adopt", cmd_adopt, "wrap a lease around untracked running work")
    ad.add_argument("target", help="a BDF, device index, or unit key")
    ad.add_argument("--who", required=True)
    ad.add_argument("--reason", default=None)

    e = add("env", cmd_env, "print export lines for a lease")
    e.add_argument("lease")

    rn = add("run", cmd_run, "acquire, run a command, always release")
    rn.add_argument("--chips", default="1")
    rn.add_argument("--who", default="shell:gozer-run")
    rn.add_argument("--reason", default=None)
    rn.add_argument("--exact", default=None)
    rn.add_argument("--fresh", action="store_true")
    rn.add_argument("--expect", default=None)
    rn.add_argument("command", nargs=argparse.REMAINDER)

    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    try:
        return args.func(args)
    except TopologyError as e:
        print(f"gozer: {e}", file=sys.stderr)
        return EXIT_NO_TOPOLOGY
    except ValueError as e:
        print(f"gozer: {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_cli.py -v`
Expected: PASS, 20 passed

- [ ] **Step 5: Smoke-test against the real box, read-only**

These two commands touch real hardware state but only read it. Neither acquires,
releases, nor resets anything.

Run: `cd ~/code/tt-gozer && ./bin/gozer topology && ./bin/gozer status`
Expected: two boards listed with distinct serials read back from sysfs (this box
reported `0000000000000001` and `0000000000000002`), grain `board`, and every chip
reported `BUSY-UNTRACKED` or `HELD` if a workload is running, `FREE` otherwise.
**Do not run `acquire` or `release` against the real box in this step** — a release
would reset chips someone may be using.

- [ ] **Step 6: Run the whole suite**

Run: `cd ~/code/tt-gozer && python3 -m pytest -q`
Expected: PASS — every test green, no failures or errors

- [ ] **Step 7: Commit**

```bash
cd ~/code/tt-gozer
git add gozer/cli.py tests/test_cli.py
git commit -m "feat: gozer CLI with human and --json output and fixed exit codes

Verbs stay plain for discoverability; summon/banish are documented aliases. A
regression test asserts that status never opens anything under /dev/tenstorrent,
which is the core safety promise of the tool."
```

---

### Task 10: Skills, installer, and docs

**Files:**
- Create: `skills/gozer-keymaster/SKILL.md`, `skills/gozer-gatekeeper/SKILL.md`, `install.sh`, `README.md`, `CLAUDE.md`
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: the finished CLI.
- Produces: an installed `~/.local/bin/gozer` and two linked skill directories.

- [ ] **Step 1: Write the failing installer test**

Create `tests/test_install.py`:

```python
import os
import subprocess
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_installer_is_executable():
    assert os.access(os.path.join(REPO, "install.sh"), os.X_OK)


def test_installer_links_cli_and_skills(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    # A pre-existing unrelated skill must survive: we link individual dirs,
    # never the parent, because ~/.claude/skills already holds ~30 of them.
    (home / ".claude" / "skills" / "unrelated").mkdir()

    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([os.path.join(REPO, "install.sh")],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    assert os.path.islink(home / ".local" / "bin" / "gozer")
    assert os.path.islink(home / ".claude" / "skills" / "gozer-keymaster")
    assert os.path.islink(home / ".claude" / "skills" / "gozer-gatekeeper")
    assert (home / ".claude" / "skills" / "unrelated").is_dir()


def test_installer_is_idempotent(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    env = dict(os.environ, HOME=str(home))
    for _ in range(2):
        r = subprocess.run([os.path.join(REPO, "install.sh")],
                           env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_skills_have_frontmatter_with_functional_descriptions():
    for name in ("gozer-keymaster", "gozer-gatekeeper"):
        path = os.path.join(REPO, "skills", name, "SKILL.md")
        text = open(path).read()
        assert text.startswith("---\n")
        assert f"name: {name}" in text
        desc = [l for l in text.splitlines() if l.startswith("description:")][0]
        # An agent must match on function, not on the Ghostbusters metaphor.
        assert "chip" in desc.lower() or "hardware" in desc.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/tt-gozer && python3 -m pytest tests/test_install.py -v`
Expected: FAIL — `install.sh` does not exist

- [ ] **Step 3: Write `install.sh`**

Create `install.sh` (then `chmod +x install.sh`):

```bash
#!/usr/bin/env bash
# Install gozer: the CLI onto PATH, the two skills into ~/.claude/skills.
#
# Idempotent — safe to re-run. Links rather than copies, so a git pull updates
# everything in place.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SKILL_DIR="${HOME}/.claude/skills"

mkdir -p "$BIN_DIR" "$SKILL_DIR"

echo "--- gozer ---"

# CLI
ln -sfn "$REPO/bin/gozer" "$BIN_DIR/gozer"
echo "  $BIN_DIR/gozer -> $REPO/bin/gozer"

# Skills. Link each directory INDIVIDUALLY: ~/.claude/skills already holds
# ~30 unrelated skills, so linking the parent would clobber them.
for skill in gozer-keymaster gozer-gatekeeper; do
    ln -sfn "$REPO/skills/$skill" "$SKILL_DIR/$skill"
    echo "  $SKILL_DIR/$skill -> $REPO/skills/$skill"
done

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "  note: $BIN_DIR is not on PATH — add it to your shell profile" ;;
esac

echo "  done. Try: gozer status"
```

- [ ] **Step 4: Write the two skills**

Create `skills/gozer-keymaster/SKILL.md`:

```markdown
---
name: gozer-keymaster
description: Use before running any workload that touches Tenstorrent chips (vLLM, tt-metal, TTNN, on-device pytest, tt-smi -r) - acquires a chip lease, tells you which chips you got and how to target them, and queues you when the box is busy.
---

# Getting chips: the keymaster

The box is shared. Several agents may be working at once, so **take a lease before
you open any device**, and honour the chips you were given.

## The one-liner that cannot go wrong

If your work is a single command, wrap it. The lease is bound to the process, so
it is released even if you are interrupted:

```bash
gozer run --chips 1 --who "claude:<your-skill>" --reason "<what you are doing>" -- \
    python your_workload.py
```

## When you need the lease to outlive one command

```bash
gozer acquire --chips 1 --who "claude:<your-skill>" --reason "<why>" --expect 45m
```

Read the output. Three things matter:

1. **The export line.** Run it, or pass the value through to your process.
   `TT_VISIBLE_DEVICES` is BDFs, not indices.
2. **The expansion note.** On a 2-chip board (a p300c QuietBox) asking for 1 chip
   grants 2. Requesting one chip through UMD's constrained-device / visibility
   path may expose both chips on the same board, so `TT_VISIBLE_DEVICES` cannot
   fence you to one of them. You got both; that is correct, not a bug. Direct
   TTNN device access *can* open a single enumerated chip — but a lease scoped
   narrower than the fence that enforces it would be a lease in name only, so
   gozer grants the pair.
3. **Any eth-neighbour warning.** It means another tenant shares your board's
   hardwired mesh and your reset on release may perturb them. Proceed, but say so
   if something odd happens to them.

Release when you are done:

```bash
gozer release <lease-id>
```

Release resets your chips so the next tenant gets clean silicon. Use
`--no-reset` only when you are handing off to your own follow-up run.

## When you are queued (exit code 10)

You get a ticket and a position. **Do not spin.** Go do work that does not need
hardware — read code, write tests, prepare configs. Then:

```bash
gozer wait <ticket> --timeout 8m
```

`wait` blocks up to 8 minutes (deliberately under the 10-minute tool-call limit)
and returns either a grant or your current position. Loop it if you are still
waiting, or `gozer cancel <ticket>` if you have changed plans.

## How many chips to ask for

| Situation | Ask for |
|-----------|---------|
| Single-chip test, smoke run, small model | `--chips 1` |
| Multi-chip / mesh work, large model | `--chips all` |
| Either would do, take what is free | `--chips 1-4` |
| A specific chip, for a reproduction | `--exact 2` or `--exact 0000:03:00.0` |
| Must start from clean silicon | add `--fresh` |

Prefer the elastic form when your work can scale — it gets you started sooner
and keeps the queue moving.

## Conventions

- `--who` should be `claude:<skill-name>` so a human reading `gozer status`
  knows which agent is holding the box.
- `--reason` is free text and shows up in status. Write something a colleague
  could act on.
- `--expect` is advisory only. Nothing reaps you when it lapses; it just tells
  people waiting how long you think you will be.

If something looks stuck or a chip is busy with no lease, switch to the
`gozer-gatekeeper` skill.
```

Create `skills/gozer-gatekeeper/SKILL.md`:

```markdown
---
name: gozer-gatekeeper
description: Use when Tenstorrent chips appear stuck, contended, or busy without a lease - reads the real state of the gate, distinguishes stale locks from live work, adopts untracked processes, and decides whether a reset is safe.
---

# Reading the gate: the gatekeeper

Use this when `gozer acquire` will not grant, something looks wedged, or you are
about to reset and want to know whether that is safe.

## Start here

```bash
gozer status
```

Every chip is in exactly one of six states. What each means, and what to do:

| State | Meaning | Do |
|-------|---------|-----|
| `FREE` | No lease, nothing holding it | Acquire it. |
| `HELD` | Leased, and the lease's own process has it open | Nothing. Working as intended. |
| `CLAIMED` | Leased, process alive, device not opened yet | Nothing. Setup phase, or between runs. |
| `HELD-FOREIGN` | Leased to one pid, a *different* pid has it open | Investigate before anything else — the lease is lying. |
| `BUSY-UNTRACKED` | No lease, but a process has it open | Do not take it. Adopt it (below). |
| `STALE` | Leased, process gone, no fd | Already reaped for you; re-run `gozer status`. |

`OVERSTAYED` is a flag, not a state: the lease ran past its advisory `--expect`.
The process is still alive and still working. **This is not permission to reap
it.** Ask the human, or wait.

## Untracked work

A chip open with no lease usually means someone started a server by hand. Make it
visible so the queue stops treating it as free:

```bash
gozer adopt 0 --who "manual:vllm-qwen3" --reason "server started outside gozer"
```

## Stale locks

`gozer reconcile` re-derives everything from kernel truth and reaps what is
genuinely dead. It is conservative on purpose: a lease is only removed when the
owning process is **gone** *and* no file descriptor remains. If a lock survives
reconcile, the work behind it is not finished — leave it alone.

Cross-user note: `/proc/<pid>/fd` is readable only by its owner, so fd truth
covers your own processes. If you suspect another user's process:

```bash
gozer reconcile --sudo
```

## Before you reset

Never `tt-smi -r` by hand while others may be working. Release does the reset for
you, scoped to exactly your chips:

```bash
gozer release <lease-id>
```

If you must reset outside a lease, check two things first:

1. `gozer status` shows nothing `HELD`, `CLAIMED`, or `BUSY-UNTRACKED` on those chips.
2. No eth-neighbour warning — the p300c mesh is hardwired between ASICs, so a
   reset may perturb a neighbour's links.

Never pass an M3/DMC reset. That is the one genuinely board-wide path; `gozer`
never emits it and neither should you.

## If the gate itself is wedged

The allocator mutex self-heals after 30 seconds. If `gozer` still hangs and you
are certain nothing is running:

```bash
ls -la /tmp/tt-gozer/          # inspect by hand; it is all plain files
rmdir /tmp/tt-gozer/.gatekeeper.lock
```
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/code/tt-gozer && chmod +x install.sh && python3 -m pytest tests/test_install.py -v`
Expected: PASS, 4 passed

- [ ] **Step 6: Write `README.md` and `CLAUDE.md`**

Create `README.md`:

```markdown
# tt-gozer

Cooperative chip leasing for Tenstorrent boxes, so several agents — Claude Code
sessions, aider, shell scripts, cron — can share one machine without corrupting
each other's runs.

*The keymaster must meet the gatekeeper for the coming of Gozer.* The
**gatekeeper** guards the gate: lock state, allocation, reconciliation, the
queue. The **keymaster** carries the key: your lease, your `TT_VISIBLE_DEVICES`,
your supervised process. **Gozer** is your workload, arriving on the chips.

## Install

```bash
git clone git@github.com:tsingletaryTT/tt-gozer.git ~/code/tt-gozer
~/code/tt-gozer/install.sh
gozer status
```

Python 3.9+, stdlib only. No dependencies.

## Use

```bash
# The safe default: lease bound to the process, released even on crash.
gozer run --chips 1 --who "me:experiment" -- python workload.py

# Or hold a lease across several commands.
gozer acquire --chips all --who "me:bringup" --reason "llama3 8b" --expect 2h
eval "$(gozer env <lease-id>)"
...
gozer release <lease-id>

# Who has what?
gozer status
```

Exit codes: `0` granted · `10` queued · `11` wait timed out · `12` unavailable ·
`13` no such lease · `14` topology unreadable.

## What it guarantees, and what it does not

**Advisory, not enforced.** Device nodes are `crw-rw-rw-`; any process can ignore
gozer entirely. What gozer guarantees is that ignoring it is *visible* — such a
chip shows as `BUSY-UNTRACKED` and is never handed out.

**fd truth is same-user.** `/proc/<pid>/fd` is readable only by its owner, so
ground truth covers your own processes. Cross-user detection degrades to the
world-readable lease files; `gozer reconcile --sudo` recovers full truth.

**State is per-machine.** `/tmp/tt-gozer` is local and does not sync.

For enforceable isolation, use device-cgroups (`docker --device
/dev/tenstorrent/N`) or Kubernetes DRA (`tt-dra-driver`). The state format here
is plain JSON so a similar agent daemon could read or adopt it.

## The allocation grain is derived, not assumed

UMD expands `TT_VISIBLE_DEVICES` to every chip on the same board when a board
holds two chips or fewer (`cluster_descriptor.cpp:
create_constrained_cluster_descriptor`). On a p300c, requesting one chip through
that constrained-device / visibility path may expose both chips on the board.

Direct TTNN device access can still open a single enumerated chip, so single-chip
work is not impossible — but `TT_VISIBLE_DEVICES`, the mechanism you would use to
fence a workload, does not fence below board granularity here. A single-chip
lease would be unenforceable, so gozer grants the board.

gozer applies the same predicate: **board grain for ≤2-chip boards, chip grain
otherwise.** So a QuietBox supports two concurrent tenants, a p150 host one per
card, and a Galaxy one per chip, with no configuration.

## State format

```
/tmp/tt-gozer/
  gate/<unit>.lock/lease.json    mkdir() is the atomic claim
  gate/<unit>.clean              unit was reset since last release
  leases/<lease_id>.json
  queue/<seq>-<ticket>.json      FIFO, zero-padded seq
  queue/.claim-window            ticket entitled to claim, 90s expiry
  .gatekeeper.lock/              allocator mutex, self-heals after 30s
```

All plain files. Inspect with `ls` and `cat`; recover by hand if you ever need to.

## Safety properties

- Never opens `/dev/tenstorrent/*`. Topology comes from
  `/sys/class/tenstorrent/`, busy-state from `os.readlink` on `/proc/*/fd`. So
  `gozer status` is safe during someone else's run.
- Resets by PCI BDF only, never by index, and never the board-wide M3/DMC path.
- Reaps a lease only when the owning process is gone *and* no fd remains.

## Testing

```bash
python3 -m pytest
```

No hardware required. `GOZER_ROOT`, `GOZER_SYSFS_ROOT`, `GOZER_PROC_ROOT` and
`GOZER_RESET_CMD` redirect every external dependency.
```

Create `CLAUDE.md`:

```markdown
# tt-gozer — project CLAUDE.md

Cooperative Tenstorrent chip leasing so multiple agents can share one box.
Gatekeeper guards the gate, keymaster carries the key, Gozer is the workload.

Design: `docs/superpowers/specs/2026-08-14-tt-gozer-design.md`
Plan: `docs/superpowers/plans/2026-08-14-tt-gozer-implementation.md`

## What happened

**Original request (2026-08-14):** "a set of claude skills we can strongly suggest
in ~/CLAUDE.md to 1) set chip-specific 'i am using this tenstorrent hardware now'
under /tmp, similar to socket lock files, and then 2) check if hardware is in use
with these and start work when things are available. We want to lightly queue
things." Plus: who holds and who asks both matter, agents should ask for *N*
chips without naming them, and non-Claude flows should be able to join.

Named in a follow-up: "The keymaster must meet the gatekeeper to allow the coming
of gozer."

## Key decisions and why

* **CLI + thin skills**, not logic inside skills. Race-sensitive code belongs in
  tested code, and non-Claude flows need the same entry point.
* **fd truth over bookkeeping.** The lease says who and why; only an open fd
  proves a chip is in use. Reaping needs the pid gone *and* no fd — the user's
  instruction was "no need to be greedy, make sure a process is completely done."
* **Grain is derived from UMD's own predicate**, not hardcoded, because the tool
  travels between machines.
* **Warn, don't block, on eth neighbours.** Whether a reset perturbs a neighbour
  is unresolved on hardware; the warning upgrades to a block without redesign.

## Corrections worth remembering

* `tt-smi -r` is **per-ASIC**, not per-board. Verified through `device_input.py`,
  `ChipReset.full_lds_reset`, per-ASIC PCIe root ports, and `blackhole.c`'s
  `pcie_timer_interrupt(pdev)`. Only `reset_m3`/`ASIC_DMC_RESET` is board-wide and
  the CLI does not expose it.
* Device **visibility** *is* board-granular, which is the real constraint. Found
  in `cluster_descriptor.cpp`.
* The p300c ethernet mesh is **hardwired inside the box** (user correction —
  earlier QuietBoxes needed external QSFP cabling). The `tt-hardware-primer`
  skill's claim that QB2 has no inter-chip Ethernet is **wrong** and should be
  fixed.

## Open

* Hardware spike: does resetting one ASIC perturb a live neighbour's eth links?
* Fix `tt-hardware-primer`.
* Revisit per-chip leases if two concurrent tenants proves too few.
```

- [ ] **Step 7: Run the whole suite and install for real**

Run: `cd ~/code/tt-gozer && python3 -m pytest -q && ./install.sh && gozer --version && gozer status`
Expected: all tests pass; installer prints two symlinks; `gozer 0.1.0`; status shows the real box.

- [ ] **Step 8: Commit**

```bash
cd ~/code/tt-gozer
git add skills install.sh README.md CLAUDE.md tests/test_install.py
git commit -m "feat: skills, installer, and docs

Installer links each skill directory individually because ~/.claude/skills
already holds ~30 unrelated skills; a test asserts a pre-existing skill survives.
Skill descriptions lead with the functional trigger, not the metaphor, so agent
matching works for someone who has never seen Ghostbusters."
```

---

### Task 11: tt-home wiring

**Files:**
- Modify: `~/tt-home/clone_my_repos.sh`, `~/tt-home/dotfiles/CLAUDE.md`, `~/tt-home/dotfiles/install.sh`, `~/tt-home/CLAUDE.md`

**Interfaces:**
- Consumes: the installed `gozer`.
- Produces: gozer travels to any machine provisioned from tt-home.

- [ ] **Step 1: Add the repo to the clone list**

In `~/tt-home/clone_my_repos.sh`, inside the `REPOS=(` array (around line 69), add:

```bash
    "tsingletaryTT/tt-gozer"
```

- [ ] **Step 2: Add the "lease first" rule to the global CLAUDE.md**

Append to `~/tt-home/dotfiles/CLAUDE.md`, after the "Working on Tenstorrent projects" section:

```markdown
## Tenstorrent hardware: always lease first

The box is shared between agents. **Before any command that opens
`/dev/tenstorrent/*`** — vLLM, tt-metal, TTNN, on-device pytest, `tt-smi -r` —
take a lease:

```bash
gozer run --chips 1 --who "claude:<skill>" --reason "<why>" -- <command>
```

or, for a lease spanning several commands, `gozer acquire --chips N --who ...`
then `gozer release <lease>` when done. Honour the `TT_VISIBLE_DEVICES` you are
given; on a 2-chip board asking for 1 chip correctly grants 2.

* Queued (exit 10)? Do non-hardware work, then `gozer wait <ticket>`.
* Contended or unclear how to ask → **gozer-keymaster** skill.
* Something stuck, or a chip busy with no lease → **gozer-gatekeeper** skill.

Never `tt-smi -r` by hand while others may be working — `gozer release` resets
exactly your chips. Repo: `~/code/tt-gozer`.
```

- [ ] **Step 3: Add the installer hook**

In `~/tt-home/dotfiles/install.sh`, after the `--- Claude MCP ---` section and before
the final separator, add:

```bash
# ── TT gozer ──────────────────────────────────────────────────────────────────
echo ""
echo "--- TT gozer (chip leasing) ---"
GOZER_INSTALLER="$HOME/code/tt-gozer/install.sh"
if [ -x "$GOZER_INSTALLER" ]; then
    $DRY_RUN || "$GOZER_INSTALLER"
    $DRY_RUN && echo "  would run $GOZER_INSTALLER"
else
    echo "  ⚠️  $GOZER_INSTALLER not found — run clone_my_repos.sh first"
fi
```

- [ ] **Step 4: Log it in the tt-home changelog**

Append to `~/tt-home/CLAUDE.md`:

```markdown
## tt-gozer chip leasing wired in (2026-08-14)

**User request:** Claude skills that claim Tenstorrent chips under `/tmp` like
socket lock files, check availability, and lightly queue requests — with who
holds and who asks both legible, and agents able to ask for "one chip" or "all
chips" without naming them.

Built as its own repo, `~/code/tt-gozer` (gatekeeper guards the gate, keymaster
carries the key, Gozer is the workload). tt-home carries only the transport:

* `clone_my_repos.sh` — added `tsingletaryTT/tt-gozer`.
* `dotfiles/CLAUDE.md` — added the "always lease first" rule, which is what
  actually makes the ~30 existing `tt-*` skills participate without editing any
  of them (most are upstream-managed and would lose the edit on sync).
* `dotfiles/install.sh` — `--- TT gozer ---` section runs the repo's installer.

Design and full hardware findings live in the tt-gozer repo, not here.
```

- [ ] **Step 5: Verify the wiring end to end**

Run:
```bash
bash -n ~/tt-home/dotfiles/install.sh && bash -n ~/tt-home/clone_my_repos.sh
grep -q tt-gozer ~/tt-home/clone_my_repos.sh && echo "clone list ok"
grep -q "lease first" ~/tt-home/dotfiles/CLAUDE.md && echo "rule ok"
gozer status >/dev/null && echo "gozer runs"
```
Expected: no syntax errors, three "ok" lines.

- [ ] **Step 6: Commit tt-home**

```bash
cd ~/tt-home
git add clone_my_repos.sh dotfiles/CLAUDE.md dotfiles/install.sh CLAUDE.md
git commit -m "feat: wire tt-gozer chip leasing into the tt-home transport

The 'always lease first' rule in the global CLAUDE.md is what makes the existing
tt-* skills participate without editing any of them -- most are upstream-managed
and would lose the change on next sync."
```

---

## Self-Review

**Spec coverage.** Every section of the design maps to a task: hardware findings →
Tasks 1, 7 (grain derivation, BDF-only reset); architecture/module layout → Tasks 1–9;
state layout → Task 3; allocation → Task 5; reconciliation → Task 4; queue → Task 6;
release → Tasks 7–8; skills and wiring → Tasks 10–11; testing → every task's test file,
with the spec's explicit coverage list distributed across Tasks 3–9; enforcement limits →
documented in Task 10's README. The spec's "Open items" stay open by design and are
recorded in Task 10's `CLAUDE.md`.

**Naming consistency.** `unit_key` (board serial at board grain, BDF at chip grain) is
used identically from Task 3 onward. `Grant.bdfs` / `Grant.dev_indices` / `Grant.units`
are consumed unchanged by Task 9. `reset_chips(bdfs, cmd, runner)` in Task 7 matches the
`self.reset_runner` injection point in Task 8. The six state strings are identical in
Tasks 4, 9, and the skills.

**Test counts.** Per-file counts are stated so a task can self-check. Whole-suite runs
just require everything green — exact totals drift as tasks compose and are not worth
failing a task over.

**Fixed during review:** the queue tests originally carried two versions of the
claim-window-expiry test (one monkeypatching `time.time`, which would also have affected
the code under test). Only the explicit-clock version, which backdates the marker file,
survives. And `cmd_renew` originally used a `claim / release / claim` chained expression
to rewrite a held lease; that is now `Gatekeeper.update_unit_lease`, added in Task 3 with
its own tests, which refuses to create a lock it did not find.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-08-14-tt-gozer-implementation.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
