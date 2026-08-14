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
