"""gozer/report.py: turning history.jsonl into the GH Pages containment report.

TDD per the project convention -- written and confirmed to fail against a
not-yet-existing gozer/report.py before any implementation lands.
"""
import json

import pytest

UNIT_A = "0000046131924062"
UNIT_B = "0000046131924055"


def _events():
    return [
        {"event": "granted", "ts": "2026-09-05T00:00:00Z", "lease_id": "l1",
         "who": "claude:alpha", "units": [UNIT_A], "chips": ["0000:01:00.0", "0000:02:00.0"],
         "reason": "bring-up"},
        {"event": "released", "ts": "2026-09-05T01:00:00Z", "lease_id": "l1",
         "who": "claude:alpha", "duration_s": 3600.0},
        {"event": "granted", "ts": "2026-09-05T02:00:00Z", "lease_id": "l2",
         "who": "claude:beta", "units": [UNIT_B], "chips": ["0000:03:00.0", "0000:04:00.0"],
         "reason": "serve"},
        {"event": "reaped", "ts": "2026-09-05T02:10:00Z", "lease_id": "l2",
         "who": "claude:beta", "duration_s": 600.0, "why": "owning process is dead"},
        {"event": "queued", "ts": "2026-09-05T02:10:00Z", "who": "claude:gamma", "ticket": "abcd"},
        {"event": "granted", "ts": "2026-09-05T03:00:00Z", "lease_id": "l3",
         "who": "claude:gamma", "units": [UNIT_A], "chips": ["0000:01:00.0", "0000:02:00.0"],
         "reason": "still running"},
    ]


class TestComputeStats:
    def test_counts_grants_ghosts_and_entities(self):
        from gozer import report

        stats = report.compute_stats(_events())
        assert stats["hero"]["entities"] == 3
        assert stats["hero"]["grants"] == 3
        assert stats["hero"]["ghosts"] == 1
        assert stats["hero"]["queue_events"] == 1

    def test_reclaimed_seconds_sums_only_reaped_durations(self):
        from gozer import report

        stats = report.compute_stats(_events())
        assert stats["hero"]["reclaimed_s"] == 600.0

    def test_roster_tracks_sessions_clean_ghosts_held_and_longest(self):
        from gozer import report

        stats = report.compute_stats(_events())
        roster = {r["who"]: r for r in stats["roster"]}
        assert roster["claude:alpha"]["sessions"] == 1
        assert roster["claude:alpha"]["clean"] == 1
        assert roster["claude:alpha"]["ghosts"] == 0
        assert roster["claude:alpha"]["held_s"] == 3600.0
        assert roster["claude:alpha"]["longest_s"] == 3600.0
        assert roster["claude:beta"]["ghosts"] == 1

    def test_still_open_lease_marks_identity_active_and_excludes_from_held(self):
        from gozer import report

        stats = report.compute_stats(_events())
        roster = {r["who"]: r for r in stats["roster"]}
        assert roster["claude:gamma"]["active"] is True
        # l3 never closes, so it contributes no held_s / longest_s.
        assert roster["claude:gamma"]["held_s"] == 0.0
        assert roster["claude:gamma"]["sessions"] == 1

    def test_log_window_spans_first_to_last_event(self):
        from gozer import report

        stats = report.compute_stats(_events())
        assert stats["window"]["start"] == "2026-09-05T00:00:00Z"
        assert stats["window"]["end"] == "2026-09-05T03:00:00Z"

    def test_empty_events_raises_value_error(self):
        from gozer import report

        with pytest.raises(ValueError):
            report.compute_stats([])


class TestDeriveBoardMap:
    def test_recovers_unit_to_chips_from_single_unit_events(self):
        from gozer import report

        board_map = report.derive_board_map(_events())
        assert board_map[UNIT_A] == ["0000:01:00.0", "0000:02:00.0"]
        assert board_map[UNIT_B] == ["0000:03:00.0", "0000:04:00.0"]

    def test_multi_unit_events_are_skipped(self):
        from gozer import report

        events = _events() + [{
            "event": "granted", "ts": "2026-09-05T04:00:00Z", "lease_id": "l4",
            "who": "claude:delta", "units": [UNIT_A, UNIT_B],
            "chips": ["0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "0000:04:00.0"],
        }]
        board_map = report.derive_board_map(events)
        assert set(board_map.keys()) == {UNIT_A, UNIT_B}


class TestAssignPalette:
    def test_same_identity_set_gets_same_colors_across_calls(self):
        from gozer import report

        identities = ["claude:alpha", "claude:beta", "claude:gamma"]
        first = report.assign_palette(identities)
        second = report.assign_palette(list(reversed(identities)))
        assert first == second

    def test_every_identity_gets_a_hex_color(self):
        from gozer import report

        palette = report.assign_palette(["claude:alpha", "claude:beta"])
        for color in palette.values():
            assert color.startswith("#")


class TestRender:
    def test_render_embeds_every_event_with_theme_in_place_of_reason(self):
        from gozer import report

        html = report.render(_events())
        start = html.index('<script type="application/json" id="gozer-events">')
        end = html.index("</script>", start)
        blob = json.loads(html[start:end].split(">", 1)[1])
        assert len(blob) == len(_events())
        assert [e.get("lease_id") for e in blob] == [e.get("lease_id") for e in _events()]

    def test_render_contains_computed_hero_numbers(self):
        from gozer import report

        html = report.render(_events())
        assert ">3<" in html  # entities and grants both compute to 3
        assert ">1<" in html  # ghosts / queue events

    def test_render_raises_on_empty_events(self):
        from gozer import report

        with pytest.raises(ValueError):
            report.render([])


class TestThemeForReason:
    def test_serving_a_demo(self):
        from gozer import report

        assert report.theme_for_reason("gradio demo") == "serving a demo"
        assert report.theme_for_reason(
            "serve tt-tnt v6 thin bundle for hardware verification") == "serving a demo"

    def test_verification_and_testing(self):
        from gozer import report

        assert report.theme_for_reason("verify rotary_embedding_llama matches convention") == "verification / testing"
        assert report.theme_for_reason("TDD: test_tt_inference should fail before implementation") == "verification / testing"
        assert report.theme_for_reason("Stage 0 end-to-end sanity check") == "verification / testing"

    def test_debugging(self):
        from gozer import report

        assert report.theme_for_reason("debug url_ok launch failure") == "debugging"
        assert report.theme_for_reason("trace bool schema origin") == "debugging"

    def test_benchmarking_and_optimization(self):
        from gozer import report

        assert report.theme_for_reason("bf16 precision optimization pass") == "benchmarking / optimization"
        assert report.theme_for_reason("benchmark tt-tnt decode throughput") == "benchmarking / optimization"

    def test_bring_up(self):
        from gozer import report

        assert report.theme_for_reason("V-JEPA2 functional decoder bring-up") == "bring-up"

    def test_build_and_packaging(self):
        from gozer import report

        assert report.theme_for_reason("rebuild+validate Dockerfile.qb2 against real hardware") == "build / packaging"

    def test_unmatched_reason_falls_back_to_general_work(self):
        from gozer import report

        assert report.theme_for_reason("something entirely novel happening here") == "general work"

    def test_missing_reason_falls_back_to_general_work(self):
        from gozer import report

        assert report.theme_for_reason(None) == "general work"


class TestRenderRedactsReasons:
    def test_embedded_events_carry_themes_not_raw_reasons(self):
        from gozer import report

        events = _events()
        events[0]["reason"] = "a very specific internal project codename and plan"
        html = report.render(events)
        assert "a very specific internal project codename and plan" not in html

    def test_embedded_events_still_carry_a_reason_field(self):
        from gozer import report

        html = report.render(_events())
        start = html.index('<script type="application/json" id="gozer-events">')
        end = html.index("</script>", start)
        blob = json.loads(html[start:end].split(">", 1)[1])
        reasons = {e.get("reason") for e in blob if e.get("event") == "granted"}
        assert reasons == {"bring-up", "serving a demo", "general work"}
