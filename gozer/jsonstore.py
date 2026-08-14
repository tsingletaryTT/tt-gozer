"""Small state-file helpers shared by gatekeeper.py and queue.py.

Lives in its own module, rather than in either of those two, so neither has
to import the other to get at it. gatekeeper.py composes queue.py (never the
reverse), so if these lived in gatekeeper.py, queue.py importing them would
create an import cycle. A third, dependency-free home lets both import the
same functions instead of each keeping its own copy.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _elapsed_seconds(since: str, now: str) -> float:
    """Seconds between two utcnow()-formatted timestamps.

    Both arguments are the fixed TIMESTAMP_FORMAT every lease and ticket
    timestamp in this codebase uses, so a plain strptime-and-subtract is
    exact -- no timezone-library dependency needed.

    A timestamp that will not parse returns inf ("maximally old"), matching
    how a *missing* timestamp is already treated by every caller. It must not
    raise: state files are hand-editable and world-writable, and cli.main
    turns a stray ValueError into exit 12, documented as "unavailable and
    queueing disabled" -- so one corrupt `since` field would tell every agent
    on the box that the hardware is busy, and they would retry forever
    instead of surfacing a corrupt state file. Reporting a corrupt record as
    ancient hands it to the ordinary stale path, where it gets reaped and
    logged as what it is.
    """
    try:
        return (datetime.strptime(now, TIMESTAMP_FORMAT)
                - datetime.strptime(since, TIMESTAMP_FORMAT)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def _atomic_write_json(path: str, payload: dict, tmp: str | None = None) -> None:
    """Write via temp file + rename so a reader never sees a partial record.

    `tmp` overrides where the temporary file is staged. It defaults to a
    sibling of `path`, which is what keeps the rename atomic (same
    filesystem) -- but a caller writing *into* a directory that another
    process may be removing needs the temp file to live outside that
    directory, or its presence makes the concurrent `rmdir` fail with
    ENOTEMPTY. See Gatekeeper.update_unit_lease. Whatever is passed must
    still be on the same filesystem as `path`.
    """
    tmp = tmp or f"{path}.tmp.{os.getpid()}"
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
