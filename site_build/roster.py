# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""The example roster and the list of projects that use gozer.

`roster()` totals each agent's leases, hold time and waiting from the example
afternoon's records. It is sorted by name. It is an example of what the log
records. It lists agents by name and does not order them by usage.

`load_users()` reads `site/data/users.json` and checks every entry.
"""
import json

from site_build.afternoon import walk_waits

USER_KEYS = {"name", "repo", "how", "evidence"}
HOW_MAX = 140


def roster(records: list[dict]) -> list[dict]:
    """One row per agent: leases, seconds held, longest lease, seconds waited. Sorted by `who`."""
    recs = sorted(records, key=lambda r: r["ts"])
    rows: dict[str, dict] = {}

    def row(who: str) -> dict:
        return rows.setdefault(who, {"who": who, "leases": 0, "held_s": 0,
                                     "longest_s": 0, "waited_s": 0})

    for r in recs:
        if r["event"] == "granted":
            row(r["who"])["leases"] += 1
        elif r["event"] == "released" and "duration_s" in r:
            x = row(r["who"])
            x["held_s"] += r["duration_s"]
            x["longest_s"] = max(x["longest_s"], r["duration_s"])
        elif r["event"] == "queued":
            row(r["who"])
    waits, _ = walk_waits(recs)
    for w in waits:
        row(w["who"])["waited_s"] += w["seconds"]
    return sorted(rows.values(), key=lambda x: x["who"])


def _check(i: int, u) -> None:
    """Raise ValueError naming entry `i` and the problem."""
    def bad(msg: str):
        raise ValueError(f"users entry {i}: {msg}")

    if not isinstance(u, dict):
        bad("not an object")
    missing = USER_KEYS - set(u)
    if missing:
        bad(f"missing key(s) {sorted(missing)}")
    extra = set(u) - USER_KEYS
    if extra:
        bad(f"extra key(s) {sorted(extra)}")
    for k in USER_KEYS:
        if not isinstance(u[k], str) or not u[k].strip():
            bad(f"{k} must be a non-empty string")
    if not u["repo"].startswith("https://github.com/") or u["repo"].endswith(".git"):
        bad("repo must be an https://github.com/ URL without .git")
    if len(u["how"]) > HOW_MAX:
        bad(f"how is longer than {HOW_MAX} characters")
    if "\n" in u["how"]:
        bad("how contains a newline")
    p = u["evidence"]
    if p.startswith("/") or ".." in p.split("/"):
        bad("evidence must be a relative path inside the repo (no leading slash, no ..)")


def load_users(path: str) -> list[dict]:
    """Read and validate the users list. Raises ValueError on a malformed entry."""
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("users file must hold a list")
    for i, u in enumerate(data):
        _check(i, u)
    return data
