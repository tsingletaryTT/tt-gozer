import sys

from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster
from conftest import QUIETBOX


def make(tmp_path, sysfs, chips=QUIETBOX):
    root = tmp_path / "proc"
    (root / "1" / "fd").mkdir(parents=True)
    (root / "1" / "comm").write_text("python\n")
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(chips),
                    proc_root=str(root))
    return Keymaster(gk), gk


def test_run_prints_reset_failure_reason_like_release_does(tmp_path, sysfs, capsys):
    """Repro of the tt-vjepa2/discolike bug: when the reset binary can't be
    found on PATH (e.g. gozer run launched from a systemd user unit, whose
    default PATH excludes ~/.local/bin), reset_chips raises FileNotFoundError
    and release() reports reset_ok=false. `gozer release` surfaces the reason
    via cmd_release's _emit; `gozer run`'s auto-release used to swallow it."""
    km, gk = make(tmp_path, sysfs)

    def missing_binary_runner(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory")

    km.reset_runner = missing_binary_runner
    rc = km.run([sys.executable, "-c", "pass"], chips_spec="1",
                who="discolike:vjepa2", pid=1)
    assert rc == 0
    assert gk.all_leases() == []

    err = capsys.readouterr().err
    assert "gozer run:" in err
    assert "reset failed" in err
    assert "No such file or directory" in err
