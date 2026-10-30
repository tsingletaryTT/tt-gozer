# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""scripts/make_afternoon_extract.py accepts a bare filename for --out."""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_bare_out_filename_is_written_in_the_current_directory(tmp_path, monkeypatch):
    src = tmp_path / "history.jsonl"
    src.write_text(json.dumps({"ts": "2026-10-26T17:00:00Z", "event": "granted", "who": "claude:a",
                               "chips": ["0000:01:00.0"], "lease_id": "x1"}) + "\n")
    spec = importlib.util.spec_from_file_location(
        "make_afternoon_extract", os.path.join(ROOT, "scripts", "make_afternoon_extract.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--source", str(src), "--out", "afternoon.jsonl"])
    assert mod.main() == 0
    assert (tmp_path / "afternoon.jsonl").read_text().strip()
