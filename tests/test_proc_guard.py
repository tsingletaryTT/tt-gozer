"""The conftest guard that keeps tests off the real /proc."""
import pytest

from gozer import procfd


@pytest.mark.parametrize("spelling", ["/proc", "/proc/", "//proc", "/proc/../proc"])
def test_the_guard_rejects_every_spelling_of_the_real_proc(spelling):
    # These all name the real /proc. An exact string compare let all but the
    # first through. The autouse fixture in conftest.py wraps procfd.holders,
    # so calling it here exercises the guard without scanning anything.
    with pytest.raises(AssertionError, match="real /proc"):
        procfd.holders(spelling)
