# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""The site's terminal sessions come from the real CLI, deterministically."""
import os
import pwd
import re

from site_build import transcripts
from site_build.transcripts import run_session, normalise

FIXED_LEASES = {"0d219a", "f2ae86", "7b31c4"}


def test_session_has_the_three_acquire_outcomes():
    steps = run_session()
    assert [s.exit_code for s in steps[:3]] == [0, 0, 10]
    assert "queued: ticket" in steps[2].output
    assert [s.note_key for s in steps] == [
        "first-grant", "second-grant", "queued", "status-full",
        "release", "wait-grant", "status-after", "release-last"]


def test_wait_grants_the_queued_agent_after_the_release():
    wait = run_session()[5]
    assert wait.command.startswith("gozer wait")
    assert wait.exit_code == 0
    assert "granted" in wait.output


def test_output_is_identical_across_two_runs():
    assert run_session() == run_session()


def test_no_real_ids_or_times_survive(monkeypatch):
    # Capture the real, un-normalised output so the assertions compare
    # against what the CLI really printed.
    raw = []
    real_run = transcripts._run

    def spy(argv):
        code, out = real_run(argv)
        raw.append(out)
        return code, out

    monkeypatch.setattr(transcripts, "_run", spy)
    steps = run_session()
    raw_text = "\n".join(raw)
    real_leases = set(re.findall(r"lease ([0-9a-f]{6})", raw_text))
    real_tickets = set(re.findall(r"ticket ([0-9a-f]{4})", raw_text))
    real_pids = set(re.findall(r"pid (\d+)", raw_text))
    assert real_leases and real_tickets and real_pids  # the spy saw real data
    for s in steps:
        text = s.command + "\n" + s.output
        assert not re.search(r"20\d\d-\d\d-\d\dT", text.replace("2026-10-26T", ""))
        for found in re.findall(r"\b[0-9a-f]{6}\b", text):
            assert found in FIXED_LEASES, (found, text)
        for t in re.findall(r"(?:ticket|wait) ([0-9a-f]{4})\b", text):
            assert t == "8fce", (t, text)
        for pid in real_pids - {"4121", "4187", "4260"}:
            assert not re.search(rf"pid {pid}\b", text), (pid, text)


def test_timestamps_do_not_depend_on_wall_clock_coincidence():
    same = "since 2026-01-01T10:00:00Z\nsince 2026-01-01T10:00:00Z"
    apart = "since 2026-01-01T10:00:00Z\nsince 2026-01-01T10:00:01Z"
    assert normalise(same) == normalise(apart)


def test_first_command_shows_a_copyable_owner_pid():
    cmd = run_session()[0].command
    assert "--owner-pid $$" in cmd
    assert not re.search(r"--owner-pid\s+\d", cmd)
    assert "owner pid 4121" in run_session()[0].output


def _snapshot(*roots):
    """Recursive (relative path, size, mtime_ns) for every file under roots."""
    snap = {}
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            for name in dirs + files:
                p = os.path.join(dirpath, name)
                st = os.lstat(p)
                snap[p] = (st.st_size, st.st_mtime_ns)
    return snap


def test_the_snapshot_helper_detects_a_write(tmp_path):
    d = tmp_path / "tt-gozer"
    d.mkdir()
    (d / "history.jsonl").write_text("a\n")
    before = _snapshot(d)
    (d / "history.jsonl").write_text("a\nb\n")
    assert _snapshot(d) != before


def test_the_real_state_directories_are_untouched(tmp_path, monkeypatch):
    real_home = pwd.getpwuid(os.getuid()).pw_dir   # not $HOME: the test resets it
    dirs = ["/tmp/tt-gozer", os.path.join(real_home, ".local/state/tt-gozer")]
    monkeypatch.setenv("HOME", str(tmp_path))
    before = _snapshot(*[d for d in dirs if os.path.isdir(d)])
    run_session()
    assert _snapshot(*[d for d in dirs if os.path.isdir(d)]) == before
