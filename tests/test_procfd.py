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


from conftest import fake_proc_tree
from gozer.procfd import is_descendant, parent_pid

OWNER = 5_000_100       # far above any real pid_max, so these never name a real process
CHILD = 5_000_200
GRANDCHILD = 5_000_300
STRANGER = 5_000_400


def test_parent_pid_reads_the_ppid_line(tmp_path):
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER}})
    assert parent_pid(CHILD, root) == OWNER


def test_parent_pid_is_none_for_a_missing_process(tmp_path):
    root = fake_proc_tree(tmp_path, {})
    assert parent_pid(CHILD, root) is None


def test_parent_pid_is_none_for_an_unreadable_or_malformed_status(tmp_path):
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER}})
    (tmp_path / "proc" / str(CHILD) / "status").write_text("Name:\tpython\nPPid:\tnot-a-number\n")
    assert parent_pid(CHILD, root) is None
    (tmp_path / "proc" / str(CHILD) / "status").write_text("Name:\tpython\n")
    assert parent_pid(CHILD, root) is None


def test_a_child_and_a_grandchild_descend_from_the_owner(tmp_path):
    root = fake_proc_tree(tmp_path, {
        OWNER: {"ppid": 1}, CHILD: {"ppid": OWNER}, GRANDCHILD: {"ppid": CHILD}})
    assert is_descendant(CHILD, OWNER, root) is True
    assert is_descendant(GRANDCHILD, OWNER, root) is True


def test_a_process_is_not_its_own_descendant(tmp_path):
    root = fake_proc_tree(tmp_path, {OWNER: {"ppid": 1}})
    assert is_descendant(OWNER, OWNER, root) is False


def test_an_unrelated_process_does_not_descend_from_the_owner(tmp_path):
    root = fake_proc_tree(tmp_path, {OWNER: {"ppid": 1}, STRANGER: {"ppid": 1}})
    assert is_descendant(STRANGER, OWNER, root) is False


def test_a_chain_that_ends_at_a_missing_process_is_not_a_descendant(tmp_path):
    # CHILD names a parent that no longer exists, so the walk cannot reach OWNER.
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": STRANGER}})
    assert is_descendant(CHILD, OWNER, root) is False


def test_a_cycle_in_the_parent_chain_terminates(tmp_path):
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": GRANDCHILD}, GRANDCHILD: {"ppid": CHILD}})
    assert is_descendant(CHILD, OWNER, root) is False


def test_the_walk_stops_at_max_depth(tmp_path):
    procs = {5_001_000 + i: {"ppid": 5_001_000 + i + 1} for i in range(10)}
    procs[5_001_010] = {"ppid": OWNER}
    procs[OWNER] = {"ppid": 1}
    root = fake_proc_tree(tmp_path, procs)
    assert is_descendant(5_001_000, OWNER, root, max_depth=20) is True
    assert is_descendant(5_001_000, OWNER, root, max_depth=3) is False


def test_a_cycle_ends_the_walk_at_the_first_repeat_not_at_max_depth(tmp_path, monkeypatch):
    # max_depth alone would also end a cycle, only after 64 reads. Count the
    # reads so the repeated-pid stop is what is being checked.
    import gozer.procfd as procfd
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": GRANDCHILD}, GRANDCHILD: {"ppid": CHILD}})
    reads = []
    real = procfd.parent_pid
    monkeypatch.setattr(procfd, "parent_pid", lambda pid, r="/proc": (reads.append(pid), real(pid, r))[1])
    assert procfd.is_descendant(CHILD, OWNER, root) is False
    assert len(reads) <= 3


def test_a_cycle_that_does_not_include_the_start_ends_at_the_first_repeat(tmp_path, monkeypatch):
    # CHILD -> A -> B -> A: the first repeated pid is A. The start pid is never repeated.
    import gozer.procfd as procfd
    a, b = 5_000_500, 5_000_600
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": a}, a: {"ppid": b}, b: {"ppid": a}})
    reads = []
    real = procfd.parent_pid
    monkeypatch.setattr(procfd, "parent_pid", lambda pid, r="/proc": (reads.append(pid), real(pid, r))[1])
    assert procfd.is_descendant(CHILD, OWNER, root) is False
    assert len(reads) <= 4


def test_a_process_whose_parent_is_itself_ends_after_one_read(tmp_path, monkeypatch):
    import gozer.procfd as procfd
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": CHILD}})
    reads = []
    real = procfd.parent_pid
    monkeypatch.setattr(procfd, "parent_pid", lambda pid, r="/proc": (reads.append(pid), real(pid, r))[1])
    assert procfd.is_descendant(CHILD, OWNER, root) is False
    assert len(reads) <= 1


def test_pid_zero_is_never_an_ancestor(tmp_path):
    # ppid 0 means "no parent" (kernel threads). A caller passing 0 as the
    # owner must not match every such process.
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": 0}})
    assert is_descendant(CHILD, 0, root) is False


def test_the_default_max_depth_stops_a_100_deep_chain(tmp_path):
    # The owner sits 100 parents above the start, past the default of 64.
    procs = {5_002_000 + i: {"ppid": 5_002_000 + i + 1} for i in range(100)}
    procs[5_002_100] = {"ppid": OWNER}
    procs[OWNER] = {"ppid": 1}
    root = fake_proc_tree(tmp_path, procs)
    assert is_descendant(5_002_000, OWNER, root) is False
    assert is_descendant(5_002_000, OWNER, root, max_depth=200) is True


def test_parent_pid_is_none_for_a_ppid_line_with_no_value(tmp_path):
    root = fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER}})
    (tmp_path / "proc" / str(CHILD) / "status").write_text("Name:\tpython\nPPid:\n")
    assert parent_pid(CHILD, root) is None
