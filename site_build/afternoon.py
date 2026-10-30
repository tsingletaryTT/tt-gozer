# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""The example afternoon: a real slice of a gozer history log.

`extract()` cuts a window out of the log and keeps only the fields the page
needs (no reasons, pids or other free text). `facts()` computes the numbers
the page quotes, `captions()` words them, and `lanes_html()` draws the
timeline as a self-contained HTML fragment.

Timestamps are UTC strings like `2026-10-26T16:00:00Z`; they sort as text.
"""
import html
import statistics
from datetime import datetime, timezone

KEEP_KEYS = ("ts", "event", "who", "chips", "lease_id", "ticket", "duration_s")
RAPID_UNDER_S = 120   # a lease shorter than this counts as a rapid cycle
RAPID_MIN_COUNT = 3   # fewer rapid leases than this is not worth reporting

# Block colours: the brand family (blues, teals, greens, yellow, orange-red)
# plus a brown and greys, so each agent in a busy afternoon gets its own colour.
# Each entry is chosen so that either dark or white text reaches 4.5:1 on it.
PALETTE = [
    "#2AA3C8", "#1A7A9C", "#136B85", "#74C5DF", "#6FABA0", "#3F7A70",
    "#A7D8C9", "#F6BC42", "#E8743B", "#C93A18", "#8C5A2B", "#5C6B6A", "#B9C4C3",
]
DARK_TEXT, LIGHT_TEXT = "#092221", "#FFFFFF"


def _luminance(hex_colour: str) -> float:
    """WCAG relative luminance of a #RRGGBB colour."""
    chans = []
    for i in (1, 3, 5):
        c = int(hex_colour[i:i + 2], 16) / 255
        chans.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


def contrast(bg: str, fg: str) -> float:
    """WCAG contrast ratio between two #RRGGBB colours."""
    hi, lo = sorted((_luminance(bg), _luminance(fg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def text_colour(bg: str) -> str:
    """Whichever of dark or white text has the higher contrast on `bg`."""
    return max((DARK_TEXT, LIGHT_TEXT), key=lambda fg: contrast(bg, fg))


def _slim(e: dict) -> dict:
    return {k: e[k] for k in KEEP_KEYS if k in e}


def extract(events: list[dict], start: str, end: str) -> list[dict]:
    """Granted and queued events in [start, end], plus the releases of those grants."""
    granted_ids = {e["lease_id"] for e in events
                   if e.get("event") == "granted" and e.get("lease_id")
                   and start <= e["ts"] <= end}
    out = []
    for e in events:
        kind = e.get("event")
        if kind in ("granted", "queued") and start <= e["ts"] <= end:
            out.append(_slim(e))
        elif kind == "released" and e.get("lease_id") and e["lease_id"] in granted_ids:
            out.append(_slim(e))
    return sorted(out, key=lambda r: r["ts"])


def _epoch(ts: str) -> int:
    return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).timestamp())


def facts(records: list[dict]) -> dict:
    """Numbers computed from the records only. Optional keys are absent when the data has none."""
    recs = sorted(records, key=lambda r: r["ts"])
    out: dict = {"waits": [], "same_second_handoffs": 0}

    # Rapid cycles: the agent with the most short leases.
    short: dict[str, list[float]] = {}
    for r in recs:
        if r["event"] == "released" and r.get("duration_s", 1e9) < RAPID_UNDER_S:
            short.setdefault(r["who"], []).append(r["duration_s"])
    cands = [(len(d), w) for w, d in short.items() if len(d) >= RAPID_MIN_COUNT]
    if cands:
        n, who = max(cands)
        out["rapid_cycles"] = {"who": who, "count": n,
                               "typical_s": int(round(statistics.median(short[who])))}

    held = [r for r in recs if r["event"] == "released" and "duration_s" in r]
    if held:
        top = max(held, key=lambda r: r["duration_s"])
        out["longest_hold"] = {"who": top["who"], "seconds": int(top["duration_s"])}

    # Waits and handoffs, walking the log once.
    waiting: dict[str, str] = {}              # who -> ts of first unsatisfied queued
    last_release: list[tuple[str, set]] = []  # (ts, chips) in order
    gaps: list[int] = []
    waits = []
    for r in recs:
        who = r["who"]
        if r["event"] == "queued":
            waiting.setdefault(who, r["ts"])
        elif r["event"] == "released":
            waiting.pop(who, None)
            last_release.append((r["ts"], set(r.get("chips", []))))
        elif r["event"] == "granted" and who in waiting:
            waits.append({"who": who, "seconds": _epoch(r["ts"]) - _epoch(waiting.pop(who))})
            chips = set(r.get("chips", []))
            prior = [t for t, c in last_release if t <= r["ts"] and c & chips]
            if prior:
                gaps.append(_epoch(r["ts"]) - _epoch(max(prior)))
    out["waits"] = sorted(waits, key=lambda w: (w["who"], w["seconds"]))
    # Fastest handoff is the smallest gap above zero; gaps of zero are counted
    # separately as same-second handoffs.
    positive = [g for g in gaps if g > 0]
    out["fastest_handoff_s"] = min(positive) if positive else None
    out["same_second_handoffs"] = sum(1 for g in gaps if g == 0)
    return out


def _dur(seconds: int) -> str:
    """'47 seconds', '34 minutes', '4 hours 5 minutes'."""
    s = int(seconds)
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    m = round(s / 60)
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, m = divmod(m, 60)
    text = f"{h} hour{'s' if h != 1 else ''}"
    if m:
        text += f" {m} minute{'s' if m != 1 else ''}"
    return text


def captions(f: dict) -> list[str]:
    """Plain sentences built from `facts` only."""
    out = []
    if "longest_hold" in f:
        h = f["longest_hold"]
        out.append(f"{h['who']} held a board for {_dur(h['seconds'])}, the longest lease in this log.")
    if "rapid_cycles" in f:
        c = f["rapid_cycles"]
        out.append(f"{c['who']} took and released a board {c['count']} times, "
                   f"a typical lease lasting about {_dur(c['typical_s'])}.")
    for w in f.get("waits", []):
        out.append(f"{w['who']} queued and waited {_dur(w['seconds'])} for a board.")
    if f.get("fastest_handoff_s") is not None:
        out.append(f"The fastest handoff from a release to the next waiting agent "
                   f"took {_dur(f['fastest_handoff_s'])}.")
    n = f.get("same_second_handoffs", 0)
    if n:
        out.append(f"{n} waiting agent{'s' if n != 1 else ''} got a board in the same second it was released.")
    return out


def _board(bdf: str) -> int:
    """Board index from a 0000:NN:00.0 address: (bus - 1) // 2.

    Assumption: chips sit on consecutive bus numbers starting at 1, two per
    board (as in the logged box: buses 01/02 are board 1, 03/04 are board 2).
    """
    try:
        return (int(bdf.split(":")[1], 16) - 1) // 2
    except (IndexError, ValueError):
        return 0


def _leases(records: list[dict]) -> list[dict]:
    rel = {r["lease_id"]: r for r in records if r["event"] == "released"}
    leases = []
    for r in records:
        if r["event"] != "granted":
            continue
        x = rel.get(r["lease_id"])
        leases.append({"who": r["who"], "start": r["ts"], "end": x["ts"] if x else None,
                       "board": _board((r.get("chips") or ["0000:01:00.0"])[0])})
    return leases


def lanes_html(records: list[dict], start: str, end: str) -> str:
    """Self-contained timeline fragment: one row per board, one block per lease."""
    leases = _leases(records)
    if not leases:
        return '<p class="lanes-empty">No leases were granted in this window.</p>'
    t0, t1 = _epoch(start), _epoch(end)
    span = max(t1 - t0, 1)
    who_list = sorted({l["who"] for l in leases})
    colour = {w: (PALETTE[i % len(PALETTE)], text_colour(PALETTE[i % len(PALETTE)]))
              for i, w in enumerate(who_list)}
    esc = html.escape

    rows = []
    for b in sorted({l["board"] for l in leases}):
        blocks = []
        for l in (x for x in leases if x["board"] == b):
            s = max(_epoch(l["start"]), t0)
            if l["end"] is None:
                e, tag = t1, "no release recorded"
            elif _epoch(l["end"]) > t1:
                e, tag = t1, "continues"
            else:
                e, tag = _epoch(l["end"]), ""
            left = 100.0 * (s - t0) / span
            width = max(100.0 * (e - s) / span, 0.3)
            bg, fg = colour[l["who"]]
            title = f"{l['who']}, {l['start']} to {l['end'] or 'now'}"
            if tag:
                title += f" ({tag})"
            label = esc(l["who"]) + (f" ({tag})" if tag else "")
            blocks.append(
                f'<div class="lease" style="position:absolute;top:2px;bottom:2px;'
                f'left:{left:.2f}%;width:{width:.2f}%;background:{bg};color:{fg};'
                f'overflow:hidden;white-space:nowrap;font-size:11px;padding:0 3px;'
                f'box-sizing:border-box;border-radius:2px" title="{esc(title, quote=True)}">'
                f'{label}</div>')
        rows.append(
            f'<div class="lane" style="display:flex;align-items:center;gap:8px;margin:4px 0">'
            f'<span style="flex:0 0 64px;font-size:12px">Board {b + 1}</span>'
            f'<div style="position:relative;flex:1 1 auto;height:28px;background:#DCF0EE;'
            f'border-radius:3px">{"".join(blocks)}</div></div>')

    legend = "".join(
        f'<li style="display:inline-block;margin:2px 12px 2px 0;font-size:12px">'
        f'<span style="display:inline-block;width:12px;height:12px;vertical-align:middle;'
        f'margin-right:4px;background:{colour[w][0]}"></span>{esc(w)}</li>'
        for w in who_list)
    axis = (f'<div style="font-size:11px;display:flex;justify-content:space-between;'
            f'margin-left:72px"><span>{esc(start)}</span><span>{esc(end)}</span></div>')
    return (f'<div class="lanes" style="width:100%">{"".join(rows)}{axis}'
            f'<ul class="legend" style="list-style:none;padding:0;margin:8px 0 0">{legend}</ul></div>')
