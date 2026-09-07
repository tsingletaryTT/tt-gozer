# gozer report — turning the containment log into a first-class feature

## Problem

`docs/index.html` (the GH Pages "Containment Log" microsite, added in
`e49b72e`) reads beautifully but is a one-off artifact: someone read
`history.jsonl` by hand on 2026-09-04, hand-computed a roster table, hand-typed
hero stats ("7 entities", "8 ghosts exorcised", "47 hours") straight into the
HTML, and hand-picked a color palette and board/lane labels. The timeline and
incident log *are* computed client-side from an embedded JSON blob of events —
but that blob is a snapshot, frozen at generation time.

Two problems follow directly from that:

1. The page is already stale — live `history.jsonl` now runs 2026-09-03
   through 2026-09-07 (143 events), not just the two days embedded.
2. Regenerating it means repeating the same manual, error-prone transcription
   — there is no way to keep it current without a person doing the arithmetic
   again.

The user wants this to be "built into the fabric" of the tool: a real
`gozer` feature, not a transient artifact under `/tmp` (`history.jsonl`
already isn't transient in the "lost on reboot" sense that leases are — it's
the one file in `GOZER_ROOT` that's never deleted — but nothing before now
*used* that durability for anything other than `gozer history`'s plain-text
tail).

## Design

### New module: `gozer/report.py`

Pure functions over a list of history records (as returned by
`history.read()`) — no filesystem or hardware access beyond reading the log.
Three responsibilities:

1. **`compute_stats(events) -> dict`** — everything currently hand-typed:
   - Per-identity roster: sessions (count of `granted`), clean releases
     (`released`), ghosts (`reaped`), total held seconds (sum of `duration_s`
     over both), longest single hold, and whether that identity has a lease
     still open at the end of the log (a `granted` with no matching
     `released`/`reaped`).
   - Hero stats: distinct entities, total grants, total ghosts, total
     reclaimed seconds (sum of `duration_s` where `event == "reaped"`), queue
     event count, and the derived board/chip topology (below).
   - Log window: first/last event timestamp, elapsed duration.
   - Pull-quote ratio: reclaimed seconds ÷ (reclaimed + cleanly-released
     seconds), as a percentage.

2. **`derive_board_map(events) -> dict[str, list[str]]`** — unit → chips.
   The event log never states "unit X has chips A/B" directly, but
   single-unit events carry both `units: [X]` and `chips: [...]` together;
   folding those pairs across the whole log recovers the mapping without
   hardcoding board serials or PCI addresses (the current page hardcodes
   `062`→chips 01/02, `055`→chips 03/04, which only holds for the box it was
   generated on). Multi-unit events are skipped for this derivation — they
   can't disambiguate which chip belongs to which unit.

3. **`assign_palette(identities) -> dict[str, str]`** — deterministic color
   per `who`, so re-running the report doesn't reshuffle colors. Hash each
   identity string, map into a fixed list of green-family hex shades (reusing
   the 7 currently hand-picked as the base list, extended cyclically if there
   are more identities than colors).

4. **`render(events, *, source_note=None) -> str`** — renders the full HTML
   page. The existing hand-authored `docs/index.html` becomes the template:
   its CSS and structure are preserved byte-for-byte; the hand-typed roster
   rows, `PALETTE`, `UNIT_LANE`/`LANE_LABEL`, hero-stat numbers, boot-banner
   text, tagline dates, and embedded JSON blob become computed insertions.
   The timeline/incident-log client-side JS logic is untouched — it already
   derives everything it needs from the embedded event JSON, so widening that
   JSON to the full log is sufficient for it to pick up every day.

Template mechanics: the current file's `<script>` blocks distinguish
"hardcoded data" (roster array, palette object, unit→lane map) from "derived
in the browser" (timeline segments, incident list) — `render()` only needs to
replace the hardcoded parts and the prose numbers, using simple string
substitution against a copy of the existing file kept as
`gozer/report_template.html` (not re-typed from scratch, so the design stays
identical). Placeholders use a `{{TOKEN}}` convention that cannot collide with
the page's own JS/CSS braces (grep-checked before landing).

### New CLI verb: `gozer report`

```
gozer report [--out PATH] [--json]
```

- Reads `history.read(gk.root)` (same root resolution as every other
  command — `--root`/`GOZER_ROOT`).
- Default `--out`: `docs/index.html` relative to the repo root (the module
  finds the repo root the same way `contrib/` scripts already assume — via
  `git rev-parse --show-toplevel`, falling back to cwd if that fails so the
  command still works outside a git checkout).
- `--json`: print `compute_stats()`'s output as JSON to stdout instead of
  writing HTML — for scripting/inspection, mirroring the `--json` convention
  every other subcommand already has.
- No lease is taken; this command never touches `/dev/tenstorrent/*`.
- Exits `EXIT_OK` on success; if the log is empty, prints a message and exits
  `EXIT_UNAVAILABLE` rather than emitting a page with no data (nothing to
  report yet is a distinct, expected case — e.g. right after
  `GOZER_ROOT` is first created).

### Docs

- README: one line under the history section — `gozer report` regenerates
  `docs/index.html` from the live log; you re-run it and commit when you want
  the published page current.
- Deliberately **not** wired into `contrib/gozer-reconcile.{service,timer}` or
  any other automatic path: publishing to `docs/` means a git commit, and a
  systemd timer silently committing to the repo is a much larger claim than
  "reconcile stale leases every 5 minutes." Regeneration stays a manual,
  reviewed step.

## Testing

New `tests/test_report.py`, TDD against synthetic event lists (same style as
`tests/test_history.py`): `compute_stats` counts/sums correctly including the
still-open-lease case, `derive_board_map` recovers a two-board mapping from
mixed single/multi-unit events, `assign_palette` is stable across two calls
with the same identity set, and `render()` produces HTML containing the
computed numbers (spot-checked substrings, not a full DOM parse) and a
`<script type="application/json">` blob that round-trips through
`json.loads` back to the full input event list. A CLI-level test exercises
`gozer report --json` and `--out` end-to-end against a temp `GOZER_ROOT`.

## Out of scope

- Any change to what gets logged (`gozer/history.py`'s event shape).
- Automatic publishing (git commit/push) of the regenerated page.
- Multi-machine aggregation — the report is always one box's one
  `GOZER_ROOT`.
