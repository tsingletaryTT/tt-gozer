# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Tests for the example roster and the verified list of projects that use gozer."""
import json
import os

import pytest

from site_build.afternoon import facts
from site_build.roster import load_users, roster

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "site", "data", "afternoon-2026-10-26.jsonl")
USERS = os.path.join(ROOT, "site", "data", "users.json")
B1 = ["0000:01:00.0", "0000:02:00.0"]


def committed():
    with open(DATA) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def rec(ts, event, who, lease=None, dur=None):
    out = {"ts": ts, "event": event, "who": who, "chips": B1}
    if lease:
        out["lease_id"] = lease
    if dur is not None:
        out["duration_s"] = dur
    return out


# ---- roster -------------------------------------------------------------

def test_roster_is_sorted_by_name_not_by_usage():
    recs = [rec("2026-10-26T16:00:00Z", "granted", "zed", "a"),
            rec("2026-10-26T16:00:10Z", "released", "zed", "a", 10.0),
            rec("2026-10-26T16:01:00Z", "granted", "zed", "b"),
            rec("2026-10-26T16:02:00Z", "released", "zed", "b", 60.0),
            rec("2026-10-26T16:03:00Z", "granted", "amy", "c")]
    rows = roster(recs)
    assert [r["who"] for r in rows] == ["amy", "zed"]   # zed has more leases but sorts last


def test_roster_sums_a_known_fixture():
    recs = [
        rec("2026-10-26T16:00:00Z", "granted", "amy", "a"),
        rec("2026-10-26T16:00:30Z", "released", "amy", "a", 30.0),
        rec("2026-10-26T16:01:00Z", "queued", "amy"),
        rec("2026-10-26T16:01:20Z", "queued", "amy"),          # second queued: wait counts from the first
        rec("2026-10-26T16:02:00Z", "granted", "amy", "b"),    # waited 60 s
        rec("2026-10-26T16:05:00Z", "released", "amy", "b", 180.0),
        rec("2026-10-26T16:00:00Z", "granted", "bob", "c"),    # never released
    ]
    assert roster(recs) == [
        {"who": "amy", "leases": 2, "held_s": 210.0, "longest_s": 180.0, "waited_s": 60},
        {"who": "bob", "leases": 1, "held_s": 0, "longest_s": 0, "waited_s": 0},
    ]


def test_roster_agrees_with_afternoon_facts_on_waits():
    recs = committed()
    expected: dict = {}
    for w in facts(recs)["waits"]:
        expected[w["who"]] = expected.get(w["who"], 0) + w["seconds"]
    assert expected, "the committed extract should contain at least one wait"
    got = {r["who"]: r["waited_s"] for r in roster(recs)}
    for who, secs in expected.items():
        assert got[who] == secs
    assert sum(got.values()) == sum(expected.values())


def test_roster_on_committed_extract_has_no_rank_fields():
    for row in roster(committed()):
        assert set(row) == {"who", "leases", "held_s", "longest_s", "waited_s"}


# ---- users.json ---------------------------------------------------------

def users():
    return load_users(USERS)


def test_users_schema():
    us = users()
    assert us
    for u in us:
        assert set(u) == {"name", "repo", "how", "evidence"}
        assert all(isinstance(v, str) and v for v in u.values())
        assert len(u["how"]) <= 140 and "\n" not in u["how"]


def test_users_are_public_github_urls():
    for u in users():
        assert u["repo"].startswith("https://github.com/")
        assert not u["repo"].endswith(".git")


def test_tt_rl_is_not_listed():
    for u in users():
        assert "tt-rl" not in (u["name"] + u["repo"] + u["evidence"]).lower()


def test_every_evidence_path_is_relative_and_nonempty():
    for u in users():
        p = u["evidence"]
        assert p.strip() and not p.startswith("/")
        assert ".." not in p.split("/")


def test_users_are_sorted_by_name():
    names = [u["name"] for u in users()]
    assert names == sorted(names, key=str.lower)


def test_users_have_no_duplicate_repos():
    repos = [u["repo"] for u in users()]
    assert len(repos) == len(set(repos))


# ---- load_users validation ---------------------------------------------

GOOD = {"name": "X", "repo": "https://github.com/a/b", "how": "Does a thing.", "evidence": "a/b.py"}


def write(tmp_path, data):
    p = tmp_path / "users.json"
    p.write_text(json.dumps(data))
    return str(p)


@pytest.mark.parametrize("bad, text", [
    ({k: v for k, v in GOOD.items() if k != "how"}, "how"),
    ({**GOOD, "how": "x" * 141}, "140"),
    ({**GOOD, "how": "two\nlines"}, "newline"),
    ({**GOOD, "repo": "http://example.com/a/b"}, "github.com"),
    ({**GOOD, "evidence": "/abs/path"}, "relative"),
    ({**GOOD, "evidence": "a/../b"}, "relative"),
    ({**GOOD, "extra": "x"}, "extra"),
])
def test_load_users_rejects_a_malformed_entry(tmp_path, bad, text):
    with pytest.raises(ValueError) as e:
        load_users(write(tmp_path, [bad]))
    assert text in str(e.value)


def test_load_users_rejects_a_non_list(tmp_path):
    with pytest.raises(ValueError):
        load_users(write(tmp_path, {"name": "X"}))


def test_load_users_accepts_a_good_entry(tmp_path):
    assert load_users(write(tmp_path, [GOOD])) == [GOOD]


def test_load_users_rejects_a_banned_phrase_in_how(tmp_path):
    entry = {**GOOD, "name": "Pat", "how": "This is what keeps the boards apart."}
    with pytest.raises(ValueError) as e:
        load_users(write(tmp_path, [GOOD, entry]))
    msg = str(e.value)
    assert "entry 1" in msg and "This is what" in msg
