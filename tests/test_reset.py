import pytest
from gozer.reset import reset_command, reset_chips, ResetError


def test_builds_a_plain_per_bdf_reset():
    assert reset_command(["0000:03:00.0", "0000:04:00.0"]) == [
        "tt-smi", "-r", "0000:03:00.0,0000:04:00.0"]


def test_honours_the_reset_cmd_override():
    assert reset_command(["0000:01:00.0"], cmd="/fake/tt-smi")[0] == "/fake/tt-smi"


def test_never_emits_a_board_level_m3_reset():
    # ASIC_DMC_RESET / reset_m3 is the one genuinely board-wide path. Banned.
    argv = reset_command(["0000:01:00.0", "0000:02:00.0"])
    joined = " ".join(argv)
    assert "m3" not in joined.lower()
    assert "--all" not in joined and "all" not in argv


def test_refuses_an_empty_bdf_list():
    with pytest.raises(ResetError):
        reset_command([])


def test_rejects_anything_that_is_not_a_bdf():
    # Guards against an integer index sneaking in, which tt-smi would read as a
    # UMD logical id -- a different device namespace.
    with pytest.raises(ResetError):
        reset_command(["2"])


def test_reset_chips_reports_success_and_failure():
    class Ok:
        returncode, stdout, stderr = 0, "reset done", ""

    class Bad:
        returncode, stdout, stderr = 1, "", "boom"

    ok, out = reset_chips(["0000:01:00.0"], runner=lambda *a, **k: Ok())
    assert ok is True and "reset done" in out

    ok, out = reset_chips(["0000:01:00.0"], runner=lambda *a, **k: Bad())
    assert ok is False and "boom" in out
