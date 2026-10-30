# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""The site's terminal sessions come from the real CLI, deterministically."""
import contextlib
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
        for pid in real_pids - {"4121", "4187", "4203"}:
            assert not re.search(rf"pid {pid}\b", text), (pid, text)


def _agent_pids(output):
    """{agent: set of pids} from status lines of the form '<who> pid <N>'."""
    found = {}
    for who, pid in re.findall(r"(\S+) pid (\d+)", output):
        if who != "owner":
            found.setdefault(who, set()).add(pid)
    return found


def test_each_agent_shows_its_own_pid_in_status():
    # In-process, every command shares one real pid; on a real box each
    # agent's process has its own. The page must show them as distinct.
    steps = {s.note_key: s for s in run_session()}
    after = _agent_pids(steps["status-after"].output)
    full = _agent_pids(steps["status-full"].output)
    assert after == {"claude:sdxl": {"4203"}, "claude:eval": {"4187"}}
    assert full == {"claude:bench": {"4121"}, "claude:eval": {"4187"}}
    pids = {p for d in (full, after) for s in d.values() for p in s}
    assert pids == {"4121", "4187", "4203"}
    # the owner line still carries the owner pid as before
    assert "owner pid 4121" in steps["first-grant"].output


def test_agent_pids_are_identical_across_two_runs():
    a = [s.output for s in run_session()]
    b = [s.output for s in run_session()]
    assert a == b


def test_timestamps_do_not_depend_on_wall_clock_coincidence():
    same = "since 2026-01-01T10:00:00Z\nsince 2026-01-01T10:00:00Z"
    apart = "since 2026-01-01T10:00:00Z\nsince 2026-01-01T10:00:01Z"
    assert normalise(same) == normalise(apart)


def test_first_command_shows_a_copyable_owner_pid():
    cmd = run_session()[0].command
    assert "--owner-pid $$" in cmd
    assert not re.search(r"--owner-pid\s+\d", cmd)
    assert "owner pid 4121" in run_session()[0].output


def _inside(path, base):
    """True when path is base or lies under it (symlinks resolved)."""
    path, base = os.path.realpath(path), os.path.realpath(base)
    return os.path.commonpath([path, base]) == base


def _gatekeeper_roots(monkeypatch):
    """Record the four paths of every Gatekeeper the code under test builds."""
    from gozer.gatekeeper import Gatekeeper
    seen = []
    real_init = Gatekeeper.__init__

    def spy(self, *a, **kw):
        real_init(self, *a, **kw)
        seen.append((self.root, self.history_root, self.sysfs_root, self.proc_root))

    monkeypatch.setattr(Gatekeeper, "__init__", spy)
    return seen


def test_the_session_only_reaches_its_own_temp_directory(monkeypatch):
    seen = _gatekeeper_roots(monkeypatch)
    tmps = []
    real_box = transcripts._fake_box

    @contextlib.contextmanager
    def box():
        with real_box() as tmp:
            tmps.append(tmp)
            yield tmp

    monkeypatch.setattr(transcripts, "_fake_box", box)
    run_session()
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    live = ["/tmp/tt-gozer", os.path.join(real_home, ".local/state/tt-gozer")]
    assert seen and len(tmps) == 1
    for paths in seen:
        for p in paths:
            assert p and _inside(p, tmps[0]), p
            assert not any(_inside(p, d) or _inside(d, p) for d in live), p


def test_the_containment_helper_can_reject(tmp_path, monkeypatch):
    # A Gatekeeper built with no GOZER_* variables lands on the default root.
    # Point that default at a stand-in so nothing is created under /tmp/tt-gozer.
    import gozer.gatekeeper as gk_mod
    stand_in = tmp_path / "default-root"
    monkeypatch.setattr(gk_mod, "DEFAULT_ROOT", str(stand_in))
    for k in [k for k in os.environ if k.startswith("GOZER_")]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seen = _gatekeeper_roots(monkeypatch)
    gk_mod.Gatekeeper()
    session_tmp = tmp_path / "session"
    session_tmp.mkdir()
    assert seen and not _inside(seen[0][0], session_tmp)
    assert _inside(seen[0][0], stand_in)


def test_the_current_user_name_never_appears(monkeypatch):
    real_box = transcripts._fake_box

    @contextlib.contextmanager
    def box():
        # The session scrubs USER/LOGNAME on entry; plant a marker first.
        monkeypatch.setenv("USER", "zz-test-user")
        monkeypatch.setenv("LOGNAME", "zz-test-user")
        with real_box() as tmp:
            yield tmp

    monkeypatch.setattr(transcripts, "_fake_box", box)
    for s in run_session():
        for text in (s.command, s.output):
            assert "zz-test-user" not in text
            assert "ttuser" not in text
