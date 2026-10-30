# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
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


@dataclass(frozen=True)
class Topology:
    """The readable boards, plus the chips set aside because they cannot be
    placed on a board."""
    boards: list[Board]
    unreadable: list[Chip] = field(default_factory=list)


# How many chips each board type carries. Used only to notice a board that has
# lost a chip: sysfs cannot say that a lone chip once had a partner, and UMD
# would expand a grant of it to the missing one. A card type not listed here
# has no expectation, so it is never called short.
EXPECTED_CHIPS_PER_BOARD = {
    "p300a": 2, "p300b": 2, "p300c": 2,
    "p150a": 1, "p150b": 1, "p150c": 1,
}


def short_boards(boards: list[Board]) -> dict[str, tuple[int, int]]:
    """Boards with fewer readable chips than their card type has:
    serial -> (readable, expected)."""
    out = {}
    for b in boards:
        want = EXPECTED_CHIPS_PER_BOARD.get(b.card)
        if want is not None and len(b.chips) < want:
            out[b.serial] = (len(b.chips), want)
    return out


def is_dead_serial(serial: str) -> bool:
    """True for a serial made only of ones: 0xffffffff, in any spelling.

    A chip that has fallen off the bus (a failed reset, a killed hung run)
    answers every register read with all ones, so its tt_serial arrives as
    FFFFFFFFFFFFFFFF. That is not an empty string, but it is no more a board
    serial: every dead chip reads the same, so they would all group together,
    and the real board partner of each would be left a board of one.
    """
    digits = serial[2:] if serial[:2] in ("0x", "0X") else serial
    return bool(digits) and all(ch in "fF" for ch in digits)


def read_topology(sysfs_root: str | None = None) -> list[Board]:
    """The readable boards of `read_topology_full`."""
    return read_topology_full(sysfs_root).boards


def read_topology_full(sysfs_root: str | None = None) -> Topology:
    """Enumerate chips and group them into boards by tt_serial.

    Boards are sorted by serial, chips within a board by device index, so output
    is deterministic and tests can assert on order.

    A chip whose serial is all ones (see `is_dead_serial`) is returned in
    `unreadable` and takes no part in boards or the lease grain, so nothing can
    be granted it. An empty or missing serial is still an error, below.
    """
    root = sysfs_root or os.environ.get("GOZER_SYSFS_ROOT") or DEFAULT_SYSFS_ROOT
    if not os.path.isdir(root):
        raise TopologyError(f"no Tenstorrent sysfs tree at {root}")

    chips: list[Chip] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError as e:
        raise TopologyError(f"cannot read Tenstorrent sysfs tree at {root}: {e}") from e
    for entry in entries:
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

    # tt_serial is the one attribute the whole allocation model rests on: it
    # groups chips into boards, and the board sizes are what lease_grain reads
    # to decide board-vs-chip grain. _read_attr swallows OSError and returns
    # "" -- correct for tt_aiclk and friends, where a missing value costs
    # nothing but a blank in a display -- but here it would group every chip
    # into a single board with serial "", flip the grain to "chip", and have
    # gozer hand out half a p300c while UMD still gives the tenant both chips.
    # That is precisely the unenforceable lease the README promises never
    # happens, so refuse (exit 14) rather than degrade into it.
    no_serial = [c.bdf for c in chips if not c.serial]
    if no_serial:
        raise TopologyError(
            f"unreadable tt_serial for {', '.join(sorted(no_serial))} under "
            f"{root} -- board serials are what determine the lease grain, so "
            "gozer cannot safely decide what to hand out; check the driver "
            "(tt-kmd) and permissions on these sysfs attributes")

    dead = sorted((c for c in chips if is_dead_serial(c.serial)),
                  key=lambda c: c.dev_index)
    chips = [c for c in chips if not is_dead_serial(c.serial)]
    if not chips:
        raise TopologyError(
            f"every Tenstorrent chip under {root} reads an all-ones tt_serial "
            "(the chips are not responding); nothing can be leased")

    by_serial: dict[str, list[Chip]] = {}
    for c in chips:
        by_serial.setdefault(c.serial, []).append(c)

    return Topology(
        boards=[
            Board(serial=serial,
                  card=members[0].card,
                  chips=sorted(members, key=lambda c: c.dev_index))
            for serial, members in sorted(by_serial.items())
        ],
        unreadable=dead)


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
