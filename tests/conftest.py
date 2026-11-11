# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
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
# indices they hold — the board with the lower-indexed chips (0,1) gets the
# lexicographically *larger* serial, and vice versa. This disagreement is load
# bearing: `_unit_sort_key` orders candidates by lowest device index, while a
# board's unit key (its serial) is what `all_units()`/`free_units()` sort by
# default iteration order comes from. If the serials instead agreed with device
# order, a test asserting "the lower-indexed board wins" would pass even if the
# device-index tiebreak were deleted and selection silently fell back to
# serial order — the test would look green but prove nothing. Keep the
# disagreement: do NOT "tidy" these back into ascending order to match dev
# index, or several tests (see test_prefers_the_lower_indexed_board_for_determinism
# in test_allocate.py) silently stop discriminating between the two orderings.
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


def fake_proc_tree(tmp_path, procs):
    """Build a fake /proc. procs: {pid: {"ppid": int, "devs": [device_index, ...]}}.

    Writes what gozer reads: fd/ symlinks to /dev/tenstorrent/<N> (the device
    scan), comm (the process name) and status (the parent pid, for the
    ownership walk). A pid with no entry does not exist, which is how a test
    models a process that has exited.
    """
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid, spec in procs.items():
        d = root / str(pid)
        (d / "fd").mkdir(parents=True, exist_ok=True)
        (d / "comm").write_text("python\n")
        (d / "status").write_text(f"Name:\tpython\nPPid:\t{spec.get('ppid', 1)}\n")
        for i, dev in enumerate(spec.get("devs", [])):
            os.symlink(f"/dev/tenstorrent/{dev}", d / "fd" / str(i + 3))
    return str(root)


@pytest.fixture(autouse=True)
def no_real_proc(monkeypatch):
    """Fail a test that scans the real /proc.

    A Gatekeeper built without a proc_root scans the real /proc for open
    Tenstorrent devices. On a shared box other agents hold devices open, so
    the chips look busy and the test's own acquire queues instead of
    granting. The test then passes or fails depending on what else is running.
    Raising here turns that silent dependence into an immediate, named error.
    """
    from gozer import procfd
    real = procfd.holders

    def guarded(proc_root="/proc", *args, **kwargs):
        # Compare the resolved path, because "/proc/", "//proc" and "/proc/../proc"
        # all name the real /proc. (normpath keeps a leading "//", so it
        # would miss that one.)
        if os.path.realpath(proc_root) == "/proc":
            raise AssertionError(
                "this test scans the real /proc; give the Gatekeeper a fake "
                "proc_root (see fake_proc_tree in conftest.py). Pass it as "
                "proc_root= on Gatekeeper, or set the GOZER_PROC_ROOT "
                "environment variable for CLI tests")
        return real(proc_root, *args, **kwargs)

    monkeypatch.setattr(procfd, "holders", guarded)


@pytest.fixture(autouse=True)
def no_real_reset(monkeypatch):
    """Fail a test that would run the real `tt-smi`.

    The fake QUIETBOX uses the same PCI addresses as a real QuietBox, so a
    test that releases a lease without faking the reset (a
    `km.reset_runner`, or `GOZER_RESET_CMD`) runs `tt-smi -r` on the real
    chips with those addresses. On 2026-11-11 that reset a live model
    server's board. Refuse any `tt-smi` argv before it can start, and drop a
    `GOZER_RESET_CMD` inherited from the developer's shell so a test sees
    only what it sets itself.
    """
    import subprocess
    real_run = subprocess.run
    attempts = []

    def guarded(argv, *args, **kwargs):
        first = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
        if isinstance(first, str) and os.path.basename(first) == "tt-smi":
            attempts.append(argv)
            raise AssertionError(
                f"this test runs the real tt-smi ({argv!r}); fake the reset "
                "with km.reset_runner or GOZER_RESET_CMD=/bin/true")
        return real_run(argv, *args, **kwargs)

    monkeypatch.delenv("GOZER_RESET_CMD", raising=False)
    monkeypatch.setattr(subprocess, "run", guarded)
    yield
    # release() catches a failed reset by design, so the raise above can be
    # swallowed; fail the test here regardless.
    if attempts:
        pytest.fail(f"test tried to run the real tt-smi: {attempts!r}")


@pytest.fixture(autouse=True)
def no_real_names_file(monkeypatch, tmp_path):
    """Point GOZER_NAMES at a path that does not exist.

    Without this the default is the real ~/.config/gozer/names.toml, so a test
    would pass or fail depending on whether the person running it has named
    their boards. A test that wants a names file sets GOZER_NAMES itself.
    """
    monkeypatch.setenv("GOZER_NAMES", str(tmp_path / "no-names-file.toml"))
