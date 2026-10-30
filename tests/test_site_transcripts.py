# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""The site's terminal sessions come from the real CLI, deterministically."""
import os
import re

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


def test_no_real_ids_or_times_survive():
    for s in run_session():
        text = s.command + "\n" + s.output
        assert not re.search(r"20\d\d-\d\d-\d\dT", text.replace("2026-10-26T", ""))
        for found in re.findall(r"\b[0-9a-f]{6}\b", text):
            assert found in FIXED_LEASES, (found, text)
        assert "826205" not in text


def test_the_real_state_directories_are_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    dirs = ["/tmp/tt-gozer", os.path.expanduser("~/.local/state/tt-gozer"),
            os.path.join(os.environ.get("REAL_HOME", "/home/ttuser"),
                         ".local/state/tt-gozer")]
    def snap():
        return {d: sorted(os.listdir(d)) for d in dirs if os.path.isdir(d)}
    before = snap()
    run_session()
    assert snap() == before
