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

BDF_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:(0[0-9a-fA-F]|1[0-9a-fA-F])\.[0-7]$")
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
