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
    assert f["fastest_handoff_s"] == 4
    assert f["same_second_handoffs"] == 1
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
    assert "a&lt;b&gt;&amp;&quot;c:d" in out
    assert "a<b>" not in out


def test_lease_ending_after_the_window_is_clipped_and_labelled_continues():
    out = lanes_html([g("2026-10-26T20:00:00Z", "claude:a", "x"),
                      r("2026-10-26T22:00:00Z", "claude:a", "x", 7200)], START, END)
    assert "continues" in out
    # starts 3.5 h into a 4.5 h window, so it must end exactly at 100%
    block = out.split('class="lease"')[1]
    left = float(block.split("left:")[1].split("%")[0])
    width = float(block.split(";width:")[1].split("%")[0])
    assert abs(left + width - 100.0) < 0.05


def test_lease_with_no_release_is_labelled_no_release_recorded():
    out = lanes_html([g("2026-10-26T17:00:00Z", "claude:a", "x")], START, END)
    assert "no release recorded" in out and "still running" not in out


def test_no_queue_events_still_renders():
    recs = [g("2026-10-26T17:00:00Z", "claude:a", "x"), r("2026-10-26T18:00:00Z", "claude:a", "x", 3600)]
    f = facts(recs)
    assert f["waits"] == [] and f["fastest_handoff_s"] is None
    assert captions(f)
    assert "claude:a" in lanes_html(recs, START, END)


def test_empty_window_renders_a_message_not_a_crash():
    assert facts([])["waits"] == []
    assert captions(facts([])) == []
    out = lanes_html([], START, END)
    assert "No leases" in out


def _handoff(gap_s):
    """B queues, A releases, B is granted gap_s later, all on board 1."""
    t = "2026-10-26T17:00:%02dZ"
    return [g(t % 0, "claude:a", "a1"),
            {"ts": t % 1, "event": "queued", "who": "claude:b", "ticket": "t"},
            r(t % 10, "claude:a", "a1", 10),
            g(t % (10 + gap_s), "claude:b", "b1")]


def test_only_same_second_handoffs_give_no_fastest_handoff():
    f = facts(_handoff(0))
    assert f["fastest_handoff_s"] is None and f["same_second_handoffs"] == 1
    text = " ".join(captions(f))
    assert "fastest handoff" not in text and "same second" in text


def test_moving_a_grant_changes_the_fact_and_its_caption():
    f4 = facts(_handoff(4))
    f7 = facts(_handoff(7))
    assert f4["fastest_handoff_s"] == 4 and f7["fastest_handoff_s"] == 7
    assert any("took 4 seconds" in c for c in captions(f4))
    assert any("took 7 seconds" in c for c in captions(f7))
    assert not any("took 4 seconds" in c for c in captions(f7))


def q(ts, who):
    return {"ts": ts, "event": "queued", "who": who, "ticket": "t"}


def test_wait_counts_from_the_first_poll_to_the_next_grant():
    recs = [q("2026-10-26T17:00:00Z", "claude:b"), q("2026-10-26T17:01:00Z", "claude:b"),
            q("2026-10-26T17:02:00Z", "claude:b"), g("2026-10-26T17:10:00Z", "claude:b", "b1")]
    assert facts(recs)["waits"] == [{"who": "claude:b", "seconds": 600}]


def test_wait_starts_after_the_agents_previous_release():
    recs = [g("2026-10-26T16:00:00Z", "claude:b", "b0"), q("2026-10-26T16:05:00Z", "claude:b"),
            r("2026-10-26T16:10:00Z", "claude:b", "b0", 600),
            q("2026-10-26T16:20:00Z", "claude:b"), g("2026-10-26T16:30:00Z", "claude:b", "b1")]
    assert facts(recs)["waits"] == [{"who": "claude:b", "seconds": 600}]


def test_rapid_cycles_ignore_long_leases_and_need_three_short_ones():
    recs = []
    for i in range(5):  # five long leases must not count
        recs.append(r(f"2026-10-26T17:0{i}:00Z", "claude:long", f"l{i}", 130 + i))
    for i in range(3):
        recs.append(r(f"2026-10-26T18:0{i}:00Z", "claude:quick", f"q{i}", 30))
    for i in range(2):  # only two short leases: below the reporting threshold
        recs.append(r(f"2026-10-26T19:0{i}:00Z", "claude:few", f"f{i}", 20))
    rc = facts(recs)["rapid_cycles"]
    assert rc == {"who": "claude:quick", "count": 3, "typical_s": 30}
    assert "rapid_cycles" not in facts(recs[:5] + recs[-2:])
    # exactly 120 s is not under the limit
    assert "rapid_cycles" not in facts([r(f"2026-10-26T17:0{i}:00Z", "claude:x", f"x{i}", 120) for i in range(3)])


def test_extract_window_edges_are_inclusive():
    ev = [g(START, "claude:a", "s"), g(END, "claude:a", "e"),
          g("2026-10-26T15:59:59Z", "claude:a", "before"), g("2026-10-26T20:30:01Z", "claude:a", "after")]
    assert [e["lease_id"] for e in extract(ev, START, END)] == ["s", "e"]


def _wcag(bg, fg):
    """Independent WCAG contrast computation (not the production helper)."""
    def lum(h):
        v = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        v = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in v]
        return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]
    a, b = sorted((lum(bg), lum(fg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def _blocks(out):
    import re
    return re.findall(r'class="lease" style="[^"]*?background:(#[0-9A-Fa-f]{6});color:(#[0-9A-Fa-f]{6});', out)


def test_every_block_text_colour_meets_4_5_contrast():
    from site_build.afternoon import PALETTE
    recs = [g("2026-10-26T17:00:00Z", f"claude:n{i}", f"l{i}") for i in range(len(PALETTE))]
    pairs = _blocks(lanes_html(recs, START, END))
    assert len(pairs) == len(PALETTE)
    for bg, fg in pairs:
        assert _wcag(bg, fg) >= 4.5, (bg, fg)


def test_each_agent_on_the_committed_afternoon_has_its_own_colour():
    recs = committed()
    agents = {x["who"] for x in recs if x["event"] == "granted"}
    pairs = _blocks(lanes_html(recs, START, END))
    assert len({bg for bg, _ in pairs}) == len(agents)
    for bg, fg in pairs:
        assert _wcag(bg, fg) >= 4.5, (bg, fg)


def test_caption_calls_the_47_seconds_typical_and_the_hold_longest_in_the_log():
    text = " ".join(captions(facts(committed())))
    assert "a typical lease lasting about 47 seconds" in text
    assert "the longest lease in this log" in text
    assert "each lease" not in text and "of the afternoon" not in text


def test_release_without_lease_id_does_not_match_a_grant_without_lease_id():
    ev = [{"ts": "2026-10-26T17:00:00Z", "event": "granted", "who": "claude:a"},
          {"ts": "2026-10-26T21:00:00Z", "event": "released", "who": "claude:b", "duration_s": 5}]
    assert [e["event"] for e in extract(ev, START, END)] == ["granted"]


def test_a_very_long_agent_name_cannot_overflow_its_block():
    name = "claude:" + "x" * 300
    out = lanes_html([g("2026-10-26T17:00:00Z", name, "x"), r("2026-10-26T18:00:00Z", name, "x", 3600)],
                     START, END)
    block = out.split('class="lease"')[1]
    assert "overflow:hidden" in block and "white-space:nowrap" in block
