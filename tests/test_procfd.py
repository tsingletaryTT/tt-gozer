import os
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
    os.chmod(os.path.join(p, "100", "fd"), 0o000)
    try:
        assert holders(p) == {}
    finally:
        os.chmod(os.path.join(p, "100", "fd"), 0o755)


def test_pid_alive_and_name(tmp_path):
    p = fake_proc(tmp_path, {777: ("vllm", [0])})
    assert pid_alive(777, p) is True
    assert pid_alive(888, p) is False
    assert process_name(777, p) == "vllm"
    assert process_name(888, p) == ""
