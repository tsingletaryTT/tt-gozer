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

## Open

* Hardware spike: does resetting one ASIC perturb a live neighbour's eth links?
* Fix `tt-hardware-primer`.
* Revisit per-chip leases if two concurrent tenants proves too few.
