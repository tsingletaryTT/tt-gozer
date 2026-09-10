"""Gatekeeper.history_root: an optional persistent home for history.jsonl,
decoupled from GOZER_ROOT (which stays ephemeral -- leases correctly vanish
on reboot, but the audit trail shouldn't have to).
"""
import os

import pytest

from gozer.gatekeeper import Gatekeeper


def test_history_root_defaults_to_root(tmp_path, sysfs):
    from conftest import QUIETBOX
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX))
    assert gk.history_root == gk.root


def test_history_root_param_overrides_default(tmp_path, sysfs):
    from conftest import QUIETBOX
    history_dir = tmp_path / "persistent"
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                     history_root=str(history_dir))
    assert gk.history_root == str(history_dir)
    assert gk.history_root != gk.root


def test_history_root_env_var_overrides_default(tmp_path, sysfs, monkeypatch):
    from conftest import QUIETBOX
    history_dir = tmp_path / "persistent"
    monkeypatch.setenv("GOZER_HISTORY_ROOT", str(history_dir))
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX))
    assert gk.history_root == str(history_dir)


def test_history_root_param_wins_over_env_var(tmp_path, sysfs, monkeypatch):
    from conftest import QUIETBOX
    monkeypatch.setenv("GOZER_HISTORY_ROOT", str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit"
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                     history_root=str(explicit))
    assert gk.history_root == str(explicit)


def test_history_root_directory_is_created(tmp_path, sysfs):
    from conftest import QUIETBOX
    history_dir = tmp_path / "persistent" / "nested"
    Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
               history_root=str(history_dir))
    assert os.path.isdir(history_dir)


def test_granted_event_lands_in_history_root_not_gozer_root(tmp_path, sysfs):
    from conftest import QUIETBOX
    from gozer.keymaster import Keymaster
    from gozer import history

    history_dir = tmp_path / "persistent"
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                     history_root=str(history_dir))
    km = Keymaster(gk)
    km.acquire("1", who="claude:test", reason="test")

    assert history.read(str(history_dir))
    assert not os.path.exists(os.path.join(str(gk.root), "history.jsonl"))


def test_history_survives_gozer_root_being_wiped(tmp_path, sysfs):
    """The whole point: GOZER_ROOT can be a reboot-cleared tmpfs while the
    history log lives elsewhere and is unaffected."""
    import shutil
    from conftest import QUIETBOX
    from gozer.keymaster import Keymaster
    from gozer import history

    history_dir = tmp_path / "persistent"
    state_dir = tmp_path / "state"
    gk = Gatekeeper(root=str(state_dir), sysfs_root=sysfs(QUIETBOX),
                     history_root=str(history_dir))
    km = Keymaster(gk)
    km.acquire("1", who="claude:test", reason="test")

    shutil.rmtree(state_dir)

    records = history.read(str(history_dir))
    assert any(r["event"] == "granted" for r in records)
