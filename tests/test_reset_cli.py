"""`gozer reset` through the CLI: exit codes and JSON."""
import json
import os
import shutil
import stat

import pytest

from conftest import QUIETBOX, fake_proc_tree
from gozer.cli import main

OWNER = 5_000_100
CHILD = 5_000_200


@pytest.fixture
def env(tmp_path, sysfs, monkeypatch):
    marker = tmp_path / "reset-ran"
    script = tmp_path / "reset.sh"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {marker}\nexit ${{FAKE_RESET_EXIT:-0}}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", fake_proc_tree(tmp_path, {OWNER: {"ppid": 1}}))
    monkeypatch.setenv("GOZER_RESET_CMD", str(script))
    return tmp_path, marker


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def held_chips(capsys):
    """The (state, who) pairs `status` shows for chips with a lease holder.
    `status` does not print lease ids, so the lease is identified by its owner."""
    code, out = run(["status", "--json"], capsys)
    assert code == 0
    return [(c["state"], c["who"]) for c in json.loads(out)["chips"] if c["who"]]


def take_lease(capsys):
    code, out = run(["acquire", "--chips", "1", "--who", "orchard:test",
                     "--owner-pid", str(OWNER), "--json"], capsys)
    assert code == 0
    return json.loads(out)["lease_id"]


def test_reset_exits_0_resets_and_keeps_the_lease(env, capsys):
    _, marker = env
    lease = take_lease(capsys)
    code, out = run(["reset", lease, "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["reset"] is True and payload["status"] == "reset"
    assert marker.exists()
    held = held_chips(capsys)                         # the lease is still held
    assert held and all(who == "orchard:test" for _, who in held)


def test_reset_of_an_unknown_lease_exits_13(env, capsys):
    code, out = run(["reset", "nope", "--json"], capsys)
    assert code == 13 and json.loads(out)["status"] == "not-found"


def test_reset_with_a_device_open_exits_15_and_runs_nothing(env, capsys, monkeypatch):
    tmp_path, marker = env
    lease = take_lease(capsys)
    monkeypatch.setenv("GOZER_PROC_ROOT", fake_proc_tree(
        tmp_path, {OWNER: {"ppid": 1}, CHILD: {"ppid": OWNER, "devs": [0]}}))
    code, out = run(["reset", lease, "--json"], capsys)
    assert code == 15 and json.loads(out)["status"] == "refused"
    assert not marker.exists()


def test_reset_force_with_a_device_open_exits_0(env, capsys, monkeypatch):
    tmp_path, marker = env
    lease = take_lease(capsys)
    monkeypatch.setenv("GOZER_PROC_ROOT", fake_proc_tree(
        tmp_path, {OWNER: {"ppid": 1}, CHILD: {"ppid": OWNER, "devs": [0]}}))
    code, _ = run(["reset", lease, "--force"], capsys)
    assert code == 0 and marker.exists()


def test_a_failed_reset_exits_17_and_keeps_the_lease(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_RESET_EXIT", "3")
    lease = take_lease(capsys)
    code, out = run(["reset", lease, "--json"], capsys)
    assert code == 17 and json.loads(out)["status"] == "failed"
    held = held_chips(capsys)
    assert held and all(who == "orchard:test" for _, who in held)


def test_exit_codes_17_and_18_are_distinct_from_the_existing_ones():
    from gozer import cli
    codes = [cli.EXIT_OK, cli.EXIT_QUEUED, cli.EXIT_WAIT_TIMEOUT, cli.EXIT_UNAVAILABLE,
             cli.EXIT_NO_LEASE, cli.EXIT_NO_TOPOLOGY, cli.EXIT_RELEASE_REFUSED,
             cli.EXIT_GATE_STUCK, cli.EXIT_RESET_FAILED, cli.EXIT_RESET_CHANGED_HANDS,
             cli.EXIT_INTERRUPTED]
    assert len(codes) == len(set(codes))
    assert (cli.EXIT_RESET_FAILED, cli.EXIT_RESET_CHANGED_HANDS) == (17, 18)


def test_a_reset_event_does_not_disturb_the_report_statistics():
    from gozer import report
    base = [
        {"ts": "2026-10-02T10:00:00Z", "event": "granted", "lease_id": "aaa",
         "who": "orchard:test", "chips": ["0000:01:00.0"]},
        {"ts": "2026-10-02T11:00:00Z", "event": "released", "lease_id": "aaa",
         "who": "orchard:test", "chips": ["0000:01:00.0"], "duration_s": 3600.0},
    ]
    reset = {"ts": "2026-10-02T10:30:00Z", "event": "reset", "lease_id": "aaa",
             "who": "orchard:test", "chips": ["0000:01:00.0"], "reset_ok": True}
    assert report.compute_stats(base[:1] + [reset] + base[1:]) == report.compute_stats(base)


# ---- exit codes and output for the remaining statuses ----

def test_json_carries_exactly_reset_status_message(env, capsys):
    lease = take_lease(capsys)
    _, out = run(["reset", lease, "--json"], capsys)
    payload = json.loads(out)
    assert set(payload) == {"reset", "status", "message"}
    assert payload["message"] == "reset ok"


def test_a_failed_reset_json_says_reset_false(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_RESET_EXIT", "3")
    lease = take_lease(capsys)
    _, out = run(["reset", lease, "--json"], capsys)
    assert json.loads(out)["reset"] is False


def test_human_output_prints_the_message(env, capsys):
    lease = take_lease(capsys)
    code, out = run(["reset", lease], capsys)
    assert code == 0 and out.strip() == "reset ok"


def test_a_lease_reaped_during_the_reset_exits_18(env, capsys):
    tmp_path, _ = env
    lease = take_lease(capsys)
    # The reset command takes the unit away by removing its gate lock, which
    # is what a reap does while the reset runs.
    gate = tmp_path / "state" / "gate"
    script = tmp_path / "reset.sh"
    script.write_text(f"#!/bin/sh\nrm -rf {gate}/*\nexit 0\n")
    code, out = run(["reset", lease, "--json"], capsys)
    assert code == 18 and json.loads(out)["status"] == "changed-hands"


def test_a_reset_of_chips_now_owned_by_another_lease_exits_15(env, capsys):
    tmp_path, marker = env
    lease = take_lease(capsys)
    import glob
    locks = glob.glob(str(tmp_path / "state" / "gate" / "*.lock" / "lease.json"))
    assert locks
    for p in locks:
        with open(p) as f:
            data = json.load(f)
        data["lease_id"] = "newtenant"
        with open(p, "w") as f:
            json.dump(data, f)
    code, out = run(["reset", lease, "--json"], capsys)
    assert code == 15 and json.loads(out)["status"] == "refused"
    assert not marker.exists()


def test_reset_requires_a_lease_argument(env, capsys):
    with pytest.raises(SystemExit) as e:
        main(["reset"])
    assert e.value.code == 2


def test_reset_is_listed_in_help(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "reset" in capsys.readouterr().out


def test_another_lease_taking_the_unit_during_the_reset_exits_18(env, capsys):
    tmp_path, _ = env
    lease = take_lease(capsys)
    lock = next((tmp_path / "state" / "gate").glob("*.lock"))
    # The reset command swaps the lock's lease for another tenant's, which is
    # what a reap followed by a new acquire looks like from outside.
    script = tmp_path / "reset.sh"
    script.write_text(
        f"#!/bin/sh\nprintf '{{\"lease_id\": \"newtenant\", \"who\": \"claude:b\"}}' "
        f"> {lock}/lease.json\nexit 0\n")
    code, out = run(["reset", lease, "--json"], capsys)
    assert code == 18 and json.loads(out)["status"] == "changed-hands"
    assert "newtenant" in json.loads(out)["message"]
    assert json.load(open(lock / "lease.json"))["lease_id"] == "newtenant"


# ---- a lease made by `gozer adopt` ----

ADOPTED = 5_000_300


def adopt_board_zero(env, capsys, monkeypatch):
    tmp_path, _ = env
    monkeypatch.setenv("GOZER_PROC_ROOT", fake_proc_tree(
        tmp_path, {ADOPTED: {"ppid": 1, "devs": [0]}}))
    code, out = run(["adopt", "0", "--who", "manual:vllm", "--json"], capsys)
    assert code == 0
    return json.loads(out)["lease_id"]


def test_an_adopted_lease_with_no_device_open_resets_exactly_its_board(env, capsys, monkeypatch):
    tmp_path, marker = env
    lease = adopt_board_zero(env, capsys, monkeypatch)
    # The adopted process exits. fake_proc_tree adds to an existing tree, so
    # its directory is removed by hand.
    shutil.rmtree(os.path.join(os.environ["GOZER_PROC_ROOT"], str(ADOPTED)))
    code, _o = run(["reset", lease], capsys)
    assert code == 0, _o
    argv = marker.read_text().split()
    assert argv[0] == "-r" and set(argv[1].split(",")) == {"0000:01:00.0", "0000:02:00.0"}


def test_an_adopted_lease_with_its_process_still_open_exits_15(env, capsys, monkeypatch):
    _, marker = env
    lease = adopt_board_zero(env, capsys, monkeypatch)
    code, out = run(["reset", lease, "--json"], capsys)
    assert code == 15 and json.loads(out)["status"] == "refused"
    assert not marker.exists()


def test_force_resets_an_adopted_lease_whose_process_is_still_open(env, capsys, monkeypatch):
    _, marker = env
    lease = adopt_board_zero(env, capsys, monkeypatch)
    code, _ = run(["reset", lease, "--force"], capsys)
    assert code == 0
    argv = marker.read_text().split()
    assert set(argv[1].split(",")) == {"0000:01:00.0", "0000:02:00.0"}


def test_a_chip_id_that_is_not_a_pci_address_exits_15_and_runs_nothing(env, capsys, monkeypatch):
    import re
    from gozer import reset as reset_mod
    _, marker = env
    lease = take_lease(capsys)
    monkeypatch.setattr(reset_mod, "BDF_RE", re.compile(r"^never-matches$"))
    code, out = run(["reset", lease, "--json"], capsys)
    assert code == 15 and json.loads(out)["status"] == "refused"
    assert "nothing was reset" in json.loads(out)["message"]
    assert not marker.exists()
