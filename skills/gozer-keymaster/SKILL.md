---
name: gozer-keymaster
description: Use before running any workload that touches Tenstorrent chips (vLLM, tt-metal, TTNN, on-device pytest, tt-smi -r) - acquires a chip lease, tells you which chips you got and how to target them, and queues you when the box is busy.
---

# Getting chips: the keymaster

The box is shared. Several agents may be working at once, so **take a lease before
you open any device**, and honour the chips you were given.

## The one-liner that cannot go wrong

If your work is a single command, wrap it. The lease is bound to the process, so
it is released even if you are interrupted:

```bash
gozer run --chips 1 --who "claude:<your-skill>" --reason "<what you are doing>" -- \
    python your_workload.py
```

## When you need the lease to outlive one command

```bash
gozer acquire --chips 1 --who "claude:<your-skill>" --reason "<why>" --expect 45m
```

A bare `acquire` has no process for gozer to supervise, so it produces a
*detached* lease — one with no fd and no owning process gozer can watch for
death. You have 900 seconds (15 minutes) to actually open the device — start
your server, launch your job — before reconciliation reaps it as `STALE`. As
soon as anything opens the chip the lease becomes `HELD`, and there is no
further timer: it stays `HELD` for as long as something holds it open. `gozer
run` does not have this window at all, since its process is supervised from
the start.

Read the output. Three things matter:

1. **The export line.** Run it, or pass the value through to your process.
   `TT_VISIBLE_DEVICES` is BDFs, not indices.
2. **The expansion note.** On a 2-chip board (a p300c QuietBox) asking for 1 chip
   grants 2. Requesting one chip through UMD's constrained-device / visibility
   path may expose both chips on the same board, so `TT_VISIBLE_DEVICES` cannot
   fence you to one of them. You got both; that is correct, not a bug. Direct
   TTNN device access *can* open a single enumerated chip — but a lease scoped
   narrower than the fence that enforces it would be a lease in name only, so
   gozer grants the pair.
3. **Any eth-neighbour warning.** It means another tenant shares your board's
   hardwired mesh and your reset on release may perturb them. Proceed, but say so
   if something odd happens to them.

Release when you are done:

```bash
gozer release <lease-id>
```

Release resets your chips so the next tenant gets clean silicon. Use
`--no-reset` only when you are handing off to your own follow-up run.

### When something *else* will hold the chips: `--owner-pid`

The grace window assumes you are the one about to open the device. If you are a
**supervisor** — you take the lease and then launch a container, a systemd unit,
or any process whose file descriptors you cannot read — that assumption is wrong
for you. A root-owned process inside a container holds fds an unprivileged gozer
can never see, at any point in its life, so a detached lease taken on its behalf
would be reaped as `STALE` about fifteen minutes into a perfectly healthy
session. Pass your *own* pid instead:

```bash
gozer acquire --chips 1 --who "station:vllm-qwen3" --reason "serving container" \
    --owner-pid $$
```

The lease is then judged by that pid's liveness, exactly like `gozer run`'s: no
fd needed, no grace window, `CLAIMED` for as long as the supervisor lives, reaped
once it is gone. A non-positive pid, or one that is not alive at acquire time, is
refused outright rather than handed back as a lease that would evaporate
immediately.

This is for supervisors only. If *you* are the process that will open the device,
you want the plain `acquire` above (or better, `gozer run`) — passing your own pid
when you are also the workload buys nothing and hides a crash behind a still-live
shell.

## When you are queued (exit code 10)

You get a ticket and a position. **Do not spin.** Go do work that does not need
hardware — read code, write tests, prepare configs. Then:

```bash
gozer wait <ticket> --timeout 8m
```

`wait` blocks up to 8 minutes (deliberately under the 10-minute tool-call limit)
and returns either a grant or your current position. Loop it if you are still
waiting, or `gozer cancel <ticket>` if you have changed plans.

## How many chips to ask for

| Situation | Ask for |
|-----------|---------|
| Single-chip test, smoke run, small model | `--chips 1` |
| Multi-chip / mesh work, large model | `--chips all` |
| Either would do, take what is free | `--chips 1-4` |
| A specific chip, for a reproduction | `--exact 2` or `--exact 0000:03:00.0` |
| Must start from clean silicon | add `--fresh` |

Prefer the elastic form when your work can scale — it gets you started sooner
and keeps the queue moving.

## Conventions

- `--who` should be `claude:<skill-name>` so a human reading `gozer status`
  knows which agent is holding the box.
- `--reason` is free text and shows up in status. Write something a colleague
  could act on.
- `--expect` is advisory only once a lease is `HELD` (or still within its
  detached grace window) — nothing reaps a `HELD` lease when `--expect`
  lapses; it just tells people waiting how long you think you will be. It
  does **not** extend the 900-second grace window on an un-opened detached
  lease, so do not rely on a long `--expect` to buy time before you open the
  device.

If something looks stuck or a chip is busy with no lease, switch to the
`gozer-gatekeeper` skill.
