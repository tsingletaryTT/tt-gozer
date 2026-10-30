# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""A history directory that cannot be created must not stop the gate.

`history.log` already swallows OSError, so a missing history directory only
means no history is written. Creating the directory used to raise inside
`Gatekeeper()`, which aborted even `gozer release` while the gate was fine.
The real gate directories (gate, leases, queue, mutex) still fail loudly.
"""
import json

import pytest

from conftest import QUIETBOX
from gozer import history
from gozer.cli import main
from gozer.gatekeeper import Gatekeeper


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in ("GOZER_ROOT", "GOZER_HISTORY_ROOT", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("gozer.gatekeeper.DEFAULT_ROOT", str(tmp_path / "gate"))
    return tmp_path


def _blocked_home(tmp_path):
    """HOME whose `.local` is a regular file, so ~/.local/state/tt-gozer cannot exist."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local").write_text("not a directory")
    return home


def _event():
    return {"event": "granted", "who": "t"}


def test_explicit_history_root_under_a_file_is_tolerated(clean_env, sysfs):
    blocker = clean_env / "blocker"
    blocker.write_text("x")
    root = str(blocker / "history")
    gk = Gatekeeper(root=str(clean_env / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root="/nonexistent", history_root=root)
    history.log(gk.history_root, _event())  # must not raise


def test_env_history_root_under_a_file_is_tolerated(clean_env, sysfs, monkeypatch):
    blocker = clean_env / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("GOZER_HISTORY_ROOT", str(blocker / "history"))
    gk = Gatekeeper(sysfs_root=sysfs(QUIETBOX), proc_root="/nonexistent")
    history.log(gk.history_root, _event())


def test_default_user_state_dir_that_cannot_be_made_is_tolerated(clean_env, sysfs):
    _blocked_home(clean_env)
    gk = Gatekeeper(sysfs_root=sysfs(QUIETBOX), proc_root="/nonexistent")
    assert gk.history_root.endswith(".local/state/tt-gozer")
    history.log(gk.history_root, _event())


def test_a_gate_directory_that_cannot_be_made_still_raises(clean_env, sysfs):
    blocker = clean_env / "blocker"
    blocker.write_text("x")
    with pytest.raises(OSError):
        Gatekeeper(root=str(blocker / "state"), sysfs_root=sysfs(QUIETBOX),
                   proc_root="/nonexistent")


def test_release_succeeds_when_the_history_dir_cannot_be_made(clean_env, sysfs, monkeypatch, capsys):
    # Take the lease while history works, then break it and release.
    (clean_env / "proc" / "1" / "fd").mkdir(parents=True)
    (clean_env / "proc" / "1" / "comm").write_text("python\n")
    monkeypatch.setenv("GOZER_ROOT", str(clean_env / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", str(clean_env / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    assert main(["acquire", "--chips", "1", "--who", "t", "--json"]) == 0
    lease = json.loads(capsys.readouterr().out)["lease_id"]
    blocker = clean_env / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("GOZER_HISTORY_ROOT", str(blocker / "history"))
    assert main(["release", lease, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "released"
