# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Build the explainer page: one self-contained HTML file.

`build(out_path)` fills `site/index.template.html` with:

* the real terminal session from `transcripts.run_session()`, each step
  beside its note from `ANNOTATIONS`;
* the example afternoon (timeline and captions) from the committed extract
  `site/data/afternoon-2026-10-26.jsonl`;
* the example roster from the same extract;
* the list of projects from `site/data/users.json`;
* `site/style.css`, inlined into a <style> block.

Python standard library only. The template uses `string.Template` tokens.
Every interpolated value is escaped with `html.escape`, except the HTML this
module generates itself (step blocks, list items, table, diagram) and the
fragment from `afternoon.lanes_html`, which escapes its own values.

The output depends only on files in the repo and on gozer's own output
against a fake box: no clock reading, no host state. Two builds give the
same bytes.
"""
import html
import json
import os
import re
from string import Template

from gozer import __version__
from site_build.afternoon import _epoch, captions, facts, lanes_html
from site_build.roster import load_users, roster
from site_build.transcripts import run_session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
TEMPLATE = os.path.join(SITE, "index.template.html")
STYLE = os.path.join(SITE, "style.css")
AFTERNOON = os.path.join(SITE, "data", "afternoon-2026-10-26.jsonl")
USERS = os.path.join(SITE, "data", "users.json")

# The window the extract was cut to (scripts/make_afternoon_extract.py).
WINDOW_START, WINDOW_END = "2026-10-26T16:00:00Z", "2026-10-26T20:30:00Z"

# Phrases the plain-English rules (global CLAUDE.md) keep out of the prose
# written for this page. tests/test_site_build.py sweeps the page's visible
# prose (terminal blocks and data regions excluded) for each of them.
SWEEP = (
    ", not ", "rather than", "instead of", "reads as", "honest",
    "the one that", "which is why", "This is what", "load-bearing",
    "open the door", "carries the story", "on-ramp", "the tell",
)

# The note shown beside each step of the session, keyed by Step.note_key.
# Each one says what happened in that step's real output and why it matters,
# for a reader who has never heard of gozer. Keep them true to the output:
# tests/test_site_build.py checks the outputs are on the page, and a reader
# checks these notes against them.
ANNOTATIONS: dict[str, str] = {
    "first-grant": (
        "The first agent asks for two chips. gozer gives it a whole board, chips 0 "
        "and 1, and prints the `TT_VISIBLE_DEVICES` line that points the Tenstorrent "
        "runtime at those chips. `--owner-pid $$` ties the lease to this shell, so "
        "it lasts as long as the shell does."
    ),
    "second-grant": (
        "A second agent asks for two chips while the first is still working. It "
        "gets the other board, chips 2 and 3. The two agents never had to talk to "
        "each other."
    ),
    "queued": (
        "A third agent asks, and both boards are taken. It gets a ticket and "
        "first place in the queue. Exit code 10 means queued, so the agent knows "
        "to go on with work that needs no chips and come back later."
    ),
    "status-full": (
        "`gozer status` shows the whole box: which agent holds each chip, and who "
        "is waiting. `CLAIMED` means a lease covers the chip and no process has "
        "opened it yet. Once one does, the state becomes `HELD`."
    ),
    "release": (
        "The first agent is done and releases its lease. gozer resets the chips "
        "so the next user starts from a clean state, and keeps the freed board "
        "for claude:sdxl, the agent at the front of the queue."
    ),
    "wait-grant": (
        "The queued agent comes back and runs `gozer wait` with its ticket. It "
        "gets chips 0 and 1 under a new lease of its own."
    ),
    "status-after": (
        "Now claude:sdxl holds the first board and claude:eval still holds the "
        "second. Three agents have had chips on a box with room for two at a time."
    ),
    "release-last": (
        "claude:sdxl finishes and releases. The chips are reset, and the board "
        "is free for whoever asks next."
    ),
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _records() -> list[dict]:
    with open(AFTERNOON, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _duration(seconds: float) -> str:
    """Compact table duration: '46 s', '3 min 22 s', '4 h 5 min', 'none' for 0."""
    s = int(round(seconds))
    if s <= 0:
        return "none"
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def _note_html(text: str) -> str:
    """Escape a note, then turn `backticked` spans into <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text, quote=False))


def _steps_html() -> str:
    """One <li> per session step: the terminal block, then its note."""
    esc = html.escape
    items = []
    for i, s in enumerate(run_session(), 1):
        if s.note_key not in ANNOTATIONS:
            raise KeyError(f"no annotation for session step {s.note_key!r}")
        body = (f'<span class="prompt">$ </span><span class="cmd">{esc(s.command, quote=False)}</span>'
                f'\n{esc(s.output, quote=False)}')
        if s.exit_code:
            body += f'\n<span class="exit">exit {int(s.exit_code)}</span>'
        items.append(
            f'      <li class="step">\n'
            f'<pre class="term">{body}</pre>\n'
            f'        <div class="note"><span class="num">Step {i}</span>'
            f'<p>{_note_html(ANNOTATIONS[s.note_key])}</p></div>\n'
            f'      </li>')
    return "\n".join(items)


def _hours_html(start: str, end: str) -> str:
    """Hour marks under the timeline, placed at their share of the window."""
    t0, t1 = _epoch(start), _epoch(end)
    span = t1 - t0
    marks = []
    t = t0 - t0 % 3600 + (3600 if t0 % 3600 else 0)   # first whole hour in the window
    while t <= t1:
        hh = (t // 3600) % 24
        marks.append((100.0 * (t - t0) / span, f"{hh:02d}:00"))
        t += 3600
    if t1 % 3600:
        mm = (t1 // 60) % 60
        marks.append((100.0, f"{(t1 // 3600) % 24:02d}:{mm:02d}"))
    out = []
    for left, label in marks:
        shift = "0" if left == 0 else ("-100%" if left == 100 else "-50%")
        out.append(f'<span style="position:absolute;left:{left:.2f}%;'
                   f'transform:translateX({shift})">{label}</span>')
    return "".join(out)


def _timeline_html(records: list[dict]) -> tuple[str, str]:
    """`lanes_html` split into (board rows with hour marks, legend).

    `lanes_html` is trusted: it escapes its own values. The legend is moved
    out of the timeline so that, on a phone, it wraps at the screen width while
    the timeline scrolls sideways in its frame. The hour marks go under the
    board rows; the stylesheet hides the fragment's own start/end timestamp
    line, which they replace.
    """
    frag = lanes_html(records, WINDOW_START, WINDOW_END)
    marker, close = '<ul class="legend"', "</div>"
    if marker not in frag or not frag.endswith(close):
        return frag, ""          # e.g. "no leases" paragraph: nothing to split
    i = frag.index(marker)
    hours = f'<div class="hours" aria-hidden="true">{_hours_html(WINDOW_START, WINDOW_END)}</div>'
    return frag[:i] + hours + close, frag[i:-len(close)]


def _who_cell(who: str) -> str:
    """Agent name for the table, in nowrap parts split after each colon.

    On a phone the stylesheet stacks the parts (`claude:` above the rest), so
    a name never breaks at a hyphen.
    """
    parts = html.escape(who).split(":")
    return "".join(f'<span class="nw">{p}{":" if i < len(parts) - 1 else ""}</span>'
                        for i, p in enumerate(parts))


def _cell_duration(seconds: float) -> str:
    """`_duration` with each number kept beside its unit (no-break spaces)."""
    words = _duration(seconds).split(" ")
    return " ".join(f"{words[i]}\u00a0{words[i + 1]}" for i in range(0, len(words) - 1, 2)) \
        if len(words) > 1 else words[0]


def _roster_html(records: list[dict]) -> str:
    esc = html.escape
    rows = []
    for r in roster(records):
        rows.append(
            f'<tr><td class="who">{_who_cell(r["who"])}</td><td class="n">{int(r["leases"])}</td>'
            f'<td class="n">{_cell_duration(r["held_s"])}</td><td class="n">{_cell_duration(r["longest_s"])}</td>'
            f'<td class="n">{_cell_duration(r["waited_s"])}</td></tr>')
    return ('<table class="roster">\n<caption class="small">Example: leases on one box, '
            '26 October 2026, 16:00 to 20:30 UTC</caption>\n'
            '<thead><tr><th scope="col">Agent</th><th scope="col" class="n">Leases</th>'
            '<th scope="col" class="n">Time held</th><th scope="col" class="n">Longest</th>'
            '<th scope="col" class="n">Waited</th></tr></thead>\n<tbody>\n'
            + "\n".join(rows) + "\n</tbody>\n</table>")


def _users_html(users: list[dict]) -> str:
    esc = html.escape
    return "\n".join(
        f'      <li><a href="{esc(u["repo"], quote=True)}">{esc(u["name"])}</a>'
        f'<br>{esc(u["how"], quote=False)}</li>'
        for u in users)


# The "how it works" diagram. Drawn at its natural size (360 x 616 user units
# = CSS pixels at desktop width), so every label is at least 15px there. On a
# 390px phone it scales to about 94%, so the smallest label stays near 14px.
DIAGRAM = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 616" width="360" height="616" role="img" aria-labelledby="how-diagram-title how-diagram-desc">
<title id="how-diagram-title">How a request for chips is handled</title>
<desc id="how-diagram-desc">An agent runs gozer acquire. If a whole board is free, gozer grants a lease on it. If not, the agent gets a ticket in the queue and keeps working, and gozer wait hands it a board when one is released. Either way the agent then runs on its chips. gozer treats a chip as in use when its device file is open; the lease record only says who asked.</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#092221"/></marker></defs>
<g font-family="system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif" fill="#092221" stroke="none" text-anchor="middle">
  <rect x="30" y="4" width="300" height="64" rx="6" fill="#FFFFFF" stroke="#092221" stroke-width="2"/>
  <text x="180" y="30" font-size="16">An agent asks for two chips</text>
  <text x="180" y="54" font-size="15" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">gozer acquire --chips 2</text>
  <rect x="40" y="104" width="280" height="44" rx="22" fill="#DCF0EE" stroke="#092221" stroke-width="2"/>
  <text x="180" y="132" font-size="16" font-weight="700">Is a whole board free?</text>
  <rect x="4" y="196" width="168" height="100" rx="6" fill="#6FABA0" stroke="#092221" stroke-width="2"/>
  <text x="88" y="224" font-size="17" font-weight="700">Lease</text>
  <text x="88" y="250" font-size="15">gozer grants the</text>
  <text x="88" y="272" font-size="15">whole board</text>
  <rect x="188" y="196" width="168" height="100" rx="6" fill="#F6BC42" stroke="#092221" stroke-width="2"/>
  <text x="272" y="224" font-size="17" font-weight="700">Ticket</text>
  <text x="272" y="250" font-size="15">a place in the queue;</text>
  <text x="272" y="272" font-size="15">keep working</text>
  <rect x="188" y="334" width="168" height="84" rx="6" fill="#FFFFFF" stroke="#092221" stroke-width="2"/>
  <text x="272" y="360" font-size="15" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">gozer wait</text>
  <text x="272" y="384" font-size="15">hands over a board</text>
  <text x="272" y="406" font-size="15">when one is released</text>
  <rect x="4" y="456" width="352" height="48" rx="6" fill="#FFFFFF" stroke="#092221" stroke-width="2"/>
  <text x="180" y="486" font-size="16">The agent runs on its chips</text>
  <rect x="4" y="532" width="352" height="80" rx="6" fill="#74C5DF" stroke="#092221" stroke-width="2"/>
  <text x="180" y="557" font-size="16" font-weight="700">Is the chip really in use?</text>
  <text x="180" y="580" font-size="15">An open device file proves it.</text>
  <text x="180" y="601" font-size="15">The lease record only says who asked.</text>
  <g stroke="#092221" stroke-width="2" fill="none" marker-end="url(#arrow)">
    <path d="M180 68 V102"/>
    <path d="M110 148 V170 H88 V194"/>
    <path d="M250 148 V170 H272 V194"/>
    <path d="M272 296 V332"/>
    <path d="M88 296 V454"/>
    <path d="M272 418 V454"/>
    <path d="M180 504 V530"/>
  </g>
  <text x="96" y="164" font-size="15" font-weight="700" text-anchor="end">yes</text>
  <text x="264" y="164" font-size="15" font-weight="700" text-anchor="start">no</text>
</g>
</svg>"""


def render() -> str:
    """The whole page as a string."""
    records = _records()
    esc = html.escape
    lanes, legend = _timeline_html(records)
    tokens = {
        "css": _read(STYLE),
        "version": esc(__version__),
        "steps": _steps_html(),
        "lanes": lanes,
        "legend": legend,
        "captions": "\n".join(f"      <li>{esc(c, quote=False)}</li>"
                              for c in captions(facts(records))),
        "diagram": DIAGRAM,
        "roster": _roster_html(records),
        "users": _users_html(load_users(USERS)),
    }
    return Template(_read(TEMPLATE)).substitute(tokens)


def build(out_path: str) -> None:
    """Write the page to `out_path` (UTF-8, LF line endings)."""
    page = render()
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)
