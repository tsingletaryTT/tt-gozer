"""gozer/history.py: the append-only JSONL event log.

TDD per the project convention -- these are written and confirmed to fail
against a not-yet-existing gozer/history.py before any implementation lands.
"""
import json
import os
import stat

import pytest


def test_log_appends_one_json_line_with_ts_and_event(tmp_path):
    from gozer import history

    history.log(str(tmp_path), "granted", lease_id="abc123", who="claude:test")

    path = tmp_path / "history.jsonl"
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "granted"
    assert rec["lease_id"] == "abc123"
    assert rec["who"] == "claude:test"
    # ISO-8601 with a trailing Z, via the same utcnow() everything else uses.
    assert rec["ts"].endswith("Z")


def test_log_appends_multiple_events_in_order(tmp_path):
    from gozer import history

    history.log(str(tmp_path), "granted", lease_id="one")
    history.log(str(tmp_path), "released", lease_id="one")

    lines = (tmp_path / "history.jsonl").read_text().splitlines()
    assert [json.loads(l)["event"] for l in lines] == ["granted", "released"]


def test_log_is_a_single_write_call(tmp_path, monkeypatch):
    """The whole point of O_APPEND + one write() is atomicity under PIPE_BUF
    (4096 bytes) between concurrent processes. If this ever becomes multiple
    write() calls -- e.g. someone "cleans it up" into a print() or several
    f.write()s -- concurrent writers can interleave partial lines. Pin the
    single-write-call contract directly."""
    from gozer import history

    calls = []
    real_write = os.write

    def spy_write(fd, data):
        calls.append(data)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", spy_write)
    history.log(str(tmp_path), "granted", lease_id="one")
    assert len(calls) == 1


def test_log_record_is_well_under_pipe_buf(tmp_path):
    """Each record must stay comfortably under 4096 bytes (PIPE_BUF on
    Linux) for the single-write atomicity guarantee to hold."""
    from gozer import history

    history.log(str(tmp_path), "granted", lease_id="abc123", who="claude:test",
                reason="a reasonably-sized but not absurd reason string",
                chips=["0000:01:00.0", "0000:02:00.0"], dev_indices=[0, 1],
                units=["0000000000000002"], detached=False, pid=12345,
                expanded=False, requested=1)
    line = (tmp_path / "history.jsonl").read_text().splitlines()[0]
    assert len(line.encode("utf-8")) < 4096


def test_log_never_raises_when_the_write_fails(tmp_path, monkeypatch):
    """Every write is best-effort: a full disk or permissions problem must
    never fail an acquire or, worse, a release."""
    from gozer import history

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", boom)
    # Must not raise.
    history.log(str(tmp_path), "granted", lease_id="one")


def test_log_never_raises_when_open_fails(tmp_path, monkeypatch):
    from gozer import history

    def boom(*a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "open", boom)
    history.log(str(tmp_path), "granted", lease_id="one")


def test_read_returns_records_in_file_order(tmp_path):
    from gozer import history

    history.log(str(tmp_path), "granted", lease_id="one")
    history.log(str(tmp_path), "released", lease_id="one")
    history.log(str(tmp_path), "granted", lease_id="two")

    records = history.read(str(tmp_path))
    assert [r["event"] for r in records] == ["granted", "released", "granted"]


def test_read_returns_empty_list_when_no_log_exists(tmp_path):
    from gozer import history

    assert history.read(str(tmp_path)) == []


def test_read_skips_corrupt_lines_rather_than_raising(tmp_path):
    from gozer import history

    history.log(str(tmp_path), "granted", lease_id="one")
    with open(tmp_path / "history.jsonl", "a") as f:
        f.write("not json at all\n")
    history.log(str(tmp_path), "released", lease_id="one")

    records = history.read(str(tmp_path))
    assert [r["event"] for r in records] == ["granted", "released"]
