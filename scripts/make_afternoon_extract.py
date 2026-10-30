#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Write the example-afternoon extract for the site from a gozer history log.

Read-only on the source. Run once; the output is committed.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from site_build.afternoon import extract  # noqa: E402

START, END = "2026-10-26T16:00:00Z", "2026-10-26T20:30:00Z"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default=os.path.expanduser("~/.local/state/tt-gozer/history.jsonl"))
    p.add_argument("--out", default=os.path.join(ROOT, "site", "data", "afternoon-2026-10-26.jsonl"))
    a = p.parse_args()
    with open(a.source) as fh:
        events = [json.loads(l) for l in fh if l.strip()]
    recs = extract(events, START, END)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    print(f"wrote {len(recs)} records to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
