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

Every chip is in exactly one of six states. What each means, and what to do:

| State | Meaning | Do |
|-------|---------|-----|
| `FREE` | No lease, nothing holding it | Acquire it. |
| `HELD` | Leased, and the device fd is open — by the lease's own supervised process, or (for a detached lease from a standalone `gozer acquire`) by whatever process opened it | Nothing. Working as intended. |
| `CLAIMED` | Leased, no fd open yet — the owning process is alive and just hasn't opened the device, or (for a detached lease) it is still inside its 900-second grace window | Nothing. Setup phase, or between runs. |
| `HELD-FOREIGN` | Leased to one pid, a *different* pid has it open | Investigate before anything else — the lease is lying. Does not apply to detached leases: any holder at all counts as expected, since a standalone `acquire` never knew its eventual workload's pid. |
| `BUSY-UNTRACKED` | No lease, but a process has it open | Do not take it. Adopt it (below). |
| `STALE` | Leased, no fd, and the owning process is gone — or, for a detached lease, its 900-second grace window elapsed with the device never opened | Already reaped for you; re-run `gozer status`. |

`OVERSTAYED` is a flag, not a state: the lease ran past its advisory `--expect`.
Whatever is holding the chip (fd open, or a detached lease still inside its
grace window) is still legitimately there. **This is not permission to reap
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
