"""The append-only history log: who held what, when, for how long.

Everything else in this tool is present-tense: once a lease is released or
reaped, every trace of it vanishes from `gate/` and `leases/`. There is no
way to answer "who had the chips two hours ago", "how long do leases
actually run", or "how often do we contend" -- which is the founding
requirement (*who holds the device* is legible) failing in the past tense.

`<GOZER_ROOT>/history.jsonl` fixes that: one JSON object per line, appended
forever, never rewritten or truncated by this module. Deliberately inside
GOZER_ROOT (default /tmp/tt-gozer), *not* a per-user path like
`~/.local/state`: the whole point is one shared timeline across every agent
on the box, and a per-user path would fragment it into as many logs as there
are users. It is lost on reboot along with the rest of /tmp -- that is a real
limitation, not a secret; do not "fix" it by moving the log somewhere
persistent without re-reading why GOZER_ROOT lives in /tmp in the first
place (see the design spec and gatekeeper.py's module docstring).

Concurrency: every write opens with O_APPEND and lands in exactly one
write() syscall. On Linux, a write() of at most PIPE_BUF (4096) bytes to a
file opened O_APPEND is atomic with respect to other writers -- the kernel
either places the whole write before the next writer's or after it, never
interleaved. That is the *only* thing standing between this file and garbled
half-lines from two concurrent agents logging at once, so it is load-bearing:
if a future change turns this into a `print()`, an `open(...).write()` plus a
second `.write("\n")`, or anything else that touches the fd more than once
per record, that guarantee is gone and two concurrent writers can interleave
a torn line into the log. Keep every record comfortably under 4096 bytes.

Every write is best-effort. A full disk, a permissions problem, or a missing
directory must never fail the caller -- especially not `release`, which has
already done its destructive work (the reset) by the time it logs. Every
OSError from open() or write() is swallowed here, silently, on purpose.
"""

from __future__ import annotations

import contextlib
import json
import os

from gozer.jsonstore import utcnow

FILENAME = "history.jsonl"


def _path(root: str) -> str:
    return os.path.join(root, FILENAME)


def log(root: str, event: str, **fields) -> None:
    """Append one event record. Never raises -- see the module docstring.

    `root` is a Gatekeeper's state root (GOZER_ROOT), not a full path: the
    caller doesn't need to know the log's filename, only where the gate's
    other state (`gate/`, `leases/`, `queue/`) lives, since this file sits
    alongside them.
    """
    record = {"ts": utcnow(), "event": event, **fields}
    # One json.dumps -> one encode -> one write(). Do not split this into
    # multiple write()s (see the module docstring on why that would
    # reintroduce interleaving), and do not print()/append via a buffered
    # `open(..., "a")` file object either -- that can issue more than one
    # underlying write() depending on buffering, which is exactly the same
    # defect by a different route.
    data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    with contextlib.suppress(OSError):
        fd = os.open(_path(root), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def read(root: str) -> list[dict]:
    """Return every parseable record, oldest first.

    A corrupt or partial line (e.g. from a crash mid-write, or hand-editing)
    is skipped rather than raising -- this is a diagnostic reader, and one
    bad line must not hide every record after it.
    """
    try:
        with open(_path(root)) as f:
            lines = f.readlines()
    except OSError:
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records
