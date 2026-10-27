# tt-gozer — project CLAUDE.md

Cooperative Tenstorrent chip leasing so multiple agents can share one box.
Gatekeeper guards the gate, keymaster carries the key, Gozer is the workload.

Design: `docs/superpowers/specs/2026-08-14-tt-gozer-design.md`
Plan: `docs/superpowers/plans/2026-08-14-tt-gozer-implementation.md`

## What happened

**Original request (2026-08-14):** "a set of claude skills we can strongly suggest
in ~/CLAUDE.md to 1) set chip-specific 'i am using this tenstorrent hardware now'
under /tmp, similar to socket lock files, and then 2) check if hardware is in use
with these and start work when things are available. We want to lightly queue
things." Plus: who holds and who asks both matter, agents should ask for *N*
chips without naming them, and non-Claude flows should be able to join.

Named in a follow-up: "The keymaster must meet the gatekeeper to allow the coming
of gozer."

**Final review pass (2026-08-14).** A whole-branch review found two lock-deleting
concurrency races the 162-test suite could not see, because the only concurrency
test raced `claim_unit` — a bare `mkdir`, the one primitive never in doubt. Fixed
in `62eb0bf`..`8c1808c`; see `.superpowers/sdd/2026-08-14-tt-gozer-implementation/
final-fix-report.md` for the full account, including the RED output the new
concurrency tests produce against the pre-fix code.

The two that mattered: `reconcile`'s reap ran outside the mutex and deleted
whatever lock it found (so a `gozer status` could delete a lease granted
milliseconds earlier), and `Keymaster.release` revalidated nothing (so releasing
a stale lease by hand could reset the *next* tenant's chips mid-startup). Both
now re-read the gate under the mutex, and `release_unit` carries the lease_id
guard in the primitive so no future caller can reintroduce either.

Also: the queue had never worked on a real box — tickets were pruned as
"dead waiters" milliseconds after creation, the detached-lease bug never carried
over to tickets — and `--no-queue` queued anyway.

**Second review wave.** A re-review found the ticket fix incomplete on the `gozer
run` path (its ticket looked supervised, so it was still pruned as a dead waiter)
and settled the design question behind it: *no gozer process ever survives to wait
on a ticket*, so age is the only rule a ticket can be judged by. It also caught
`TimeoutError` becoming newly reachable from `status` — a consequence of making the
reap take the mutex — and two places where the spec still described the old code.
See `## Known limitations` below for what the reviews found and left standing.

**Hold through a model swap (2026-10-02).** tt-orchard (the supervisor in
`~/code/tt-orchard`) needs to hold a board while it swaps the model server. gozer
gained `reset <lease>`, descendant-aware ownership for owner pids above 1, and the
`gozer-park` skill. The work lives on branch `orchard-hold-through-swap` in a
worktree. The test suite no longer reads the real `/proc`.
`tests/test_hold_through_swap.py` runs the whole swap through the CLI and pins the
container blind spot. The version was not bumped, because the live checkout has an
uncommitted bump to 0.3.3 and the owner bumps at merge. Open item: `gozer wait`
grants a lease without an owner pid, so a supervisor re-runs `acquire --owner-pid
... --ticket`; supporting `--owner-pid` on `wait` is a small code change if wanted.

**Release derives its chips (2026-10-02).** `Keymaster.release` took the chips to reset and the
devices to scan from the lease record while checking the gate against its units only, so a
corrupt record could reset another tenant's chips or crash before cleanup. It now derives both
from the units through the topology (as `reset` does), notes and logs a disagreement
(`release_record_mismatch`), skips the reset when the units give no chips, and treats a
`ResetError` as a failed reset. `tests/test_release_derives_chips.py` covers it.

## Key decisions and why

* **CLI + thin skills**, not logic inside skills. Race-sensitive code belongs in
  tested code, and non-Claude flows need the same entry point.
* **fd truth over bookkeeping.** The lease says who and why; only an open fd
  proves a chip is in use. For a lease with a supervised process (`gozer run`),
  reaping needs the pid gone *and* no fd — the user's instruction was "no need
  to be greedy, make sure a process is completely done." A standalone `gozer
  acquire` has no process to supervise, so it is recorded as `detached`:
  judged instead by a fixed 900-second grace window (`DETACHED_GRACE_SECONDS`)
  from the lease's `since` timestamp, and reaped as `STALE` only if that
  window elapses with the device still unopened. Any fd opening at all moves
  it straight to `HELD`.
* **Grain is derived from UMD's own predicate**, not hardcoded, because the tool
  travels between machines.
* **Warn, don't block, on eth neighbours.** Whether a reset perturbs a neighbour
  is unresolved on hardware; the warning upgrades to a block without redesign.

## Corrections worth remembering

* `tt-smi -r` is **per-ASIC**, not per-board. Verified through `device_input.py`,
  `ChipReset.full_lds_reset`, per-ASIC PCIe root ports, and `blackhole.c`'s
  `pcie_timer_interrupt(pdev)`. Only `reset_m3`/`ASIC_DMC_RESET` is board-wide and
  the CLI does not expose it.
* Device **visibility** *is* board-granular, which is the real constraint. Found
  in `cluster_descriptor.cpp`. Requesting one chip through UMD's
  constrained-device / visibility path may expose both chips on the same
  board; direct TTNN device access can still open a single enumerated chip,
  so single-chip work is not impossible — `TT_VISIBLE_DEVICES` just doesn't
  fence below board granularity there.
* The p300c ethernet mesh is **hardwired inside the box** (user correction —
  earlier QuietBoxes needed external QSFP cabling). The `tt-hardware-primer`
  skill's claim that QB2 has no inter-chip Ethernet is **wrong** and should be
  fixed.
* An earlier design used a fixed sentinel pid (`1`, i.e. always-alive `init`)
  to stand in for a standalone `acquire`'s "no process" case. Rejected: it made
  a detached lease immortal instead of merely outliving the one-shot CLI
  invocation that created it. Replaced with an explicit `detached` flag plus
  the 900-second grace window described above.

## Known limitations

Surfaced by the final reviews (2026-08-14) and recorded here rather than left in a
review transcript. All are known, none is a surprise, and each says what it costs
and roughly what fixing it would take.

* **`release`'s revalidation narrows the window, it does not close it.** Between
  the pre-reset check and the reset finishing, a concurrent `reconcile` can
  legitimately reap the lease and grant the unit to a new tenant — so the reset
  can still land on that tenant mid-startup. The post-reset check prevents the
  strictly worse outcome (deleting their lock and opening a claim window on it)
  but cannot undo a reset. *Closure:* pin the lease under the first critical
  section — write `state: "releasing"` into the lock — and teach the reap to
  refuse a unit that is mid-release. Not attempted here: it is a state-format
  change, and the reap would need a staleness rule for a crashed releaser.
  `reset` has the same window and reports it as exit 18.
* **A queued agent loses its place after an hour even while polling correctly.**
  `since` is stamped at enqueue and never refreshed, and `_send_to_back`
  preserves it, so a ticket behind a multi-hour lease expires under a caller
  doing everything right. *Fix:* refresh `since` on each `wait`, or judge age
  from `requeued_at` when present.
* **After a reap, nothing ever resets those chips.** The reap has never reset,
  and `release` now correctly declines to reset a unit it no longer holds, so a
  crashed workload's silicon goes to the next plain `acquire` un-reset. `--fresh`
  is correctly protected (it only takes units a real reset marked clean), and the
  gatekeeper skill now documents the acquire-then-release recipe — but this is
  the guaranteed outcome of the documented recovery flow, not an edge case.
* **`mark_clean`/`clear_clean` are best-effort cross-user.** A marker owned by a
  previous tenant can be neither rewritten nor removed by a different user, so a
  stale "clean" marker can hand `--fresh` a board that was not reset. *Fix:*
  per-user marker files, i.e. a state-format change.
* **`cmd_adopt` claims outside the mutex.** One microscopic hole remains: a
  malformed lock (no `lease_id`) racing adopt's `mkdir`/`os.replace`.
* **Exit `12` → `15` is a breaking contract change.** Anything already matching
  on `12` to mean "device still open" now sees `15`. Nothing in the tool flags
  this as a migration; the README table is the only notice.
* **`cmd_release` distinguishes 13 from 15 by substring** (`"not found" in msg`),
  and `msg` can carry embedded `tt-smi` output. It has not misfired, but the
  right shape is a structured reason on the return value rather than prose.

## `gozer-gatekeeper` distinguishes observing from leasing (2026-09-15)

**Original request:** tools like `tt-smi` and `tt-toplike` only watch telemetry -- they
hold no lease and never open the device exclusively -- so seeing chip activity through
them shouldn't be read as "hijacked." The one case that really is a deliberate event is
`tt-smi -r` actually running: someone or something decided the boards need a reset, which
is where gozer's own hardware-mutating path and the passive-observation tools actually
meet ("crossing the streams"). Taking a lease before running `tt-smi -r` yourself is
already covered by the existing `--owner-pid`/`gozer run` guidance.

Added a new "Observers are not leasees" section to `skills/gozer-gatekeeper/SKILL.md`,
ahead of "Untracked work": reserves "hijacked" for what a lease actually protects against
(`HELD-FOREIGN`, or your own held chip opened by someone else's process), calls a busy
chip with no lease `BUSY-UNTRACKED` rather than a takeover, and calls out an in-progress
`tt-smi -r` as the one legitimate exception worth noticing -- alongside the reminder to
lease before running it yourself.

## Open

* Hardware spike: does resetting one ASIC perturb a live neighbour's eth links?
* Fix `tt-hardware-primer`.
* Revisit per-chip leases if two concurrent tenants proves too few.
* **Backend-agnostic gozer.** Today the gate *is* the filesystem: locks, fd truth, and a
  mutex under `GOZER_ROOT`. Floated 2026-08-24 -- factor that behind an interface so the
  same `acquire`/`release`/`status` surface could sit on top of a durable queue service
  (`boopdotpng/tt-device-queue`) or Slurm instead of, or alongside, the local gate. The hard
  part is not the interface: it is that fd truth is a *local* verification, and a remote
  backend cannot offer it. Anything not filesystem-backed would have to trust its own ledger,
  which is the property gozer deliberately does not rely on. Not scoped; "maybe one day."

## Real-use fixes and observability (2026-08-15/16)

**Original request:** three improvements found by inspecting a live box after real use --
1) `gozer status` was mutating state (reaping) as a side effect of being looked at; 2) there
was no way to see who held what in the past, only right now; 3) nothing reconciled unless a
`gozer` command happened to run, so a crashed lease could sit stale for hours.

**1. `status` stops mutating state.** `cmd_status` called `reconcile()`'s `reap=True` default,
so an investigator running `gozer status` on a two-hour-old lease reaped it as a side effect --
destroying the evidence before it could be read. Now `reconcile(reap=False)`: status reports
every state, including `STALE`, without touching disk. `free_units()` (and therefore
`acquire`) deliberately keeps `reap=True` -- untouched, per its own comment, because a `STALE`
unit would otherwise stay unallocatable until a human ran `gozer reconcile` by hand. Docs
(README, `gozer-gatekeeper` SKILL.md, the design spec) updated everywhere the old "status
already reaped it" language appeared. TDD: a test pinning "status leaves STALE on disk" failed
red against the old code before the fix landed.

**2. Append-only history.** New `gozer/history.py` writes `<GOZER_ROOT>/history.jsonl` --
`granted`, `queued`, `released`, `reaped`, `adopted`, `refused`, one JSON line per event,
appended via a single `O_APPEND` `os.write()` (atomic under Linux's 4096-byte `PIPE_BUF`, so
concurrent agents cannot interleave a torn line) and best-effort (`OSError` swallowed, so a
full disk can never fail an `acquire`/`release`). New `gozer history [-n N] [--json]` reads it
back. This is the one file in `GOZER_ROOT` that is never deleted -- everything else is
present-tense and vanishes on release/reap.

**3. Optional periodic reconciliation.** `contrib/gozer-reconcile.{service,timer}`: a systemd
**user** unit running `gozer reconcile` every 5 minutes, so a crashed lease self-heals even
between commands. Never enabled automatically -- `install.sh` only prints the two
`ln`/`systemctl --user enable` commands, since flipping on a systemd unit is a machine change
the tool should not make unasked. The service checks for the `gozer` binary and swallows its
own output so a broken box fails quietly (one failed-unit transition per tick) instead of
spamming the journal, and `Restart=no` stops it from spinning.

All three landed as separate commits, each with its own failing-test-first evidence; full suite
(217 tests) green throughout.

## Supervisor-held leases: `--owner-pid` (2026-08-16)

**Original request:** a lease taken by something that will not itself open the device --
`tt-station-agentd` acquires chips, launches a serving container pinned to them, and
releases when the container stops.

That case breaks the detached-lease design. A detached lease is judged by
`DETACHED_GRACE_SECONDS` (900s) from `since`, on the assumption that whoever acquired is
about to open the device and an fd will appear shortly. A root-owned process inside a
container has `/proc/<pid>/fd` readable only by its owner, so an unprivileged gozer can
*never* see its fds -- not "not yet", but permanently. The lease would age past the grace
window and be reaped as `STALE` about fifteen minutes into a perfectly healthy session,
leaving the chips reading `FREE` while the container kept using them. That is precisely the
collision gozer exists to prevent, manufactured by gozer itself.

**`gozer acquire --owner-pid N`** passes the supervisor's own pid (long-lived, outside the
container) through `Keymaster.acquire`'s pre-existing `pid` parameter -- the same one `gozer
run` uses for its child. The lease is then **not detached**: `Gatekeeper.reconcile` judges it
by ordinary `pid_alive(N)`, so it stays `CLAIMED` for exactly as long as the supervisor lives,
no fd and no timer involved. No new state machine, no new reap rule; it reuses the
non-detached path that already existed and only `gozer run` could reach.

Key decisions:

* **Validated in `cmd_acquire`, before `Keymaster` ever sees it.** A non-positive pid is
  never a real process; an already-dead pid would mint a lease reapable the instant it was
  created, which is strictly worse than refusing. Both are refused with `EXIT_UNAVAILABLE`
  and a message that says why.
* **Checked against `gk.proc_root`, never the real `/proc`.** Keeps the validation hermetic
  under the test suite, which fakes a proc tree.
* **Wired into `cmd_acquire` only, deliberately not `cmd_wait`.** A ticket replayed by `wait`
  belongs to whoever is waiting *now*, and `wait` never re-derives a `pid` from the ticket it
  replays -- inheriting a stale supervisor pid recorded on an old ticket would misattribute a
  fresh grant to a supervisor that may no longer be the one waiting.
* **The grant output says so.** A supervisor-held grant prints `owner pid N (not detached --
  held for as long as that pid lives, no grace window)`, so the difference is visible at the
  point of acquisition rather than only in the docs.

Docs: README's reaping section, the design spec, and both skills. The gatekeeper skill needed
it most -- an investigator reading a lease with a live pid, no fd, and hours on the clock
would otherwise diagnose the healthy shape of a supervisor lease as a stale lock.

TDD throughout (`tests/test_owner_pid.py`, 153 lines); full suite 225 tests green. Merged to
`main` on 2026-08-24 -- the branch sat unmerged for a week while the comparison against
`boopdotpng/tt-device-queue` (a community durable-queue take on the same problem: HTTP
service + SQLite + per-card jobs, versus gozer's daemonless fd-truth leases) was written up.
Ideas from it worth stealing, all of which map onto entries already in `## Known limitations`:
round-robin fairness across clients, durable dead-card state surviving a restart, and
refreshing a ticket's queue position so a correctly-polling waiter cannot expire. Also floated:
making gozer backend-agnostic so a queue like that -- or Slurm -- could sit underneath it.

## `gozer report`: the containment log becomes a computed feature (2026-09-07)

**Original request:** the GH Pages microsite (`e49b72e`) only covered 2026-09-03/04 and hadn't
been refreshed since; bring it up to date, and make usage reporting "part of the fabric" rather
than a transient `/tmp` artifact someone regenerates by hand.

Turned out the page was less "static" than it looked: the timeline and incident log were already
computed client-side JS from an embedded event blob, but the roster table, hero stats (entity
counts, ghost counts, reclaimed hours), palette, board/lane labels, and most of the prose had all
been hand-typed by whoever built the page on 2026-09-04. Regenerating it without fixing that would
just mean repeating the same manual transcription next week.

**New `gozer/report.py`** computes everything that used to be typed in by hand, purely from a
`history.read()` event list: per-identity roster stats (sessions/clean/ghosts/held-time/longest/
still-active), hero numbers, a stable per-identity color (hashed into a small green-shade
palette so re-runs don't reshuffle colors), and a unit-to-chips board map *recovered from the
data itself* -- single-unit events carry both `units` and `chips` together, so folding those
pairs across the log reconstructs "board X has chips A/B" without hardcoding PCI addresses or
board serials for one particular box. `gozer/report_template.html` is the original hand-authored
page with every hand-typed number/palette/mapping/prose-block replaced by a `{{TOKEN}}`; the CSS
and the client-side timeline/incident JS are untouched -- widening the embedded event list was
enough for them to pick up the wider window on their own.

**New CLI verb `gozer report [--out PATH] [--json]`** — reads history, never touches hardware.
Default output is `docs/index.html` at the repo root (found via `git rev-parse --show-toplevel`).
`--json` prints the computed stats without rendering HTML. Refuses (`EXIT_UNAVAILABLE`) on an
empty log rather than rendering a page with no data.

Deliberately **not** wired into the `contrib/gozer-reconcile` systemd timer or any other
automatic path -- publishing means a git commit, and a timer silently committing to the repo is a
much bigger claim than "reconcile stale leases." Regeneration stays a manual, reviewed step:
`gozer report && git commit docs/index.html`.

TDD throughout (`tests/test_report.py`, plus CLI-level tests in `tests/test_cli.py`); full suite
241 tests green. Version bumped to 0.3.0. `docs/index.html` regenerated against the live
2026-09-03..09-07 log (145 events, 9 identities) as part of the same change.

## `gozer report` stops leaking raw --reason text (2026-09-08)

**Original request:** "update it again. let's leak less our specific prompts but instead note
the theme of them too; revise previous." The published page's embedded event JSON carried every
lease's literal `--reason` string verbatim -- e.g. "V-JEPA2 functional decoder bring-up
(multi-session)", "verify rotary_embedding_llama matches our rotate_half convention" -- which is
effectively the task prompt/context for whatever internal project was running, sitting in public
page source.

New `gozer.report.theme_for_reason()`: an ordered keyword match against a small set of public-safe
labels (serving a demo, verification / testing, debugging, benchmarking / optimization, bring-up,
build / packaging, exploration, general work). `render()` now redacts every event's `reason`
through this before embedding the event list in the page's JSON blob -- the one thing the
client-side timeline tooltips and incident cards read from, so the fix lands in exactly the place
the leak was reaching the page. `who` identities (e.g. `claude:vjepa2-bringup`) are left alone --
that's the roster's whole point ("who's on the line"), and the request was specifically about
prompt/reason text, not operator identity.

`docs/index.html` regenerated against the current live log with themed reasons throughout.
TDD (`TestThemeForReason`, `TestRenderRedactsReasons` in `tests/test_report.py`); full suite 251
tests green. Version bumped to 0.3.1.

## `GOZER_HISTORY_ROOT`: history.jsonl survives a reboot (2026-09-10)

**Original request:** "Add a persistent logging option so logs aren't lost on reboot anymore."
`history.jsonl` had always inherited `GOZER_ROOT`'s home (`/tmp/tt-gozer` by default) purely
because it lived alongside `gate/`/`leases/`/`queue/` — state that legitimately *should*
vanish on reboot, since the chips themselves get reset. The audit trail doesn't need that
property, and losing it every reboot defeated the point of `gozer report`'s containment page.

New `Gatekeeper(history_root=...)` param / `GOZER_HISTORY_ROOT` env var, read by `cli._make`
alongside the existing `GOZER_ROOT`/`GOZER_SYSFS_ROOT`/`GOZER_PROC_ROOT`. Defaults to
`self.root` — unset, behavior is byte-for-byte what it was before. When set to a path outside
`/tmp` (e.g. `~/.local/state/tt-gozer`), `history.jsonl` lives there and survives a reboot while
`gate/`/`leases/`/`queue/`/`mutex/` stay under `GOZER_ROOT` as before. Every `history.log`/
`history.read` call site (gatekeeper.py's reap, keymaster.py's granted/queued/released/refused,
cli.py's adopted/history/report) now goes through `gk.history_root` instead of `gk.root`.
`_ensure_dirs` creates the history root too (same sticky-0o1777 multi-user rationale) when it
differs from `root`.

Bounded-path brainstorm (no spec file): decoupling the log's location from the lock state's
location, rather than just repointing `GOZER_ROOT` itself, which would also make leases survive
reboot — the wrong fix, since a lease surviving past the reboot that reset its chips is stale by
construction.

TDD: `tests/test_history_root.py` (7 tests: default unchanged, param/env-var override, param
wins over env var, directory created, events land in the right root, history survives the
GOZER_ROOT directory being wiped out from under it) plus one CLI-level test in `tests/test_cli.py`
(`test_gozer_history_root_env_var_redirects_the_log`). Docs updated everywhere the old
"lost on reboot, that's just how it is" language appeared: `gozer/history.py` and
`gozer/gatekeeper.py` module docstrings, README's State Format and Testing sections, and the
design spec's Observability section. Full suite 260 tests green. Version bumped to 0.3.2.

## `gozer report` blends recovered pre-migration history into every render (2026-09-21)

**Original request:** the working-tree diff in `docs/index.html` showed the containment report
snapping from a 182-hour, 41-identity window down to a 20-hour, 4-identity one. Root cause:
`GOZER_HISTORY_ROOT` (added in the previous entry) was never actually set on this box, so
`history.jsonl` kept living under `/tmp/tt-gozer` and kept resetting to empty on every reboot,
same as before that feature existed. Ask: "do a kind of merge where this report will get the
old data blended into the new data every time."

The 2026-09-03..09-10 window wasn't gone, though -- it was still sitting, redacted, in the
`<script type="application/json" id="gozer-events">` blob of the last report rendered before
the migration (commit `e756fe2`'s `docs/index.html`), since a rendered report is the only thing
that ever accumulated a long window in the first place. Extracted those 398 events back out and
checked them in as `gozer/history_archive.jsonl` -- the one place in the repo that isn't wiped
by a reboot, unlike `/tmp/tt-gozer`.

New `report.load_archive()` reads that file (empty list on a missing/corrupt file, never
raises); new `report.merge_events(archived, live)` unions it with `history.read(gk.history_root)`,
de-duplicating on `(event, lease_id, ts)` and sorting by `ts` (`compute_stats`/`render` both
assume the list they're handed is already chronological). `cmd_report` in `cli.py` now always
merges the archive in before checking for emptiness or rendering -- so the page reads as one
continuous ledger regardless of how much the live log has lost since the last reboot, and
`gozer report` refuses (exit 12) only when *both* the archive and the live log are empty.

One correctness wrinkle surfaced by feeding archived events back through the redaction path:
archived events already carry a theme label in `reason` (they were redacted before ever being
embedded), and `theme_for_reason` used to re-derive a label from keywords every time it was
called. Most labels happen to contain their own trigger keyword ("debugging" contains "debug"),
but `"exploration"` doesn't contain `"explore"` -- so re-theming an archived exploration event
silently drifted it to `"general work"` on every re-render. Fixed by having `theme_for_reason`
return a reason unchanged when it's already one of the known labels, rather than re-matching it.

Regenerated `docs/index.html`: window now reads `2026-09-03 .. 2026-09-21` (446h, 42 entities)
instead of snapping back to whatever the live log held since the last reboot.

Deliberately not done: actually setting `GOZER_HISTORY_ROOT` persistently (e.g. in a shell
profile or the `contrib/gozer-reconcile` timer's environment) so this stops recurring going
forward. That's a machine/dotfiles change to `tt-home`, not a change to this repo's behavior,
and wasn't what was asked -- the archive-and-merge is what makes the *report* durable against
it recurring; the live log itself will still reset on every reboot until that env var is
actually set somewhere persistent.

TDD: `TestLoadArchive`, `TestMergeEvents`, `TestThemeForReasonIsIdempotent` in
`tests/test_report.py`; `test_report_blends_the_checked_in_archive_with_live_history` and a
reworked `test_report_refuses_when_archive_and_history_are_both_empty` (monkeypatching
`report.load_archive` to `[]`, since the real archive is now always present) in
`tests/test_cli.py`. Full suite 272 tests, 271 green -- the one failure
(`test_history_survives_gozer_root_being_wiped`) predates this change and reproduces identically
on `main` before it. Version bumped to 0.3.3.

## Release waits for a fast-exiting command's holders (2026-10-28, 0.3.4)

Prompt (tt-orchard issue 117): a `gozer run --chips 1 --exact 1 -- <command>` ended with
`chips [0, 1] still open by [pid]`, a stale lease and no reset, after a command that failed in
about a second.

Cause, reproduced on a real board. `Keymaster.run` released the lease right after the command
was reaped, with one `/proc` scan. A command that forks a worker (no exec, so the device fd
arrives with it even though the fd is close-on-exec) and then exits leaves that worker holding
the device until the worker ends. A plain open and exit, with or without `close_device`, never
refused in 8 runs. A command that opened the device, forked a worker that slept 1.5 s and
exited at once refused 6 of 6 times in about 2.6 s, left a STALE lease on the board and did not
reset it. The refusal text and timing match the issue. Which process held the device in the
original occurrence is not known (the issue names one pid and does not say whether it was the
command itself); a reaped process cannot hold a descriptor, so a helper or worker of the
command is the likely holder.

Fix: `release(..., settle=SECONDS)` re-scans every 0.25 s while a device of the lease is open,
and goes on to the reset when the holders are gone. `run`'s release passes
`RELEASE_SETTLE_SECONDS` (10 s). If a process still holds the device after that, the refusal is
the same as before, with the wait added to the message and to the history record
(`waited_s`). `gozer release` by hand, `reset` and `--force` do not wait. The reset still never
runs while a live process holds one of the lease's devices, and nothing outside the lease is
looked at differently.

Measured (one 2-chip board, a one-chip lease, `bin/gozer` of this worktree):
before, 8 of 8 `os._exit` runs released normally (about 44.5 s each) and 6 of 6 forked-worker
runs refused (about 2.6 s, stale lease). After, 6 of 6 forked-worker runs released and reset
(45.6 to 45.9 s, the 1.6 s wait stated in the message), and a worker that lives 15 s was
refused after 10.1 s.

TDD: `tests/test_release_settle.py` (7 tests, all red before the change; fake `/proc`, fake
clock). 13 mutations (no loop, no wait from `run`, wait with `--force`, no re-scan, never
refuse, unbounded or doubled bound, no sleep, nonzero default, missing messages and log
field), all red. Full suite 400 passed.

Not done: a holder that stays longer than 10 s is still a refusal that leaves a stale lease
(the caller can run `gozer reconcile` and the next lease resets). Whether a child of a
compile step is what held the device in the original run was not shown.
