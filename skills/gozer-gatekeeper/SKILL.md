---
name: gozer-gatekeeper
description: Use when Tenstorrent chips appear stuck, contended, or busy without a lease - reads the real state of the gate, distinguishes stale locks from live work, adopts untracked processes, and decides whether a reset is safe.
---

# Reading the gate: the gatekeeper

Use this when `gozer acquire` will not grant, something looks wedged, or you are
about to reset and want to know whether that is safe.

## Start here

```bash
gozer status
```

Every chip is in exactly one of six states. **The table defining them lives in
`~/code/tt-gozer/README.md`, under "How it decides what's free" — read it
there.**
It is deliberately not duplicated here: this file is the copy an agent reads
instead of the code, so it is the copy most likely to drift.

What to do about each, which the README does not say:

- `FREE` — acquire it.
- `HELD`, `CLAIMED` — nothing. Working as intended.
- `HELD-FOREIGN` — investigate before anything else; the lease is lying.
- `BUSY-UNTRACKED` — do not take it. Adopt it (below).
- `STALE` — `status` reports it but deliberately leaves it on disk (it never mutates state).
  Clear it with `gozer reconcile`.

`OVERSTAYED` is a flag, not a state: the lease ran past its advisory `--expect`.
Whatever is holding the chip (fd open, a live owning process, or a detached
lease still inside its grace window) is still legitimately there. **This is not permission to reap
it.** Ask the human, or wait.

## Untracked work

A chip open with no lease usually means someone started a server by hand. Make it
visible so the queue stops treating it as free:

```bash
gozer adopt 0 --who "manual:vllm-qwen3" --reason "server started outside gozer"
```

## Stale locks

`gozer reconcile` re-derives everything from kernel truth and reaps what is
genuinely dead. It is conservative on purpose: a lease with a supervised
process is only removed once that process is **gone** *and* no file
descriptor remains; a detached lease (no supervised process to begin with) is
only removed once its 900-second grace window has elapsed with no fd ever
opened. If a lock survives reconcile, the work behind it is not finished —
leave it alone.

**Not every lease with an owning pid came from `gozer run`.** `gozer acquire
--owner-pid N` produces a non-detached lease whose owner is a *supervisor* —
something that took the lease and then launched the real workload elsewhere
(`tt-station-agentd` and its serving container are the motivating case). Two
consequences when you are reading such a lease:

- **No fd is not evidence of a dead lease here.** The workload may be a
  root-owned process in a container whose `/proc/<pid>/fd` an unprivileged
  gozer can never read — permanently, not just during startup. "pid alive, no
  fd, hours old" is the *expected* healthy shape, not a stale lock.
- **The pid you see is the supervisor, not the workload.** Killing it, or
  reasoning about what the chips are doing from it, will mislead you. Find the
  workload through the supervisor (the container, the unit), not through the
  lease.

Reconciliation already treats these correctly — it judges them by
`pid_alive(N)` like any other non-detached lease. This note is so *you* do not
diagnose one as stale when the tool has not.

`gozer status` is read-only: it shows you `STALE` but never removes it, so
that looking at the gate can never destroy the evidence you were looking at.
`gozer reconcile` (and `gozer acquire`, as a side effect of allocation) are
the only commands that actually reap.

Cross-user note: `/proc/<pid>/fd` is readable only by its owner, so fd truth
covers your own processes. If you suspect another user's process:

```bash
gozer reconcile --sudo
```

## Before you reset

Never `tt-smi -r` by hand while others may be working. Release does the reset for
you, scoped to exactly your chips:

```bash
gozer release <lease-id>
```

**With one exception you will hit often.** If the lease's lock is already gone —
`reconcile` reaps as a side effect of every `acquire` (never of `status`, which is
read-only), so a crashed or abandoned lease is often reaped before anyone releases
it — `release` does bookkeeping only and says `not resetting: … no longer locked`.
It is not being lazy: those chips are free for anyone to take at that moment, so a
reset would land on whoever grabs them next. Nothing has reset that silicon, and the
next plain `acquire` gets it un-reset.

To actually reset it, take the unit and give it back:

```bash
gozer acquire --exact <chip-or-board> --who "human:cleanup" --reason "reset after crash"
gozer release <new-lease-id>
```

`--fresh` is safe either way: it only ever hands out units marked clean by a
release that really did reset them.

If you must reset outside a lease, check two things first:

1. `gozer status` shows nothing `HELD`, `HELD-FOREIGN`, `CLAIMED`, or
   `BUSY-UNTRACKED` on those chips.
2. No eth-neighbour warning — the p300c mesh is hardwired between ASICs, so a
   reset may perturb a neighbour's links.

Never pass an M3/DMC reset. That is the one genuinely board-wide path; `gozer`
never emits it and neither should you.

## If the gate itself is wedged

The allocator mutex self-heals after 30 seconds. If `gozer` still hangs and you
are certain nothing is running:

```bash
ls -la /tmp/tt-gozer/                    # inspect by hand; it is all plain files
rmdir /tmp/tt-gozer/mutex/.gatekeeper.lock
```
