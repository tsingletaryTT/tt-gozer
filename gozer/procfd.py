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

import json
import os
import shlex
import subprocess
import sys

DEV_PREFIX = "/dev/tenstorrent/"
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


def holders(proc_root: str = "/proc", use_sudo: bool = False,
            runner=None) -> dict[int, list[int]]:
    """Map device index -> sorted pids holding it open.

    With use_sudo, merge in an elevated scan so other users' processes are
    visible too. The unprivileged scan still runs first, so an unavailable
    sudo degrades to same-user truth instead of to an empty result.
    """
    found: dict[int, set[int]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        entries = []

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
            # isdigit() accepts non-canonical forms like "007", but kernel-generated paths are well-formed.
            if suffix.isdigit():
                found.setdefault(int(suffix), set()).add(pid)

    result = {dev: sorted(pids) for dev, pids in sorted(found.items())}

    if use_sudo:
        for dev, pids in _sudo_holders(
                proc_root, **({"runner": runner} if runner else {})).items():
            result[dev] = sorted(set(result.get(dev, [])) | set(pids))
    return dict(sorted(result.items()))


def pid_alive(pid: int, proc_root: str = "/proc") -> bool:
    return os.path.isdir(os.path.join(proc_root, str(pid)))


def process_name(pid: int, proc_root: str = "/proc") -> str:
    try:
        with open(os.path.join(proc_root, str(pid), "comm")) as f:
            return f.read().strip()
    except OSError:
        return ""


if __name__ == "__main__":
    # Invoked as `sudo python3 procfd.py --scan /proc` by _sudo_holders above.
    # Prints the same mapping as holders(), as JSON, for the parent to merge.
    import sys as _sys
    if len(_sys.argv) == 3 and _sys.argv[1] == "--scan":
        print(json.dumps({str(k): v for k, v in holders(_sys.argv[2]).items()}))
    else:
        _sys.exit(2)
