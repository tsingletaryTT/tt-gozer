import os
import pytest
from gozer.procfd import holders, pid_alive, process_name


def fake_proc(tmp_path, pids):
    """Build a fake /proc. pids: {pid: (comm, [device_index, ...])}."""
    root = tmp_path / "proc"
    for pid, (comm, devs) in pids.items():
        d = root / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        for i, dev in enumerate(devs):
            os.symlink(f"/dev/tenstorrent/{dev}", d / "fd" / str(i + 3))
    return str(root)


def test_finds_pids_holding_devices(tmp_path):
    p = fake_proc(tmp_path, {861926: ("python", [0, 1, 2, 3])})
    assert holders(p) == {0: [861926], 1: [861926], 2: [861926], 3: [861926]}


def test_multiple_holders_of_one_device_are_all_listed(tmp_path):
    p = fake_proc(tmp_path, {100: ("a", [2]), 200: ("b", [2])})
    assert holders(p) == {2: [100, 200]}


def test_ignores_non_tenstorrent_fds_and_dead_links(tmp_path):
    root = tmp_path / "proc" / "500" / "fd"
    root.mkdir(parents=True)
    (tmp_path / "proc" / "500" / "comm").write_text("sh\n")
    os.symlink("/dev/null", root / "3")
    os.symlink("/nonexistent/thing", root / "4")
    assert holders(str(tmp_path / "proc")) == {}


def test_ignores_unreadable_pid_dirs(tmp_path):
    # Another user's process: /proc/<pid>/fd exists but is not listable.
    # We must skip it, not crash. This is the documented same-user limitation.
    p = fake_proc(tmp_path, {100: ("a", [1])})
    fd_dir = os.path.join(p, "100", "fd")
    os.chmod(fd_dir, 0o000)
    # If running as root, chmod 000 does not actually deny access.
    # Skip the test in that case with a clear reason.
    if os.access(fd_dir, os.R_OK):
        pytest.skip("running as root; chmod 000 does not deny access")
    try:
        assert holders(p) == {}
    finally:
        os.chmod(fd_dir, 0o755)


def test_permission_error_on_fd_dir_is_skipped(tmp_path, monkeypatch):
    # Test that PermissionError on fd listing is caught and skipped.
    # This deterministically exercises the error-handling branch that works
    # in any context (root or not) and proves it is taken.
    p = fake_proc(tmp_path, {100: ("a", [1])})

    # Monkeypatch os.listdir to raise PermissionError only for this pid's fd dir.
    original_listdir = os.listdir
    def patched_listdir(path):
        if path.endswith("100/fd"):
            raise PermissionError("mock: permission denied")
        return original_listdir(path)
    monkeypatch.setattr(os, "listdir", patched_listdir)

    # When fd is not listable, holders() must skip it and return empty.
    assert holders(p) == {}


def test_pid_alive_and_name(tmp_path):
    p = fake_proc(tmp_path, {777: ("vllm", [0])})
    assert pid_alive(777, p) is True
    assert pid_alive(888, p) is False
    assert process_name(777, p) == "vllm"
    assert process_name(888, p) == ""


def test_sudo_available_is_false_when_sudo_refuses(monkeypatch):
    import subprocess as sp
    from gozer import procfd

    class Refused:
        returncode = 1

    monkeypatch.setattr(sp, "run", lambda *a, **k: Refused())
    assert procfd.sudo_available() is False


def test_sudo_available_is_false_when_sudo_is_missing(monkeypatch):
    import subprocess as sp
    from gozer import procfd

    def boom(*a, **k):
        raise FileNotFoundError("no sudo here")

    monkeypatch.setattr(sp, "run", boom)
    assert procfd.sudo_available() is False


def test_sudo_scan_uses_non_interactive_flag(tmp_path):
    """An agent has no terminal; sudo must never be able to prompt."""
    from gozer import procfd
    seen = {}

    class Ok:
        returncode, stdout, stderr = 0, "{}", ""

    def capture(argv, **kw):
        seen["argv"] = argv
        return Ok()

    procfd.holders(str(tmp_path), use_sudo=True, runner=capture)
    assert seen["argv"][:2] == ["sudo", "-n"]


def test_sudo_results_merge_with_unprivileged_scan(tmp_path):
    from gozer import procfd
    p = fake_proc(tmp_path, {100: ("mine", [1])})

    class Ok:
        returncode = 0
        stdout = '{"1": [200], "2": [300]}'
        stderr = ""

    merged = procfd.holders(p, use_sudo=True, runner=lambda *a, **k: Ok())
    assert merged == {1: [100, 200], 2: [300]}


def test_failed_sudo_degrades_to_same_user_truth(tmp_path):
    """Losing the elevated scan must not lose the scan we could do."""
    from gozer import procfd
    p = fake_proc(tmp_path, {100: ("mine", [1])})

    class Failed:
        returncode, stdout, stderr = 1, "", "sudo: a password is required"

    assert procfd.holders(p, use_sudo=True,
                          runner=lambda *a, **k: Failed()) == {1: [100]}
