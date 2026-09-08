"""Turn history.jsonl into the GH Pages containment report.

`docs/index.html` used to be a hand-authored snapshot: someone read
`history.jsonl` on a given day, hand-computed a roster table, and hand-typed
hero stats and prose straight into the HTML. That page goes stale the moment
a new event is logged and can only be refreshed by repeating the same manual
transcription.

This module computes everything that page used to have typed in by hand --
per-identity roster stats, headline hero numbers, the unit-to-chips board
map, and a stable color per identity -- and renders it into
`report_template.html`, whose CSS/JS/structure is otherwise untouched. The
client-side JS in that template already derives the timeline and incident
log from the full embedded event list, so widening that list is enough for
it to pick up every day on its own.
"""

from __future__ import annotations

import hashlib
import json
import os

# The 7 hand-picked green shades from the original hand-authored page, reused
# as the base palette so re-running the report doesn't invent new colors.
_PALETTE_COLORS = [
    "#45ff7a", "#8dffb0", "#31c866", "#7fe6a0",
    "#1f9c50", "#b7ffce", "#3fae66",
]

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "report_template.html")

# Ordered (label, keywords) pairs -- first match wins. This is what stands
# between the published page and leaking the literal --reason text of every
# lease this box has ever taken, which can carry internal project names,
# codenames, or task specifics that have no business on a public GH Pages
# site. The report should be able to say "a lot of this box's time went to
# verification work" without saying what was being verified.
_REASON_THEMES = [
    ("serving a demo", ("gradio", "demo", "serve", "asgi", "container")),
    ("verification / testing", ("verify", "verif", "test", "tdd", "sanity", "suite", "confirm", "check")),
    ("debugging", ("debug", "trace", "offending", "diff", "fix")),
    ("benchmarking / optimization", ("benchmark", "optimiz", "throughput", "precision", "measure")),
    ("bring-up", ("bring-up", "bringup")),
    ("build / packaging", ("build", "dockerfile", "packag", "rebuild")),
    ("exploration", ("explore", "plan")),
]


def theme_for_reason(reason: str | None) -> str:
    """Map a free-text --reason string to a small, public-safe theme label.

    Order matters: the first matching theme wins, so more specific themes
    (e.g. "serving a demo") are checked before broader ones that might also
    match the same words.
    """
    if not reason:
        return "general work"
    lowered = reason.lower()
    for label, keywords in _REASON_THEMES:
        if any(kw in lowered for kw in keywords):
            return label
    return "general work"


def compute_stats(events: list[dict]) -> dict:
    """Compute every number the report needs from a raw event list.

    Raises ValueError on an empty event list -- there is nothing to report
    on GOZER_ROOT's freshly-created history, and rendering a page with no
    data is a worse outcome than refusing.
    """
    if not events:
        raise ValueError("no events to report on")

    roster: dict[str, dict] = {}
    open_leases: dict[str, dict] = {}
    grants = 0
    ghosts = 0
    reclaimed_s = 0.0
    queue_events = 0

    def entry(who: str) -> dict:
        return roster.setdefault(who, {
            "who": who, "sessions": 0, "clean": 0, "ghosts": 0,
            "held_s": 0.0, "longest_s": 0.0, "active": False,
        })

    for e in events:
        kind = e.get("event")
        who = e.get("who")
        if kind == "granted":
            grants += 1
            entry(who)["sessions"] += 1
            open_leases[e["lease_id"]] = e
        elif kind == "queued":
            queue_events += 1
            entry(who)
        elif kind in ("released", "reaped"):
            g = open_leases.pop(e.get("lease_id"), None)
            duration = e.get("duration_s") or 0.0
            r = entry(who)
            if kind == "reaped":
                ghosts += 1
                r["ghosts"] += 1
                reclaimed_s += duration
            else:
                r["clean"] += 1
            r["held_s"] += duration
            r["longest_s"] = max(r["longest_s"], duration)

    for lid, g in open_leases.items():
        entry(g.get("who"))["active"] = True

    total_held_s = sum(r["held_s"] for r in roster.values())

    return {
        "window": {"start": events[0]["ts"], "end": events[-1]["ts"]},
        "hero": {
            "entities": len(roster),
            "grants": grants,
            "ghosts": ghosts,
            "reclaimed_s": reclaimed_s,
            "queue_events": queue_events,
            "total_held_s": total_held_s,
        },
        "roster": sorted(roster.values(), key=lambda r: r["held_s"], reverse=True),
    }


def derive_board_map(events: list[dict]) -> dict[str, list[str]]:
    """Recover unit -> chips from single-unit events.

    The log never states the mapping directly, but a single-unit event
    carries both `units: [X]` and the chips leased alongside it -- folding
    those pairs across the log recovers it without hardcoding board serials
    or PCI addresses for one particular box. Multi-unit events can't
    disambiguate which chip belongs to which unit, so they're skipped.
    """
    board_map: dict[str, list[str]] = {}
    for e in events:
        units = e.get("units")
        chips = e.get("chips")
        if not units or not chips or len(units) != 1:
            continue
        board_map.setdefault(units[0], chips)
    return board_map


def assign_palette(identities: list[str]) -> dict[str, str]:
    """Stable, deterministic color per identity.

    Hashing rather than insertion order means the same identity always gets
    the same color across separate report runs, regardless of what order
    events happen to appear in.
    """
    palette = {}
    for who in sorted(set(identities)):
        digest = hashlib.sha256(who.encode("utf-8")).digest()
        palette[who] = _PALETTE_COLORS[digest[0] % len(_PALETTE_COLORS)]
    return palette


def _fmt_hours(seconds: float) -> str:
    return f"{seconds / 3600:.2f}h"


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{round(seconds)}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _redact_reasons(events: list[dict]) -> list[dict]:
    """Replace each event's free-text `reason` with its theme.

    This is the only thing that reaches the published page's embedded JSON
    (and therefore the timeline tooltips and incident cards the client-side
    JS builds from it) -- everything else in an event (who, chips, units,
    timestamps, durations) is operational metadata, not prompt content.
    """
    redacted = []
    for e in events:
        if "reason" in e:
            e = {**e, "reason": theme_for_reason(e["reason"])}
        redacted.append(e)
    return redacted


def render(events: list[dict], *, source_note: str | None = None) -> str:
    """Render the full containment-report HTML page."""
    stats = compute_stats(events)
    board_map = derive_board_map(events)
    identities = [r["who"] for r in stats["roster"]]
    palette = assign_palette(identities)

    hero = stats["hero"]
    pct_reclaimed = (
        hero["reclaimed_s"] / hero["total_held_s"] * 100
        if hero["total_held_s"] else 0.0
    )

    units = sorted(board_map.keys())
    lane_labels = []
    for u in units:
        chips = board_map[u]
        short = u[-3:]
        chip_nums = "/".join(c.split(":")[1] for c in chips) if chips else "?"
        lane_labels.append(f"BOARD {short} — chips {chip_nums}")
    unit_lane = {u: i for i, u in enumerate(units)}

    start, end = stats["window"]["start"], stats["window"]["end"]
    from datetime import datetime, timezone
    start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    elapsed = end_dt - start_dt
    elapsed_h = elapsed.total_seconds() / 3600
    h = int(elapsed_h)
    m = int(round((elapsed_h - h) * 60))

    n_entities = hero["entities"]
    n_boards = len(units)
    n_chips = sum(len(c) for c in board_map.values())

    lede = (
        f"{n_boards} Blackhole board{'s' if n_boards != 1 else ''}, "
        f"{n_chips} chip{'s' if n_chips != 1 else ''}, one lock directory. "
        "<code>tt-gozer</code> is the thing that stands between whichever agent "
        "asks first and whoever's already running — it hands out chip leases, "
        "queues the rest, and quietly reclaims anything a dead process left "
        "pinned to the hardware. This is its own ledger read back: every grant, "
        "release, queue and reap it logged this period, while "
        f"{n_entities} {'unrelated session' if n_entities != 1 else 'session'}"
        f"{'s' if n_entities != 1 else ''} took turns on the same "
        f"{'boards' if n_boards != 1 else 'board'} without a person ever refereeing."
    )

    pull = (
        f"<b>{pct_reclaimed:.1f}%</b> of every chip-hour this ledger closed out "
        "this period belonged to a session that never called <code>release</code> "
        "itself. gozer's grace-window timer called it for them."
    )

    roster_note = (
        f"{n_entities} name{'s' if n_entities != 1 else ''} shared "
        f"{n_boards} board{'s' if n_boards != 1 else ''} this period."
    )

    timeline_note = "; ".join(
        f"Board {u[-3:]} carries chips <code>{'</code>/<code>'.join(c.split(':')[1] for c in board_map[u])}</code>"
        for u in units
    ) + ". Solid bars are clean handoffs. Hazard-striped bars are the ones gozer had to reclaim on its own."

    log_note = (
        f"{hero['ghosts']} session{'s' if hero['ghosts'] != 1 else ''} never called it quits. "
        "Their owning processes died, detached, or just wandered off — each left a lease "
        "pinned to a chip until gozer's own grace-window timer noticed and pulled it. "
        "No one ran <code>tt-smi -r</code> by hand."
    )

    incident_note = (
        f"{hero['queue_events']} queue event{'s' if hero['queue_events'] != 1 else ''} "
        "occurred this period -- see the containment log above for the reclaimed leases."
    )

    meta_description = (
        f"A {h}-hour ledger of tt-gozer arbitrating {n_boards} shared Blackhole "
        f"board{'s' if n_boards != 1 else ''} for {n_entities} agent session"
        f"{'s' if n_entities != 1 else ''} — who held what, who forgot to hang up, "
        "and what gozer reclaimed without anyone asking it to."
    )
    meta_og = (
        f"{h} hours, {n_boards} boards, {n_entities} agent identities, "
        f"{hero['ghosts']} zombie leases reclaimed automatically. tt-gozer's own "
        "history.jsonl, read back as a mainframe accounting report."
    )

    roster_rows = [
        {
            "who": r["who"], "sessions": r["sessions"], "clean": r["clean"],
            "ghosts": r["ghosts"], "held": _fmt_hours(r["held_s"]),
            "longest": _fmt_hours(r["longest_s"]), "active": r["active"],
        }
        for r in stats["roster"]
    ]

    with open(_TEMPLATE_PATH) as f:
        template = f.read()

    substitutions = {
        "{{REPORT_PERIOD}}": f"{start[:10]} → {end[:10]}",
        "{{BOOT_NODE_LINE}}": f"BOARDS {n_boards} · CHIPS {n_chips} · ROOT {source_note or '/tmp/tt-gozer'}",
        "{{BOOT_WINDOW_LINE}}": f"WINDOW {start} .. {end}  ({h}h {m:02d}m)",
        "{{LEDE}}": lede,
        "{{HERO_ENTITIES}}": str(n_entities),
        "{{HERO_GRANTS}}": str(hero["grants"]),
        "{{HERO_GHOSTS}}": str(hero["ghosts"]),
        "{{HERO_RECLAIMED}}": _fmt_hours(hero["reclaimed_s"]),
        "{{HERO_QUEUE_COUNT}}": str(hero["queue_events"]),
        "{{HERO_QUEUE_LABEL}}": "Queue events" if hero["queue_events"] != 1 else "Queue event",
        "{{HERO_BOARDS_CHIPS}}": f"{n_boards}/{n_chips}",
        "{{PULL_QUOTE}}": pull,
        "{{ROSTER_NOTE}}": roster_note,
        "{{TIMELINE_TITLE}}": f"{h} Hours",
        "{{TIMELINE_NOTE}}": timeline_note,
        "{{LOG_NOTE}}": log_note,
        "{{INCIDENT_NOTE}}": incident_note,
        "{{HISTORY_PATH}}": os.path.join(source_note or "/tmp/tt-gozer", "history.jsonl"),
        "{{COMPILED_DATE}}": end[:10],
        "{{META_DESCRIPTION}}": meta_description,
        "{{META_OG_DESCRIPTION}}": meta_og,
        "{{EVENTS_JSON}}": json.dumps(_redact_reasons(events)),
        "{{PALETTE_JSON}}": json.dumps(palette),
        "{{ROSTER_JSON}}": json.dumps(roster_rows),
        "{{UNIT_LANE_JSON}}": json.dumps(unit_lane),
        "{{LANE_LABEL_JSON}}": json.dumps(lane_labels),
    }
    for token, value in substitutions.items():
        template = template.replace(token, value)
    return template
