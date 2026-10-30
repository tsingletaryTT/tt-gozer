#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Fail unless the given git tag is "v" + gozer.__version__.

Usage: check_tag.py <tag>        (the release workflow passes $GITHUB_REF_NAME)

Exit 0 and one line when the tag matches. Exit 1 with a message naming both
values when it does not. Exit 2 with usage when no tag is given.

The version is read the way bin/gozer reaches the package: the repo root goes
on sys.path and `gozer.__version__` is imported. Importing the package has no
side effects (gozer/__init__.py defines the version and nothing else).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from gozer import __version__  # noqa: E402


def main(argv):
    if len(argv) != 1:
        print("usage: check_tag.py <tag>", file=sys.stderr)
        return 2
    tag = argv[0]
    expected = "v" + __version__
    if tag != expected:
        print(
            f"error: tag {tag!r} does not match gozer.__version__ "
            f"{__version__!r}; the tag must be {expected!r}.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: tag {tag} matches gozer.__version__ {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
