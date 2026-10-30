#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Build the explainer page into docs/index.html, or check it is current.

    python scripts/build_site.py               # write docs/index.html
    python scripts/build_site.py --out PATH    # write somewhere else
    python scripts/build_site.py --check       # exit 1 and print a diff if docs/index.html is stale

With --check, --out names the file to compare against (default docs/index.html);
nothing is written there.
"""
import argparse
import difflib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

from site_build.build import build  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "docs", "index.html")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=DEFAULT_OUT, help="page to write, or to compare with --check")
    p.add_argument("--check", action="store_true",
                   help="build to a temporary file and compare it byte for byte with --out")
    a = p.parse_args(argv)

    if not a.check:
        build(a.out)
        print(f"wrote {a.out}")
        return 0

    with tempfile.TemporaryDirectory(prefix="gozer-site-check-") as tmp:
        fresh = os.path.join(tmp, "index.html")
        build(fresh)
        with open(fresh, "rb") as fh:
            new = fh.read()
    try:
        with open(a.out, "rb") as fh:
            old = fh.read()
    except FileNotFoundError:
        old = b""
    if old == new:
        print(f"{a.out} is current")
        return 0
    diff = difflib.unified_diff(
        old.decode("utf-8", "replace").splitlines(keepends=True),
        new.decode("utf-8", "replace").splitlines(keepends=True),
        fromfile=a.out, tofile="fresh build")
    sys.stdout.writelines(diff)
    print(f"\n{a.out} is out of date: run python scripts/build_site.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
