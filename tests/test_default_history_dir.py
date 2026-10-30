# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""history.jsonl defaults to a durable per-user directory.

It used to live in GOZER_ROOT (/tmp/tt-gozer) and was lost at every reboot,
because nothing ever set GOZER_HISTORY_ROOT. With no root and no history root
given, it now goes to $XDG_STATE_HOME/tt-gozer (~/.local/state/tt-gozer). A
caller who names a root, by argument or GOZER_ROOT, keeps history beside it.
"""
import os
import stat

import pytest

from gozer.gatekeeper import Gatekeeper


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in ("GOZER_ROOT", "GOZER_HISTORY_ROOT", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # The gate itself must not touch the real /tmp/tt-gozer in a test.
    monkeypatch.setattr("gozer.gatekeeper.DEFAULT_ROOT", str(tmp_path / "gate"))
    return tmp_path


def make(sysfs):
    from conftest import QUIETBOX
    return Gatekeeper(sysfs_root=sysfs(QUIETBOX), proc_root="/nonexistent")


def test_default_is_the_user_state_dir(clean_env, sysfs):
    gk = make(sysfs)
    assert gk.history_root == str(clean_env / "home" / ".local" / "state" / "tt-gozer")
    assert os.path.isdir(gk.history_root)
    assert gk.history_root != gk.root


def test_xdg_state_home_is_honoured(clean_env, sysfs, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(clean_env / "xdg"))
    assert make(sysfs).history_root == str(clean_env / "xdg" / "tt-gozer")


def test_an_empty_xdg_state_home_is_ignored(clean_env, sysfs, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert make(sysfs).history_root.endswith("/.local/state/tt-gozer")


def test_gozer_history_root_still_wins(clean_env, sysfs, monkeypatch):
    monkeypatch.setenv("GOZER_HISTORY_ROOT", str(clean_env / "chosen"))
    assert make(sysfs).history_root == str(clean_env / "chosen")


def test_gozer_root_env_keeps_history_beside_it(clean_env, sysfs, monkeypatch):
    from conftest import QUIETBOX
    monkeypatch.setenv("GOZER_ROOT", str(clean_env / "custom"))
    gk = Gatekeeper(sysfs_root=sysfs(QUIETBOX), proc_root="/nonexistent")
    assert gk.history_root == gk.root == str(clean_env / "custom")


def test_a_root_argument_keeps_history_beside_it(clean_env, sysfs):
    from conftest import QUIETBOX
    gk = Gatekeeper(root=str(clean_env / "arg"), sysfs_root=sysfs(QUIETBOX),
                    proc_root="/nonexistent")
    assert gk.history_root == gk.root


def test_the_state_dir_is_not_made_world_writable(clean_env, sysfs):
    mode = stat.S_IMODE(os.stat(make(sysfs).history_root).st_mode)
    assert not mode & 0o002          # not world-writable
    assert not mode & stat.S_ISVTX   # and not the shared-gate sticky bit
