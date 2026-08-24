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
