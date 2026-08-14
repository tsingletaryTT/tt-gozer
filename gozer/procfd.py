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
            # isdigit() accepts non-canonical forms like "007", but kernel-generated paths are well-formed.
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
