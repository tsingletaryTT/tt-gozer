import json
import os
import pytest
from gozer.cli import main
from conftest import QUIETBOX


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    proc = tmp_path / "proc" / "1" / "fd"
    proc.mkdir(parents=True)
    (tmp_path / "proc" / "1" / "comm").write_text("python\n")
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return tmp_path


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def test_status_json_lists_every_chip_free(env, capsys):
    code, out = run(["status", "--json"], capsys)
    assert code == 0
    data = json.loads(out)
    assert len(data["chips"]) == 4
    assert {c["state"] for c in data["chips"]} == {"FREE"}


def test_status_human_output_names_boards_and_chips(env, capsys):
    code, out = run(["status"], capsys)
    assert code == 0
    assert "0000000000000001" in out
    assert "chip 0" in out and "FREE" in out


def test_topology_reports_grain(env, capsys):
    code, out = run(["topology", "--json"], capsys)
    assert json.loads(out)["grain"] == "board"


def test_acquire_prints_the_export_line_and_exits_zero(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test"], capsys)
    assert code == 0
    assert "export TT_VISIBLE_DEVICES=" in out
    assert "0000:" in out


def test_acquire_notes_the_board_expansion(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "claude:test"], capsys)
    assert "expand" in out.lower() or "whole" in out.lower()


def test_acquire_json_is_machine_readable(env, capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "x", "--json"], capsys)
    data = json.loads(out)
    assert data["granted"] is True
    assert data["env"]["TT_VISIBLE_DEVICES"].count(",") == 1


def test_third_acquire_exits_10_with_a_ticket(env, capsys):
    run(["acquire", "--chips", "1", "--who", "a"], capsys)
    run(["acquire", "--chips", "1", "--who", "b"], capsys)
    code, out = run(["acquire", "--chips", "1", "--who", "c", "--json"], capsys)
    assert code == 10
    assert json.loads(out)["ticket"]


def test_summon_is_an_alias_for_acquire(env, capsys):
    code, out = run(["summon", "--chips", "1", "--who", "claude:test"], capsys)
    assert code == 0 and "export TT_VISIBLE_DEVICES=" in out


def test_release_frees_the_board(env, capsys):
    _, out = run(["acquire", "--chips", "1", "--who", "x", "--json"], capsys)
    lease = json.loads(out)["lease_id"]
    code, _ = run(["release", lease], capsys)
    assert code == 0
    _, status = run(["status", "--json"], capsys)
    assert {c["state"] for c in json.loads(status)["chips"]} == {"FREE"}


def test_banish_is_an_alias_for_release(env, capsys):
    _, out = run(["acquire", "--chips", "1", "--who", "x", "--json"], capsys)
    lease = json.loads(out)["lease_id"]
    assert run(["banish", lease], capsys)[0] == 0


def test_release_of_unknown_lease_exits_13(env, capsys):
    code, _ = run(["release", "nosuch"], capsys)
    assert code == 13


def test_env_prints_export_lines_for_a_lease(env, capsys):
    _, out = run(["acquire", "--chips", "1", "--who", "x", "--json"], capsys)
    lease = json.loads(out)["lease_id"]
    code, text = run(["env", lease], capsys)
    assert code == 0 and text.startswith("export TT_VISIBLE_DEVICES=")


def test_queue_lists_waiting_requests(env, capsys):
    run(["acquire", "--chips", "all", "--who", "hog"], capsys)
    run(["acquire", "--chips", "1", "--who", "waiter"], capsys)
    code, out = run(["queue", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["queue"][0]["who"] == "waiter"


def test_cancel_removes_a_ticket(env, capsys):
    run(["acquire", "--chips", "all", "--who", "hog"], capsys)
    _, out = run(["acquire", "--chips", "1", "--who", "w", "--json"], capsys)
    ticket = json.loads(out)["ticket"]
    assert run(["cancel", ticket], capsys)[0] == 0
    _, q = run(["queue", "--json"], capsys)
    assert json.loads(q)["queue"] == []


def test_adopt_wraps_a_lease_around_untracked_work(env, capsys, tmp_path):
    # Simulate a manually started server holding chips 0 and 1.
    fd = tmp_path / "proc" / "1" / "fd"
    os.symlink("/dev/tenstorrent/0", fd / "3")
    os.symlink("/dev/tenstorrent/1", fd / "4")
    _, before = run(["status", "--json"], capsys)
    assert "BUSY-UNTRACKED" in before
    code, _ = run(["adopt", "0", "--who", "manual:vllm"], capsys)
    assert code == 0
    _, after = run(["status", "--json"], capsys)
    states = {c["dev_index"]: c["state"] for c in json.loads(after)["chips"]}
    assert states[0] == "HELD" and states[1] == "HELD"


def test_wait_times_out_and_exits_11(env, capsys):
    run(["acquire", "--chips", "all", "--who", "hog"], capsys)
    _, out = run(["acquire", "--chips", "1", "--who", "w", "--json"], capsys)
    ticket = json.loads(out)["ticket"]
    code, _ = run(["wait", ticket, "--timeout", "1s"], capsys)
    assert code == 11


def test_wait_keeps_the_callers_ticket_and_leaves_no_orphan(env, capsys):
    """The queue was non-functional on a real box, and this is the proof.

    `gozer acquire` hands back a ticket whose pid is the CLI process that
    just exited. The first iteration of `gozer wait` called acquire, whose
    prune dropped that ticket as a dead waiter, and _ticket_for then minted a
    brand-new one the caller never learned about: `wait` reported the ticket
    gone, the caller's place in line was lost, and an orphan sat in the queue.
    """
    run(["acquire", "--chips", "all", "--who", "hog"], capsys)
    _, out = run(["acquire", "--chips", "1", "--who", "w", "--json"], capsys)
    ticket = json.loads(out)["ticket"]

    code, _ = run(["wait", ticket, "--timeout", "1s"], capsys)

    assert code == 11                       # still queued, not "gone" (13)
    _, q = run(["queue", "--json"], capsys)
    assert [e["ticket"] for e in json.loads(q)["queue"]] == [ticket]


def test_acquire_with_an_unknown_ticket_exits_13(env, capsys):
    """A ticket that is not in the queue is an error, never a silent new
    ticket issued under the caller's nose."""
    run(["acquire", "--chips", "all", "--who", "hog"], capsys)
    code, _ = run(["acquire", "--chips", "1", "--who", "x",
                   "--ticket", "nosuch"], capsys)
    assert code == 13
    _, q = run(["queue", "--json"], capsys)
    assert json.loads(q)["queue"] == []


def test_status_survives_a_corrupt_since_timestamp(env, capsys):
    """A corrupt state file must not masquerade as "the box is busy".

    cli.main maps a stray ValueError to exit 12 ("unavailable and queueing
    disabled"), so an unparseable `since` used to tell every agent on the box
    to keep waiting for hardware that was in fact free.
    """
    from gozer.gatekeeper import Gatekeeper
    gk = Gatekeeper()
    lease = {"lease_id": "bad", "detached": True, "since": "not-a-timestamp",
             "chips": ["0000:01:00.0"], "dev_indices": [0],
             "units": ["0000000000000002"], "who": "claude:x", "pid": 1}
    gk.claim_unit("0000000000000002", lease)
    gk.write_lease(lease)

    code, out = run(["status", "--json"], capsys)
    assert code == 0
    assert "FREE" in out


def test_unreadable_topology_exits_14(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", str(tmp_path / "nothing"))
    code, _ = run(["status"], capsys)
    assert code == 14


def test_reconcile_sudo_flag_reaches_the_scan(env, capsys, monkeypatch):
    """--sudo must actually change behaviour, not just be accepted."""
    from gozer import procfd
    seen = {}
    real = procfd.holders

    def spy(proc_root="/proc", use_sudo=False, **kw):
        seen["use_sudo"] = use_sudo
        return real(proc_root)

    monkeypatch.setattr(procfd, "holders", spy)
    code, _ = run(["reconcile", "--sudo"], capsys)
    assert code == 0
    assert seen["use_sudo"] is True


def test_reconcile_without_sudo_does_not_elevate(env, capsys, monkeypatch):
    from gozer import procfd
    seen = {}
    real = procfd.holders

    def spy(proc_root="/proc", use_sudo=False, **kw):
        seen["use_sudo"] = use_sudo
        return real(proc_root)

    monkeypatch.setattr(procfd, "holders", spy)
    run(["reconcile"], capsys)
    assert seen["use_sudo"] is False


def test_status_never_opens_a_device_node(env, capsys, monkeypatch):
    """Hard guard on the core safety promise of this tool."""
    real_open = open
    opened = []

    def watching_open(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", watching_open)
    run(["status"], capsys)
    assert not any(p.startswith("/dev/tenstorrent") for p in opened)


def test_acquire_then_status_reports_the_lease_still_held(tmp_path, sysfs, monkeypatch, capsys):
    """The workflow that was actually broken: `acquire`, then some other
    command reconciles (here, `status`) before anything has opened the
    device. Deliberately does NOT reuse the `env` fixture above, whose fake
    /proc happens to register pid "1" as alive -- that would let a
    fixed-sentinel-pid "fix" pass this test by coincidence rather than by
    being structurally correct. This fixture's /proc is simply empty, the
    way a fresh box's `/proc` won't happen to contain whatever fixed pid a
    lease might be bound to.
    """
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setenv("GOZER_PROC_ROOT", str(proc_root))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")

    code, out = run(["acquire", "--chips", "1", "--who", "claude:test", "--json"], capsys)
    assert code == 0
    lease_id = json.loads(out)["lease_id"]

    code, out = run(["status", "--json"], capsys)
    assert code == 0
    chips = json.loads(out)["chips"]
    states = {c["dev_index"]: c["state"] for c in chips}
    assert states[0] == "CLAIMED" and states[1] == "CLAIMED"
    assert "STALE" not in states.values()

    # And the lease record itself must still be there -- not silently reaped.
    _, out = run(["env", lease_id], capsys)
    assert out.startswith("export TT_VISIBLE_DEVICES=")
