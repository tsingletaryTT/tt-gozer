# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Terminal sessions for the site, produced by the real gozer CLI.

`run_session()` calls `gozer.cli.main` in-process against a fake two-board
box (sysfs and /proc built in a temporary directory, reset command stubbed
with /bin/true), so the real tt-smi and the real state directories are never
touched. Every output is the CLI's own text; `normalise()` only replaces ids,
pids and timestamps with fixed values so two builds produce identical pages.
"""
import contextlib
import io
import os
import re
import shlex
import tempfile
from dataclasses import dataclass

from gozer.cli import main


@dataclass
class Step:
    command: str      # as typed on the page, e.g. "gozer status"
    output: str       # normalised CLI output
    exit_code: int
    note_key: str     # which prose note the page attaches to this step


# Same shape as tests/conftest.py: two p300c boards of two chips each. The
# board serials are ordered opposite to the device indices on purpose.
QUIETBOX = [
    {"dev_index": 0, "bdf": "0000:01:00.0", "serial": "0000000000000002",
     "asic_id": "1111111111111111", "card": "p300c"},
    {"dev_index": 1, "bdf": "0000:02:00.0", "serial": "0000000000000002",
     "asic_id": "2222222222222222", "card": "p300c"},
    {"dev_index": 2, "bdf": "0000:03:00.0", "serial": "0000000000000001",
     "asic_id": "3333333333333333", "card": "p300c"},
    {"dev_index": 3, "bdf": "0000:04:00.0", "serial": "0000000000000001",
     "asic_id": "4444444444444444", "card": "p300c"},
]


def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(value + "\n")


def build_sysfs(root, chips):
    """Fake /sys/class/tenstorrent tree, laid out as the kernel does."""
    for c in chips:
        base = os.path.join(root, "class", "tenstorrent", f"tenstorrent!{c['dev_index']}")
        _write(os.path.join(base, "tt_serial"), c["serial"])
        _write(os.path.join(base, "tt_asic_id"), c["asic_id"])
        _write(os.path.join(base, "tt_card_type"), c["card"])
        _write(os.path.join(base, "tt_heartbeat"), "12345")
        pci = os.path.join(root, "bus", "pci", "devices", c["bdf"])
        os.makedirs(pci, exist_ok=True)
        link = os.path.join(base, "device")
        if not os.path.lexists(link):
            os.symlink(pci, link)
    return os.path.join(root, "class", "tenstorrent")


@contextlib.contextmanager
def _fake_box():
    """Temp state/sysfs/proc dirs and a scrubbed environment; restored on exit."""
    saved = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="gozer-site-") as tmp:
        try:
            for k in [k for k in os.environ if k.startswith("GOZER_")]:
                del os.environ[k]
            proc = os.path.join(tmp, "proc")
            os.makedirs(os.path.join(proc, "1", "fd"))
            _write(os.path.join(proc, "1", "comm"), "python")
            os.makedirs(os.path.join(tmp, "home"))
            os.environ.update({
                "HOME": os.path.join(tmp, "home"),
                "GOZER_ROOT": os.path.join(tmp, "state"),
                "GOZER_HISTORY_ROOT": os.path.join(tmp, "history"),
                "GOZER_SYSFS_ROOT": build_sysfs(os.path.join(tmp, "sys"), QUIETBOX),
                "GOZER_PROC_ROOT": proc,
                "GOZER_RESET_CMD": "/bin/true",   # stub; real tt-smi never runs
            })
            yield tmp
        finally:
            os.environ.clear()
            os.environ.update(saved)


def _run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


# ----- normalisation -------------------------------------------------------

_LEASE_FIXED = ["0d219a", "f2ae86", "7b31c4"]
_TICKET_FIXED = ["8fce", "a3d1", "c9e7"]
_PID_FIXED = [4121, 4187, 4260]
_TS_RE = re.compile(r"\b20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ")


class _Mapper:
    """Maps each distinct original value to the next fixed one, in order."""
    def __init__(self, fixed):
        self.fixed, self.seen = fixed, {}

    def __call__(self, value):
        if value not in self.seen:
            self.seen[value] = str(self.fixed[len(self.seen)])
        return self.seen[value]


class _Session:
    def __init__(self):
        self.lease = _Mapper(_LEASE_FIXED)
        self.ticket = _Mapper(_TICKET_FIXED)
        self.pid = _Mapper(_PID_FIXED)
        self.times = {}

    def ts(self, value):
        if value not in self.times:
            # one minute apart, in order of first appearance
            n = len(self.times)
            self.times[value] = f"2026-10-26T16:{n:02d}:00Z"
        return self.times[value]


_ACTIVE = None


def normalise(text, _s=None):
    """Replace lease ids, ticket ids, pids and timestamps with fixed values.

    The same original value maps to the same fixed value within one session.
    """
    s = _s or _ACTIVE or _Session()
    text = _TS_RE.sub(lambda m: s.ts(m.group(0)), text)
    text = re.sub(r"(?<=lease )[0-9a-f]{6}\b", lambda m: s.lease(m.group(0)), text)
    # a ticket id appears as "ticket X" and as the argument of "gozer wait X"
    text = re.sub(r"\b(ticket|wait) ([0-9a-f]{4})\b",
                  lambda m: m.group(1) + " " + s.ticket(m.group(2)), text)
    text = re.sub(r"(?<=pid )\d+", lambda m: s.pid(m.group(0)), text)
    return text


# ----- the session ---------------------------------------------------------

def run_session():
    """Run the walkthrough through the real CLI and return its steps."""
    global _ACTIVE
    with _fake_box():
        _ACTIVE = _Session()
        try:
            return _script()
        finally:
            _ACTIVE = None


def _script():
    steps = []

    def step(argv, note_key):
        code, out = _run(argv)
        steps.append(Step(
            command=normalise("gozer " + " ".join(shlex.quote(a) for a in argv)),
            output=normalise(out).rstrip("\n"), exit_code=code, note_key=note_key))
        return out

    def find(pattern, text):
        m = re.search(pattern, text)
        assert m, (pattern, text)
        return m.group(1)

    out = step(["acquire", "--chips", "2", "--who", "claude:bench", "--reason",
                "llama decode benchmark", "--owner-pid", "1"], "first-grant")
    first = find(r"lease ([0-9a-f]{6})", out)
    step(["acquire", "--chips", "2", "--who", "claude:eval", "--reason",
          "accuracy eval"], "second-grant")
    out = step(["acquire", "--chips", "2", "--who", "claude:sdxl", "--reason",
                "image batch"], "queued")
    ticket = find(r"ticket ([0-9a-f]+)", out)
    step(["status"], "status-full")
    step(["release", first], "release")
    out = step(["wait", ticket, "--timeout", "5s"], "wait-grant")
    queued_lease = find(r"lease ([0-9a-f]{6})", out)
    step(["status"], "status-after")
    step(["release", queued_lease], "release-last")
    return steps
