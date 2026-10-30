# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""scripts/check_tag.py: the release workflow's tag-versus-version gate.

The script is run as a subprocess, the way the workflow runs it, so the exit
codes and messages tested here are the ones the workflow sees.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_tag.py"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd="/",
    )


def _version():
    sys.path.insert(0, str(ROOT))
    try:
        from gozer import __version__
    finally:
        sys.path.remove(str(ROOT))
    return __version__


def test_check_tag_accepts_matching_and_rejects_mismatch():
    v = _version()

    ok = _run("v" + v)
    assert ok.returncode == 0, ok.stderr
    assert "v" + v in ok.stdout

    wrong = _run("v9.9.9")
    assert wrong.returncode == 1
    assert "v9.9.9" in wrong.stderr and "v" + v in wrong.stderr

    noprefix = _run(v)
    assert noprefix.returncode == 1
    assert v in noprefix.stderr and "v" + v in noprefix.stderr

    missing = _run()
    assert missing.returncode == 2
    assert "usage" in missing.stderr.lower()
