# tt-gozer

Cooperative chip leasing for Tenstorrent boxes, so several agents — Claude Code sessions,
aider, shell scripts, cron jobs — can share one machine without corrupting each other's runs.

A lease says *who* is using *which chips* and *why*. The kernel says whether that's actually
true. When those two disagree, the kernel wins.

## The problem

One box, several agents. Nothing coordinates them, so two Claude sessions — or a session and
a vLLM server someone started by hand last Tuesday — will happily open the same chips and
ruin both runs. The failure is silent and looks like a model bug.

tt-gozer makes "I am using this hardware now" a legible, checkable fact.

## Install

Requires Python 3.9+. Standard library only — no dependencies, no virtualenv.

```bash
git clone https://github.com/tsingletaryTT/tt-gozer.git ~/code/tt-gozer
~/code/tt-gozer/install.sh
gozer status
```

The installer symlinks rather than copies, so `git pull` updates everything in place:

| What | Where | Why there |
|---|---|---|
| `bin/gozer` → `~/.local/bin/gozer` | your `PATH` | so any shell, script or agent can call it |
| `skills/gozer-keymaster` → `~/.claude/skills/` | Claude Code skills dir | teaches agents to lease before they run |
| `skills/gozer-gatekeeper` → `~/.claude/skills/` | same | teaches agents to diagnose a stuck gate |

Skill directories are linked **individually**, never the parent — `~/.claude/skills/` usually
holds many unrelated skills and linking over it would clobber them.

Lock state lives in `/tmp/tt-gozer/`, created on first use. Nothing needs root.

**Optional: periodic reconciliation.** Nothing reconciles unless a `gozer` command runs, so a
crashed agent's lease can sit stale for hours if nobody happens to invoke gozer in the
meantime — a real box hit this: a crashed lease's locks sat for over two hours because nothing
ran a gozer command. `contrib/gozer-reconcile.{service,timer}` run `gozer reconcile` every 5
minutes as a systemd **user** unit, so the gate self-heals even between commands. The installer
never enables this — it only prints the two commands, because enabling a systemd unit changes
your machine in a way you did not ask for:

```bash
mkdir -p ~/.config/systemd/user
ln -sfn ~/code/tt-gozer/contrib/gozer-reconcile.service ~/.config/systemd/user/
ln -sfn ~/code/tt-gozer/contrib/gozer-reconcile.timer ~/.config/systemd/user/
systemctl --user enable --now gozer-reconcile.timer
```

The service is harmless if `gozer` is not installed or the sysfs topology is unreadable — it
checks for the binary first, swallows `gozer reconcile`'s own output, and never restarts on
failure, so a broken box gets one quiet failed-unit transition per tick instead of a noisy
journal.

## For humans

The safest form binds the lease to a process, so it cannot leak if you Ctrl-C:

```bash
gozer run --chips 1 --who "me:sdxl-smoke" -- python generate.py
```

To hold a lease across several commands:

```bash
gozer acquire --chips all --who "me:llama-bringup" --reason "8b perf sweep" --expect 2h
eval "$(gozer env 2f9a1c)"     # exports TT_VISIBLE_DEVICES
pytest tests/test_decode.py
python bench.py
gozer release 2f9a1c           # resets your chips for the next tenant
```

To see who has what:

```bash
$ gozer status
grain: board   (2 boards, 4 chips)
board 0000000000000002  (p300c)
  chip 0  0000:01:00.0  HELD             claude:ttm-optimize pid 902311
  chip 1  0000:02:00.0  HELD             claude:ttm-optimize pid 902311
board 0000000000000001  (p300c)
  chip 2  0000:03:00.0  BUSY-UNTRACKED   pid 861926 — no lease; try: gozer adopt
  chip 3  0000:04:00.0  BUSY-UNTRACKED   pid 861926 — no lease; try: gozer adopt
queue:
  1) claude:tt-generate  wants 1 chip(s)  since 2026-08-14T13:02:11Z
```

`BUSY-UNTRACKED` is the interesting one: a chip somebody is using without a lease. gozer
never hands those out, and `gozer adopt` wraps a lease around them so the queue stops
treating them as free.

To see who had what, in the past -- gozer's present-tense state (`gate/`, `leases/`) vanishes
the moment a lease is released or reaped, so this is the only way to answer "who had the chips
two hours ago" or "how often do we contend":

```bash
$ gozer history -n 5
2026-08-14T12:40:11Z  granted    chips=['0000:03:00.0', '0000:04:00.0'] detached=False ...
2026-08-14T13:02:11Z  queued     max_chips=1  min_chips=1  ticket=8f21  who=claude:tt-generate
2026-08-14T13:25:07Z  released   chips=['0000:03:00.0', '0000:04:00.0'] duration_s=1496.0 ...
2026-08-14T13:25:07Z  granted    chips=['0000:03:00.0', '0000:04:00.0'] ... who=claude:tt-generate
2026-08-14T15:40:02Z  reaped     chips=['0000:01:00.0', '0000:02:00.0'] why=owning process is dead
```

## Commands

| Command | Does |
|---|---|
| `status` [`--json`] | who holds what, and who is waiting -- read-only, never mutates state |
| `topology` [`--json`] | chips, boards, and the lease grain |
| `acquire` / `summon` `--chips N\|all\|LO-HI --who W` | take a lease on chips |
| `wait <ticket>` [`--timeout 8m`] | block until a queued ticket is granted |
| `queue` [`--json`] | list waiting requests |
| `cancel <ticket>` | drop a queue ticket |
| `renew <lease> --expect D` | extend a lease's advisory deadline |
| `release` / `banish <lease>` [`--no-reset`] [`--force`] | give chips back (resets them) |
| `reconcile` [`--sudo`] | re-derive state from kernel truth, reaping what is genuinely dead |
| `adopt <target> --who W` | wrap a lease around untracked running work |
| `env <lease>` | print export lines for a lease |
| `run --chips N -- <command>` | acquire, run a command, always release |
| `history` [`-n N`] [`--json`] | who held what, when, for how long -- the last 20 events by default |

Every command also accepts `--json` for machine-readable output.

## For agents

Point your agent at the two skills and one rule; that's the whole integration.

**The rule**, in a `CLAUDE.md` or equivalent:

> Before any command that opens `/dev/tenstorrent/*` — vLLM, tt-metal, TTNN, on-device
> pytest, `tt-smi -r` — take a lease with `gozer run --chips N --who "claude:<skill>" --
> <command>`, or `gozer acquire` for a longer session. Honour the `TT_VISIBLE_DEVICES` you
> are given. Release when done. Queued? Do non-hardware work, then `gozer wait <ticket>`.

**The skills**, which the installer links for you:

- `gozer-keymaster` — how to ask for chips, read the grant, behave while queued, release
- `gozer-gatekeeper` — what to do when the gate looks stuck, contended, or lying

**For non-Claude flows**, `--json` makes every command machine-readable and the exit codes
carry the outcome, so no output parsing is required:

| Code | Meaning |
|---|---|
| `0` | granted / success |
| `10` | queued — a ticket is on stdout |
| `11` | `wait` timed out, still queued |
| `12` | unavailable and queueing disabled |
| `13` | no such lease or ticket |
| `14` | topology unreadable |
| `15` | release refused, nothing done — a device is still open, or the lease's units now belong to someone else |
| `16` | the allocator mutex is stuck; the message names the path to clear |
| `130` | `gozer run` was interrupted by a signal (128 + SIGINT); the lease was still released |

Waiting is designed around agents: `acquire` never blocks, so an agent gets a ticket and can
go do other work. `gozer wait <ticket>` blocks for a bounded 8 minutes — deliberately under
the 10-minute ceiling on a Claude Code tool call — and returns either a grant or your current
position, so a loop is always safe.

## The one surprising thing

**Asking for one chip may correctly get you two.**

On a p300c, requesting one chip through UMD's constrained-device / visibility path may expose
**both** chips on the same board. UMD expands `TT_VISIBLE_DEVICES` to every chip on a board
holding two chips or fewer (`cluster_descriptor.cpp: create_constrained_cluster_descriptor`).

The caveat matters: direct TTNN device access can still open a single enumerated chip. So
single-chip work is not impossible — but `TT_VISIBLE_DEVICES`, the mechanism you would
normally reach for to fence a workload, **does not fence below board granularity** on such a
board.

That is precisely why gozer leases the board. A single-chip lease would be one you cannot
enforce: set `TT_VISIBLE_DEVICES` to one chip and the process may still see its neighbour's.
Handing out the pair is the only grant that means what it says.

gozer derives this from the same predicate rather than assuming a layout: **board grain when
boards hold ≤2 chips, chip grain otherwise.** So a QuietBox supports two concurrent tenants,
a p150 host one per card, and a Galaxy one per chip, with no configuration. When your grant
is wider than your request, the output says so instead of quietly rounding.

## What it guarantees, and what it doesn't

**Advisory, not enforced.** Device nodes are `crw-rw-rw-`; any process can ignore gozer
entirely. What gozer guarantees is that ignoring it is *visible* — such a chip shows as
`BUSY-UNTRACKED` and is never handed to anyone else.

**Leases are not owned.** Any local user can release, renew or reap any lease — nothing
checks who took it. That is deliberate, not an omission: releasing someone else's abandoned
lease is exactly what `gozer-gatekeeper` tells a human or a successor agent to do when an
agent dies mid-run, and an ownership check would wall off that recovery path. Exit `13`
means "no such lease or ticket", never "not yours". The lease records *who and why* so people
can coordinate; enforcement is not on offer here — see the bottom of this section.

**fd truth is same-user.** `/proc/<pid>/fd` is readable only by its owner, so ground truth
covers your own processes — the common case, where every agent runs as you. Cross-user
detection falls back to the world-readable lease files, i.e. cooperation.
`gozer reconcile --sudo` recovers full truth where passwordless sudo is available.

**State is per-machine.** `/tmp/tt-gozer` is local and does not sync.

For enforceable isolation, use device cgroups (`docker --device /dev/tenstorrent/N`) or
Kubernetes DRA via `tt-dra-driver`. The state format here is plain JSON precisely so a
supervisor can read or adopt it later.

## How it decides what's free

Six states, derived from comparing the lease files against the kernel:

| Lease? | fd open? | State | Meaning |
|---|---|---|---|
| ✓ | by the owner | `HELD` | working as intended |
| ✓ | by someone else | `HELD-FOREIGN` | the lease is lying — investigate |
| ✓ | none, owner alive (or, for a detached lease, still inside its 900s grace window) | `CLAIMED` | setup phase, or between runs |
| ✓ | none, owner gone (or, for a detached lease, its 900s grace window elapsed) | `STALE` | reaped by `acquire` or `reconcile` — `status` reports it but leaves it on disk; clear it with `gozer reconcile` |
| ✗ | yes | `BUSY-UNTRACKED` | untracked work — never handed out |
| ✗ | none | `FREE` | allocatable |

A *detached* lease is one with no supervised process — a bare `gozer acquire` rather than
`gozer run` — so there is no pid whose death reconciliation can watch for. Such a lease is
judged instead by a fixed 900-second grace window from the moment it was acquired: `CLAIMED`
until the window elapses or a fd opens, `HELD` as soon as anything opens the device (any
holder counts, so `HELD-FOREIGN` never applies to it), and reaped as `STALE` if the window
elapses with the device still unopened. `gozer run`'s lease is never detached, since its
process is supervised for the lease's whole lifetime.

Reaping is deliberately conservative: a non-detached lease is removed only when its process is
**gone** *and* no file descriptor remains; a detached lease is removed only once its grace
window has elapsed with no fd ever opened. A live process that has run past its advisory
`--expect` is flagged `OVERSTAYED` and left strictly alone. Nothing here is in a hurry to take
hardware away from something that might still be using it.

**`status` never reaps.** `gozer status` calls the same reconciliation `acquire` and
`reconcile` do, but with reaping turned off — it reports every state, including `STALE`, without
deleting anything. Only `gozer acquire` (as a side effect of asking "what's free?") and the
explicit `gozer reconcile` actually remove a stale lease. This matters: an earlier version of
`status` reaped by default, so the act of *looking* at a lease could destroy it — an
investigator ran `gozer status` on a lease they wanted to inspect, and it was gone by the time
they read the output. If `status` shows `STALE`, run `gozer reconcile` to clear it.

## State format

```
/tmp/tt-gozer/
  gate/<unit>.lock/lease.json    mkdir() is the atomic claim
  gate/<unit>.clean              unit was reset since its last release
  leases/<lease_id>.json         who, why, pid, since
  queue/<seq>-<ticket>.json      FIFO, zero-padded seq
  queue/.claim-window            ticket entitled to claim, 90s expiry
  mutex/.gatekeeper.lock/        allocator mutex, self-heals after 30s
  history.jsonl                  append-only log: granted, queued, released,
                                  reaped, adopted, refused -- one JSON object
                                  per line, forever (see below)
```

All plain files. Inspect with `ls` and `cat`; recover by hand if you ever need to.

`history.jsonl` is the one file here that is never deleted or rewritten, by design: it is the
only place `gozer` remembers anything past-tense. Everything else in this state directory is
present-tense and vanishes the moment a lease is released or reaped, which is exactly why
`gozer history` exists -- to answer "who had the chips two hours ago" after the gate itself has
already forgotten. Like the rest of `/tmp/tt-gozer`, it does not survive a reboot; that's a real
limitation of living in `/tmp`, not a secret. Each line is written with a single `write()` call
on a file opened `O_APPEND`, which is atomic on Linux for writes under `PIPE_BUF` (4096 bytes),
so concurrent agents logging at the same moment cannot interleave a torn line. Logging is
best-effort: a full disk or a permissions problem is swallowed rather than failing the command
that triggered it (never worse for `release`, which has already done its destructive work by
the time it logs).

## Safety properties

- **Never opens `/dev/tenstorrent/*`.** Topology comes from `/sys/class/tenstorrent/`,
  busy-state from `os.readlink` on `/proc/*/fd`. So `gozer status` is safe to run in the
  middle of somebody else's job.
- **`status` never mutates state, either.** It reconciles with reaping turned off, so looking
  at the gate cannot delete a lease as a side effect. Only `acquire` and `reconcile` reap.
- **Resets by PCI BDF only**, never by device index — `tt-smi -r` reads a bare integer as a
  UMD logical ID, a different namespace, which could reset the wrong device.
- **Never emits the board-wide M3/DMC reset.** `tt-smi -r <bdf>` is per-ASIC; `reset_m3` is
  not, and would reset a neighbour's chip out from under them.
- **Leases key on BDF**, not `/dev/tenstorrent/N`, because the vendor code re-resolves that
  index from the BDF after a post-reset hotplug.

## Testing

```bash
python3 -m pytest
```

No hardware required. `GOZER_ROOT`, `GOZER_SYSFS_ROOT`, `GOZER_PROC_ROOT` and
`GOZER_RESET_CMD` redirect every external dependency, so the suite runs anywhere and never
touches a device.

## The name

Gozer arrives only when the Keymaster and the Gatekeeper come together.

The mapping is load-bearing rather than decorative: `gatekeeper.py` really does guard the
gate — lock state, allocation, the queue, and who gets refused — while `keymaster.py` carries
your key and holds it for exactly as long as your process lives. What arrives when the two
meet is your workload, on the chips.

`summon` and `banish` work as aliases for `acquire` and `release`. That's as far as it goes;
everything else is named so you can guess it.

## License

Apache 2.0. See [LICENSE](LICENSE).
