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


QUIETBOX = [
    {"dev_index": 0, "bdf": "0000:01:00.0", "serial": "0000000000000001",
     "asic_id": "1111111111111111", "card": "p300c"},
    {"dev_index": 1, "bdf": "0000:02:00.0", "serial": "0000000000000001",
     "asic_id": "2222222222222222", "card": "p300c"},
    {"dev_index": 2, "bdf": "0000:03:00.0", "serial": "0000000000000002",
     "asic_id": "3333333333333333", "card": "p300c"},
    {"dev_index": 3, "bdf": "0000:04:00.0", "serial": "0000000000000002",
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
