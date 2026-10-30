# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Tests for the example afternoon: extract, facts, captions, timeline."""
import copy
import json
import os

from site_build.afternoon import captions, extract, facts, lanes_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "site", "data", "afternoon-2026-10-26.jsonl")
START, END = "2026-10-26T16:00:00Z", "2026-10-26T20:30:00Z"
B1 = ["0000:01:00.0", "0000:02:00.0"]


def committed():
    with open(DATA) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def g(ts, who, lease, chips=B1):
    return {"ts": ts, "event": "granted", "who": who, "chips": chips, "lease_id": lease}


def r(ts, who, lease, dur, chips=B1):
    return {"ts": ts, "event": "released", "who": who, "chips": chips,
            "lease_id": lease, "duration_s": dur}


def test_extract_drops_reasons_and_unknown_keys():
    ev = [dict(g("2026-10-26T17:00:00Z", "claude:a", "x1"), reason="secret", pid=7, why="w")]
    out = extract(ev, START, END)
    assert out == [g("2026-10-26T17:00:00Z", "claude:a", "x1")]


def test_extract_keeps_only_the_window():
    ev = [g("2026-10-26T15:59:59Z", "claude:a", "old"), r("2026-10-26T16:30:00Z", "claude:a", "old", 9),
          g("2026-10-26T17:00:00Z", "claude:b", "in"),
          r("2026-10-26T21:00:00Z", "claude:b", "in", 14400),
          g("2026-10-26T21:00:01Z", "claude:c", "late"),
          {"ts": "2026-10-26T18:00:00Z", "event": "queued", "who": "claude:d", "ticket": "t"},
          {"ts": "2026-10-26T18:00:00Z", "event": "reaped", "who": "claude:d"}]
    out = extract(ev, START, END)
    assert [(e["event"], e["who"]) for e in out] == [
        ("granted", "claude:b"), ("queued", "claude:d"), ("released", "claude:b")]


def test_checked_in_extract_has_no_reason_text():
    for rec in committed():
        assert "reason" not in rec and "why" not in rec and "pid" not in rec
        assert set(rec) <= {"ts", "event", "who", "chips", "lease_id", "ticket", "duration_s"}


def test_facts_match_the_log_the_spec_quotes():
    f = facts(committed())
    assert f["longest_hold"] == {"who": "claude:kernel-probe", "seconds": 14705}
    waits = {(w["who"], w["seconds"]) for w in f["waits"]}
    assert ("claude:graft-accept", 2048) in waits
    assert ("claude:full-port-pilot", 1631) in waits
    # The spec's "fastest handoff 4 s" is the fastest non-zero gap (17:47:14 to
    # 17:47:18). The data also has a same-second handoff (18:12:42), which the
    # agreed definition counts as a gap of 0, so the minimum is 0.
    assert f["fastest_handoff_s"] == 0
    assert f["same_second_handoffs"] >= 1
    assert f["rapid_cycles"]["count"] == 6
    assert f["rapid_cycles"]["who"] == "claude:orchard-kernel-probe"
    assert 44 <= f["rapid_cycles"]["typical_s"] <= 50


def test_captions_only_state_computed_facts():
    f = facts(committed())
    base = captions(f)
    assert any("4 hours 5 minutes" in c for c in base)
    f2 = copy.deepcopy(f)
    f2["longest_hold"]["seconds"] = 7200
    f2["rapid_cycles"]["count"] = 9
    changed = captions(f2)
    assert any("2 hours" in c for c in changed) and not any("4 hours 5 minutes" in c for c in changed)
    assert any("9 times" in c for c in changed) and not any("6 times" in c for c in changed)


def test_lane_html_escapes_agent_names():
    name = 'a<b>&"c:d'
    out = lanes_html([g("2026-10-26T17:00:00Z", name, "x"), r("2026-10-26T18:00:00Z", name, "x", 3600)],
                     START, END)
    assert "<b>" not in out
    assert "a&lt;b&gt;&amp;&quot;c:d" in out or "a&lt;b&gt;&amp;&#x27;c:d" in out or "a&lt;b&gt;&amp;" in out


def test_lease_ending_after_the_window_is_clipped_and_labelled_continues():
    out = lanes_html([g("2026-10-26T20:00:00Z", "claude:a", "x"),
                      r("2026-10-26T22:00:00Z", "claude:a", "x", 7200)], START, END)
    assert "continues" in out
    # starts 3.5 h into a 4.5 h window, so it must end exactly at 100%
    block = out.split('class="lease"')[1]
    left = float(block.split("left:")[1].split("%")[0])
    width = float(block.split(";width:")[1].split("%")[0])
    assert abs(left + width - 100.0) < 0.05


def test_lease_with_no_release_is_labelled_still_running():
    out = lanes_html([g("2026-10-26T17:00:00Z", "claude:a", "x")], START, END)
    assert "still running" in out


def test_no_queue_events_still_renders():
    recs = [g("2026-10-26T17:00:00Z", "claude:a", "x"), r("2026-10-26T18:00:00Z", "claude:a", "x", 3600)]
    f = facts(recs)
    assert f["waits"] == [] and "fastest_handoff_s" not in f
    assert captions(f)
    assert "claude:a" in lanes_html(recs, START, END)


def test_empty_window_renders_a_message_not_a_crash():
    assert facts([])["waits"] == []
    assert captions(facts([])) == []
    out = lanes_html([], START, END)
    assert "No leases" in out
