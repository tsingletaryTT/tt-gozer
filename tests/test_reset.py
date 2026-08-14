import subprocess

import pytest

from gozer.reset import reset_command, reset_chips, ResetError


def test_builds_a_plain_per_bdf_reset():
    assert reset_command(["0000:03:00.0", "0000:04:00.0"]) == [
        "tt-smi", "-r", "0000:03:00.0,0000:04:00.0"]


def test_honours_the_reset_cmd_override():
    assert reset_command(["0000:01:00.0"], cmd="/fake/tt-smi")[0] == "/fake/tt-smi"


def test_never_emits_a_board_level_m3_reset():
    # ASIC_DMC_RESET / reset_m3 is the one genuinely board-wide path. Banned.
    # The structural guarantee is that reset_command returns a fixed three-element list
    # [exe, "-r", "bdfs"], so no extra flags can be added. See test_builds_a_plain_per_bdf_reset
    # for the test that ensures that shape. This test is a tripwire for the literal name.
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


def test_rejects_bdf_with_out_of_range_function():
    # Function field is 3 bits: 0-7. Values 8-f are impossible.
    with pytest.raises(ResetError):
        reset_command(["0000:01:00.8"])
    with pytest.raises(ResetError):
        reset_command(["0000:01:00.f"])


def test_rejects_bdf_with_out_of_range_device():
    # Device field is 5 bits: 0x00-0x1f (0-31). Values 0x20+ are impossible.
    with pytest.raises(ResetError):
        reset_command(["0000:01:20.0"])


def test_rejects_mixed_valid_and_invalid_bdfs():
    # If any BDF is invalid, reject the whole list before executing anything.
    with pytest.raises(ResetError):
        reset_command(["0000:01:00.0", "0000:02:00.8"])


def test_reset_chips_reports_success_and_failure():
    class Ok:
        returncode, stdout, stderr = 0, "reset done", ""

    class Bad:
        returncode, stdout, stderr = 1, "", "boom"

    ok, out = reset_chips(["0000:01:00.0"], runner=lambda *a, **k: Ok())
    assert ok is True and "reset done" in out

    ok, out = reset_chips(["0000:01:00.0"], runner=lambda *a, **k: Bad())
    assert ok is False and "boom" in out


def test_reset_chips_never_raises_on_timeout():
    # A wedged device is exactly when a reset hangs. reset_chips must not raise
    # so that release() can still tear down the lease and report honestly.
    def timeout_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 300)

    ok, out = reset_chips(["0000:01:00.0"], runner=timeout_runner)
    assert ok is False
    assert "TimeoutExpired" in out or "timed out" in out.lower()
    assert out  # non-empty message


def test_reset_chips_never_raises_on_missing_executable():
    def not_found_runner(*args, **kwargs):
        raise FileNotFoundError("tt-smi not found")

    ok, out = reset_chips(["0000:01:00.0"], runner=not_found_runner)
    assert ok is False
    assert "FileNotFoundError" in out or "not found" in out.lower()
    assert out  # non-empty message


def test_reset_chips_never_raises_on_oserror():
    def oserror_runner(*args, **kwargs):
        raise OSError("permission denied")

    ok, out = reset_chips(["0000:01:00.0"], runner=oserror_runner)
    assert ok is False
    assert "OSError" in out or "permission" in out.lower()
    assert out  # non-empty message


def test_reset_chips_env_override_when_cmd_is_none(monkeypatch):
    # GOZER_RESET_CMD is honored when cmd parameter is None.
    monkeypatch.setenv("GOZER_RESET_CMD", "/custom/tt-smi")

    class Mock:
        returncode, stdout, stderr = 0, "", ""

    def capture_argv_runner(argv, *args, **kwargs):
        # Store argv so we can check it
        capture_argv_runner.last_argv = argv
        return Mock()

    reset_chips(["0000:01:00.0"], cmd=None, runner=capture_argv_runner)
    assert capture_argv_runner.last_argv[0] == "/custom/tt-smi"


def test_reset_chips_cmd_parameter_wins_over_env(monkeypatch):
    # Explicit cmd parameter takes precedence over GOZER_RESET_CMD env var.
    monkeypatch.setenv("GOZER_RESET_CMD", "/custom/tt-smi")

    class Mock:
        returncode, stdout, stderr = 0, "", ""

    def capture_argv_runner(argv, *args, **kwargs):
        # Store argv so we can check it
        capture_argv_runner.last_argv = argv
        return Mock()

    reset_chips(["0000:01:00.0"], cmd="/explicit/tt-smi", runner=capture_argv_runner)
    assert capture_argv_runner.last_argv[0] == "/explicit/tt-smi"
