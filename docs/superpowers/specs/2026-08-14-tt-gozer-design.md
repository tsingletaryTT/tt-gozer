# Design: tt-gozer — cooperative Tenstorrent chip leasing for agents

*Written 2026-08-14. Validated against the hardware and source on
a QuietBox 2 (2× p300c = 4 Blackhole ASICs, tt-kmd 2.9.0, tt-smi 5.3.0,
tt-umd 0.9.5). Audience: any agentic flow on a Tenstorrent box — Claude Code sessions,
aider, plain shell scripts, cron.*

## The problem

Several agents now share one Tenstorrent box. Nothing coordinates them. Two Claude
sessions, or a session and a background vLLM server, will happily open the same chips and
corrupt each other's runs. We want:

* An agent can declare "I am using this hardware now," visibly, to everyone else.
* An agent can ask for *N* chips without knowing which — one chip, all chips, or a range —
  and be told exactly which it got and how to target them.
* Requests queue when the box is busy. Who holds a lease and who is waiting are both
  first-class, legible facts.
* Non-Claude flows can participate with the same mechanism.

Explicitly **not** in scope: real OS-level enforcement. This is an advisory protocol,
cross-checked against kernel truth. See [Enforcement](#enforcement-and-its-limits).

## The metaphor, and what it maps to

Gozer arrives only when the Keymaster and the Gatekeeper come together. Here, *the coming
of Gozer* is your process getting the hardware.

| Name | Role | Lives in |
|------|------|----------|
| **gatekeeper** | Guards the gate. Owns the lock state, allocation, the reconciliation table, and the queue. Decides who is refused. | `gozer/gatekeeper.py` |
| **keymaster** | Carries the key. Requests a lease, emits `TT_VISIBLE_DEVICES`, runs the workload, returns the key on exit. | `gozer/keymaster.py` |
| **gozer** | What arrives when the two meet: your workload, on the chips. Also the CLI name. | `gozer/cli.py` |

The theme lives in module names, help text, and two command aliases. Primary command verbs
stay plain and guessable, because agents and scripts have to discover them.

## Hardware findings that constrain the design

These were established by reading source and sysfs on the box, not by running resets
against live work. They are the load-bearing facts; if any turns out false, revisit.

### Reset is per-ASIC, not per-board

`tt-smi -r` resets exactly the devices named. Verified through four layers:

1. **Selection** — `tt_smi/device_input.py` parses `-r` targets into a homogeneous list
   (UMD logical id / PCI BDF / `/dev/tenstorrent/N`) and passes it through unchanged. No
   board lookup exists anywhere in the reset path.
2. **Orchestration** — `tt_tools_common/reset_common/chip_reset.py:
   ChipReset.full_lds_reset` (the path taken for tt-kmd ≥ 2.4.1; this box runs 2.9.0)
   iterates `for pci_interface in pci_interfaces:` issuing `RESET_PCIE_LINK`, then
   `ASIC_RESET`, then `POST_RESET`. It never enumerates a board's other ASIC.
3. **PCIe** — each ASIC sits behind its own root port (`00:01.1/.3/.4/.5` → buses 01–04),
   so a secondary bus reset lands on a single endpoint. Had both ASICs on a card shared one
   downstream port, SBR would take both. They do not.
4. **KMD** — `blackhole.c: blackhole_reset()` for `TENSTORRENT_RESET_DEVICE_ASIC_RESET`
   calls `pcie_timer_interrupt(pdev)`, two config-space writes to that one `pci_dev`.

The one genuine board-level path is `reset_m3` / `ASIC_DMC_RESET`, which sends ARC
`TRIGGER_RESET` arg 3 — tt-umd's own docstring calls it "a M3 board level reset."
`tt-smi`'s CLI does not expose it (no flag in `--help`; the parameter defaults `False`), so
plain `tt-smi -r` never takes it. **gozer must never pass `reset_m3`.**

### Device visibility *is* board-granular

This is the constraint that sets the allocation grain.
`umd/device/cluster_descriptor.cpp: create_constrained_cluster_descriptor`:

```cpp
// Expand to same boards only for multi-board topologies (e.g. T3K: 2 chips per N300).
// Skip expansion for Galaxy-style (many chips per board) so TT_VISIBLE_DEVICES is honored.
expand_to_same_boards = true;
for (chip : visible_chips)
    if (get_board_chips(get_board_id_for_chip(chip)).size() > 2) {
        expand_to_same_boards = false; break;
    }
if (expand_to_same_boards)
    visible_chips = get_chips_from_same_boards(visible_chips);
```

A p300c has exactly 2 chips per board, and `2 > 2` is false, so expansion stays **on**.
Setting `TT_VISIBLE_DEVICES=2` silently yields chips {2,3}.

Stated precisely: **on a p300c, requesting one chip through UMD's constrained-device /
visibility path may expose both chips on the same board.** The caveat is that direct TTNN
device access can still open a single enumerated chip — so single-chip work is not
impossible, and an earlier draft of this document overstated it as such.

What follows for the design is unchanged, and in fact rests on the caveat rather than
despite it: the mechanism a caller would normally use to fence a workload,
`TT_VISIBLE_DEVICES`, **does not fence below board granularity** on a ≤2-chip board. A
single-chip lease would therefore be unenforceable — gozer could grant chip 2 while the
tenant's process still sees chip 3. Leasing the board is the only grant whose boundary is
real.

**Derived rule, not a hardcoded one:** the gatekeeper leases per *board* when a board holds
≤2 chips, and per *chip* when it holds more — the same predicate UMD uses. This is correct
on this QuietBox (2 tenants), on a p150 (1 chip/board → per-chip), and on Galaxy
(per-chip). tt-gozer is meant to travel between machines, so deriving beats assuming.

`TT_VISIBLE_DEVICES` accepts BDF strings or integer chip ids
(`get_target_chip_ids_from_visible_devices`). gozer emits **BDFs** — unambiguous and
stable. `TT_METAL_VISIBLE_DEVICES` is a separate, higher-layer filter recorded in
`llrt/rtoptions.cpp`; gozer does not set it.

### The ethernet mesh is real and hardwired

The p300c mesh is prewired inside the box (older QuietBoxes required external QSFP cabling
between cards). All four ASICs show live links — `ETH_LIVE_STATUS` is non-zero on each.
Reset performs ethernet link training on re-init (hence `--eth_train_skip`). Whether
resetting one ASIC perturbs a neighbour's links is a hardware question that source reading
cannot settle, so gozer **warns** about leased live-eth neighbours rather than blocking. If
practice shows resets do wedge neighbours, the warning becomes a hard block with no
redesign.

> Note: the `tt-hardware-primer` skill currently claims QB2 has no inter-chip Ethernet.
> That is wrong and should be fixed separately; it is not a dependency of this work.

### Everything needed is readable without touching a device

`/sys/class/tenstorrent/tenstorrent!<N>/` exposes `tt_serial` (board serial — the board
grouping key), `tt_asic_id` (durable per-ASIC identity), `tt_card_type`, `tt_aiclk`, and
`tt_heartbeat` (changing = firmware alive).
`/sys/bus/pci/devices/<bdf>/tenstorrent/tenstorrent!N` maps BDF → device index.

Observed on this box:

| dev | BDF | `tt_serial` | `tt_asic_id` | card |
|-----|-----|-------------|--------------|------|
| 0 | 0000:01:00.0 | 0000000000000002 | 1111111111111111 | p300c |
| 1 | 0000:02:00.0 | 0000000000000002 | 2222222222222222 | p300c |
| 2 | 0000:03:00.0 | 0000000000000001 | 3333333333333333 | p300c |
| 3 | 0000:04:00.0 | 0000000000000001 | 4444444444444444 | p300c |

**Requirement:** gozer must never open `/dev/tenstorrent/*`. Status has to be safe to run
during someone else's active workload.

### Device index is not guaranteed stable

After reset the device hotplug-removes and reappears;
`ChipReset.wait_for_device_to_reappear()` re-resolves the index *from the BDF*. In practice
indices are stable on this box, but the vendor code declines to assume it, so leases key on
BDF. `/dev/tenstorrent/N` is display only.

## Architecture

A single Python 3 package (stdlib only) exposing one CLI, `gozer`. Two thin Claude skills
tell agents which command to run and how to read the output. A rule in the global
`~/CLAUDE.md` makes leasing the default expectation for every session.

```
agent (Claude / aider / script)
        │  gozer acquire --chips 2 --who "claude:ttm-optimize"
        ▼
   keymaster ──── asks ───▶ gatekeeper
                              │ reads ──▶ /sys/class/tenstorrent/*   (topology, liveness)
                              │ reads ──▶ /proc/*/fd                 (truth: who holds a chip)
                              │ writes ─▶ /tmp/tt-gozer/             (leases, queue)
                              │ runs ───▶ tt-smi -r <bdf>            (on release only)
        ▼
   TT_VISIBLE_DEVICES=0000:03:00.0,0000:04:00.0    ← the gate is open; Gozer may come
```

Why a CLI rather than instructions inside each skill: atomicity and stale-reaping are
race-sensitive and belong in testable code, not in prose an agent re-derives each time; and
non-Claude flows need the same entry point.

### Module layout

| Module | Responsibility |
|--------|----------------|
| `gozer/topology.py` | Read sysfs → chips, boards, liveness. Never opens a device node. |
| `gozer/gatekeeper.py` | State dir, atomic lock/unlock, allocation, reconciliation, queue. |
| `gozer/keymaster.py` | Lease acquisition, env emission, `run` process supervision. |
| `gozer/reset.py` | `tt-smi -r` invocation for exactly the named BDFs. Never `reset_m3`. |
| `gozer/cli.py` | Argument parsing, human and `--json` rendering, exit codes. |

Each is independently testable: topology from a fixture tree, gatekeeper against a temp
state dir, keymaster against a fake gatekeeper, reset against a stub command.

## State layout

```
/tmp/tt-gozer/                      # mode 1777, sticky — multi-user safe
  topology.json                     # cached view of sysfs; refreshed when the BDF set changes
  gate/<serial>.lock/               # mkdir() is the atomic acquire primitive
      lease.json
  leases/<lease_id>.json            # full lease record
  queue/<seq>-<ticket>.json         # FIFO, ordered by zero-padded seq
  .gatekeeper.lock/                 # briefly-held global mutex around the critical section
```

`mkdir` is the primitive because it is atomic on every POSIX filesystem, avoids
flock-inheritance surprises across subshells, and leaves an artifact a human can inspect or
remove by hand. The parent directory is sticky, so one user cannot remove another's lock
directory.

`lease.json`:

```json
{
  "lease_id": "2f9a1c",
  "chips": ["0000:03:00.0", "0000:04:00.0"],
  "dev_indices": [2, 3],
  "board_serial": "0000000000000001",
  "who": "claude:ttm-optimize",
  "human": "ttuser",
  "host": "quietbox",
  "pid": 902311,
  "pgid": 902300,
  "session": "e212aa0a",
  "cwd": "/home/ttuser/code/tt-metal",
  "reason": "decoder perf sweep",
  "since": "2026-08-14T12:40:11Z",
  "expect_done": "2026-08-14T13:25:00Z",
  "reset_on_release": true,
  "state": "active"
}
```

`expect_done` is an **advisory hint shown to the queue**. It is never a reaper — see
[Reconciliation](#reconciliation--fd-truth-over-bookkeeping).

`--who` is free-form; the convention is `claude:<skill>`, `aider:<task>`, `human:<name>`,
`cron:<job>`. `session` comes from the Claude session id when present.

## Allocation

### Grain

At startup the gatekeeper groups chips by `tt_serial` and computes the lease unit with
UMD's own predicate: **board if that board has ≤2 chips, else chip**. On this box the unit
is the board, giving 2 concurrent tenants.

### Request forms

| Form | Meaning |
|------|---------|
| `--chips 1` | at least one chip; on this box grants a whole board, and says so |
| `--chips 2` | two chips |
| `--chips all` | every chip on the host |
| `--chips 1-4` | elastic: take what is free, at least 1, at most 4 |
| `--exact 0000:03:00.0` or `--exact 2` | a named chip/board |
| `--fresh` | require chips reset since their last release |

### Selection policy

When more is free than requested, prefer in order: (1) boards with no other tenant, to
minimise eth-neighbour disturbance; (2) already-reset/clean boards; (3) lowest device
index, for determinism in tests.

### Allocation reaps as a side effect

Selection asks `free_units` which units are free, and `free_units` reconciles first with
reaping enabled. So **allocating garbage-collects leases whose owning process is gone**,
deleting their lease records and releasing their gate locks — including other tenants'.

This is deliberate and load-bearing, not an oversight. A unit counts as free only when
every one of its chips reads `FREE`; a `STALE` unit — dead pid, no open fd, chips
genuinely idle — would otherwise stay unallocatable until a human ran `gozer reconcile` by
hand, so one crashed agent would block the box indefinitely. Reaping a lease whose process
is completely gone is garbage collection, not claiming, and it is what lets the gate
recover from a crash unattended.

Two consequences follow. Allocation must run inside `critical_section`, so the reap cannot
race a concurrent claim. And `allocate` is not a safe dry-run primitive: anything wanting a
speculative "what would I get" answer must reconcile with `reap=False` and reason about
`STALE` itself, rather than calling `allocate`.

### Grant output

Human-readable by default, `--json` for scripts. Always includes the export line:

```
$ gozer acquire --chips 1 --who "claude:tt-generate" --reason "sdxl smoke"
granted: chips 2,3  (board 0000000000000001, p300c)
  note: asked for 1 — UMD expands TT_VISIBLE_DEVICES to the whole 2-chip board
  export TT_VISIBLE_DEVICES=0000:03:00.0,0000:04:00.0
  lease 2f9a1c   release with: gozer release 2f9a1c
  ⚠ live eth links to chips 0,1 (held by claude:tt-serve-llm)
```

## Command surface

```
gozer status [--json] [--watch]
gozer topology [--json]
gozer acquire --chips N|all|LO-HI [--exact T] [--who W] [--reason R]
              [--expect DURATION] [--fresh] [--owner-pid N] [--json]
gozer wait <ticket> [--timeout 8m] [--json]
gozer queue [--json]
gozer cancel <ticket>
gozer renew <lease> [--expect DURATION]
gozer release <lease> [--no-reset] [--force]
gozer reconcile [--sudo] [--json]
gozer adopt <chips> --who W [--reason R]
gozer env <lease>
gozer run --chips N [options] -- <command...>
```

Aliases, because the theme earns two: `gozer summon` → `acquire`, `gozer banish` →
`release`. Documented, never the only spelling.

`gozer run` acquires, execs the command with `TT_VISIBLE_DEVICES` set, and releases on exit
**including on signal or crash**. It is the form skills and scripts should prefer, because
the lease cannot leak.

Exit codes: `0` granted/success · `10` queued (ticket on stdout) · `11` wait timed out,
still queued · `12` unavailable and queueing disabled · `13` no such lease or ticket ·
`14` topology unreadable.

*Amended during implementation:* `13` originally read "lease not found **or not owned**".
Nothing checks ownership, and nothing should: `gozer-gatekeeper` deliberately tells a human
or a successor agent to release someone else's stale lease, and enforcement would break that
recovery path. The tool is advisory by design (see [Enforcement](#enforcement-and-its-limits)),
so the documentation was corrected rather than the code.

*Amended during implementation:* `--refresh` is gone from `topology` — there is no topology
cache to bypass, and a flag that is accepted and never read is a placebo. Three codes were
added: `15` release refused and nothing done (a device is still open, or the lease's units
now belong to another lease) — previously overloaded onto `12`, which told an agent to wait
for hardware when it needed to stop its own workload; `16` the allocator mutex is stuck,
message naming the path to clear; and `130` for `gozer run` interrupted by a signal, the
conventional 128 + SIGINT.

*Amended after real use:* `16` was briefly reachable from `status` too, for one release —
`status` used to reap as a side effect of looking (see the STALE-state amendment above), and
a reap that finds something to delete takes the mutex to do it. Now that `status` is
`reap=False`, it never takes the mutex at all, so `16` is reachable only from commands whose
reconcile actually reaps: `acquire`/`run`/`wait` (via `free_units`) and the explicit
`reconcile`.

## Reconciliation — fd truth over bookkeeping

The lease file records *who and why*. The kernel decides *free or busy*. `reconcile` runs
implicitly before every `acquire` (with reaping on) and `status` (with reaping off — see the
STALE-state amendment above).

For each chip: `fd_busy` = does any process hold `/dev/tenstorrent/N` open (scan
`/proc/*/fd` symlinks), and `leased` = is there a lock directory.

| leased | fd open | state | action |
|--------|---------|-------|--------|
| ✓ | by the lease's pid/pgid | **HELD** | leave alone |
| ✓ | by another pid | **HELD-FOREIGN** | flag loudly; never reallocate |
| ✓ | none, pid alive | **CLAIMED** | honour — setup phase or between runs |
| ✓ | none, pid dead | **STALE** | reaped by `acquire`/`reconcile`; `status` reports it but leaves it on disk (see below); mark board dirty (needs reset) |
| ✗ | yes | **BUSY-UNTRACKED** | never allocate; suggest `gozer adopt` |
| ✗ | none | **FREE** | allocatable |

*Amended during real use.* `status` originally called `reconcile()` with its `reap=True`
default — the same call `acquire` makes — so the command documented as the safe one to run
**deleted leases as a side effect of looking at them**. Found the hard way: an investigator ran
`gozer status` to inspect a two-hour-old lease, and the act of looking reaped it, destroying the
record before it could be read. `cmd_status` now calls `reconcile(reap=False)`: it reports every
state, including `STALE`, without deleting anything. `free_units()` (and therefore `acquire`) is
unchanged and must stay `reap=True` — a `STALE` unit would otherwise stay unallocatable until a
human ran `gozer reconcile` by hand, so one crashed agent would block the box. `reconcile` remains
the explicit, intentional reaper. A `STALE` chip in `status` output now names the fix: `gozer
reconcile` clears it.

Reaping requires the pid to be **gone** *and* no fd open. A lease whose process is alive is
never force-reaped; if it runs past `expect_done` it is displayed as `OVERSTAYED` and left
for a human or the queue head to act on. The governing principle: never be greedy, make
sure a process is completely done, then reset and start clean.

`gozer adopt` retroactively wraps a lease around an untracked-but-busy chip, so
pre-existing work (like a manually started vLLM server) becomes visible to the queue
instead of looking free.

### Detached leases

*Added during implementation, after the CLI exposed a hole in the rule above.*

The reaping rule assumes a lease's `pid` is the workload's. That holds for `gozer run`,
which owns its child. It does **not** hold for a standalone `gozer acquire`: that process
prints the export line and exits immediately, so its recorded pid is dead within
milliseconds. Reconciliation would then see a dead owner with no fd and reap a lease that
was seconds old — the documented workflow (`acquire`, `eval "$(gozer env …)"`, start the
workload) racing itself, with any concurrent `status` or `acquire` handing the chips away
before the workload ever opened a device.

Binding such leases to an always-alive pid "fixes" this by making them immortal, which is
worse: one crashed agent then wedges the box permanently and no `reconcile` can recover it.

So a lease acquired without an owning process is marked **detached**, and reconciliation
treats it on its own terms:

| Condition | State | Why |
|---|---|---|
| Any fd open on the chip | `HELD` | The workload started. Its pid is unknowable at acquire time, so a holder that is not the creating pid is expected — `HELD-FOREIGN` applies only to non-detached leases, where we do know who should hold it. |
| No fd, within the grace window from `since` | `CLAIMED` | The acquire-to-workload-start gap. Legitimately minutes: a server can spend a long time loading weights before touching a device. |
| No fd, past the grace window | `STALE` → reaped | Stops an abandoned `acquire` wedging the box. |

`DETACHED_GRACE_SECONDS` defaults to 900. It is deliberately generous: reaping too early
corrupts a live run, reaping too late costs a delay. `--expect` is **not** wired into it and
remains advisory, never a reaper.

`gozer run` is unaffected — it owns a real process and keeps exact pid semantics.

### Supervisor-owned leases

*Added 2026-08-16, for tt-station-agentd.* `gozer run` fixes the detached problem by owning
the workload itself — but not every caller can. tt-station-agentd takes a lease, launches a
serving container pinned to the granted chips, and releases when the container stops; it
never execs the workload as its own child the way `gozer run` does; the workload's real
process lives inside a container namespace, root-owned.

That root-owned process is exactly the case a bare detached lease cannot cover safely: its
file descriptors under `/proc/<pid>/fd` are readable only by their owner, so an unprivileged
gozer can never see them, ever, at any point in the container's life — this isn't the
acquire-to-workload-start gap the grace window forgives, it is permanent invisibility. A
lease taken for that container would sit with no observable fd, age past
`DETACHED_GRACE_SECONDS`, and be reaped as `STALE` roughly fifteen minutes into a perfectly
healthy session — the chips would read `FREE` while the container kept using them, which is
the exact collision gozer exists to prevent.

`gozer acquire --owner-pid N` covers this: `N` is the *supervisor's* own pid (long-lived,
outside the container), passed through as `acquire`'s pre-existing `pid` parameter — the same
one `gozer run` already uses for its own child. The resulting lease is **not detached**: it
is judged in `Gatekeeper.reconcile` by ordinary `pid_alive(N)` the same as a `gozer run`
lease, not by elapsed time. It stays `CLAIMED` for exactly as long as the supervisor lives —
no fd required, no grace window, no fifteen-minute surprise — and is reaped the moment the
supervisor is gone (and no fd remains), exactly like any other non-detached lease.

`--owner-pid` is validated before it ever reaches `Keymaster.acquire`: a non-positive value,
or a pid that is not alive at acquire time (checked against the configured proc root, never
the real `/proc`, so tests stay hermetic), is refused outright rather than accepted. Handing
back a lease for an already-dead pid would make it reapable the instant it was created —
worse than refusing with a clear message.

The flag is wired into `cmd_acquire` only, deliberately not `cmd_wait`: a ticket replayed by
`wait` belongs to whoever is waiting *now*, and `wait` never re-derives a `pid` from the
ticket it replays (see the Queue section) — inheriting a stale supervisor pid recorded on an
old ticket would misattribute a fresh grant to a supervisor that may no longer even be the
one waiting.

## Queue

Tickets are FIFO by zero-padded sequence number. `acquire` never blocks: it returns a grant
or a ticket with position and who is ahead. `wait <ticket>` blocks up to 8 minutes by
default — deliberately under Claude Code's 10-minute Bash ceiling — and returns granted or
still-queued, so an agent can loop or interleave other work.

When chips free, the head of queue gets a **90-second exclusive claim window**. If it does
not claim in time its ticket moves to the back, which prevents both a lurker stealing the
slot and a dead waiter blocking the queue forever.

*Amended during implementation.* This section originally read "a ticket whose creating
process has exited is dropped at reconcile." That is not implementable, because **no gozer
process ever survives to wait on a ticket**: `acquire` prints its ticket and exits, and
`run` prints `queued: ticket X` and returns 10 just the same. Every ticket's recorded pid
is therefore dead within milliseconds, and pruning on liveness deleted tickets that were
working exactly as intended — `gozer wait` came back "no longer queued" about a second
after the ticket was issued.

A ticket is instead dropped once it has gone unclaimed for `TICKET_MAX_AGE_SECONDS`
(**3600**, in `gozer/queue.py`), measured from its `since` stamp. One hour is deliberately
generous: a queued agent is *expected* to go and do other work, and `wait` only blocks 8
minutes at a time, so a patient caller may be away for several wait-and-work cycles.
Dropping too early loses a real waiter's place in line; dropping too late costs at most one
unclaimed 90-second window, which the claim-window expiry already sends to the back. The
ticket's `pid` and `detached` fields are recorded for humans reading `queue/*.json`, and are
not consulted. If a blocking run mode is ever added, that mode — and only that mode — could
reintroduce a liveness check for its own tickets.

Elastic requests (`1-4`) are satisfied at whatever is available above the minimum, so a
large request cannot starve behind a stream of small ones — the head of queue is served
first regardless of size.

## Release

1. Verify no fds remain on the leased chips. If any do, refuse (`--force` overrides, with a
   warning) — a lease must not be released out from under live work.
2. **Re-read the gate under the mutex**, then drop the mutex.
3. Unless `--no-reset`, and only if step 2 says the units are still ours: run
   `tt-smi -r <bdf>[,<bdf>]` for exactly the leased chips. Never `reset_m3`.
4. **Re-read the gate under the mutex again.**
5. Mark the board clean, remove the lock directory and lease file.
6. Prune the queue, then notify its head by opening a claim window.

`--no-reset` exists for fast handoff between related runs by the same owner.

*Amended during implementation.* Steps 2 and 4 were added because a lease record is not
proof that the gate still holds it: stale leases get released by hand (the gatekeeper skill
says to), `reconcile` reaps them automatically, and the `/proc` scan in step 1 takes
hundreds of milliseconds — long enough for another agent's `acquire` to own the unit before
step 3 runs. Three outcomes, depending on what the re-read finds:

| Gate says | Release does |
|---|---|
| the units are still ours | the full sequence above |
| a unit now carries a **different** lease | **abort**, touch nothing, exit `15`. Resetting would hit the new tenant mid-startup, and the teardown would delete their lock. |
| a unit carries **no** lease at all | **skip the reset** — those chips are free for anyone to take, so a reset would land on whoever grabs them next — but still delete our own lease record, and say so. |

The mutex is deliberately **not** held across the reset: `tt-smi -r` takes tens of seconds
and would block every other user of the box for all of it. That leaves a narrowed but real
window — see "known limitations" in the project `CLAUDE.md`.

## Skills and wiring

**`gozer-keymaster`** — the lifecycle skill. Triggers before any work that touches TT
hardware. Covers: decide how many chips are needed, acquire, interpret grant/queue output,
what to do while queued, honour `TT_VISIBLE_DEVICES`, release when done. Points at
`gozer run` as the default.

**`gozer-gatekeeper`** — the "something looks stuck" skill. Triggers on contention,
suspected stale locks, or before a reset. Covers: read `gozer status` and `reconcile`,
distinguish the six states, adopt untracked work, decide whether a reset is safe given eth
neighbours, and when to leave an overstayed lease alone.

Skill `description:` fields lead with the functional trigger, not the metaphor, so agent
matching works: an agent looking for "how do I get chips" must find `gozer-keymaster`
without knowing Ghostbusters.

**`~/CLAUDE.md` rule** (in `tt-home/dotfiles/CLAUDE.md`, symlinked): before any command
that opens `/dev/tenstorrent/*` — vLLM, tt-metal, TTNN, `tt-smi -r`, on-device pytest —
acquire a lease; honour `TT_VISIBLE_DEVICES`; release when done; contended →
`gozer-keymaster`; stuck → `gozer-gatekeeper`.

The ~30 existing `tt-*` skills are deliberately **not** edited. Many are upstream-managed
and would lose the change on next sync; the global rule covers them.

## Packaging and cross-repo wiring

tt-gozer is its own repo (`~/code/tt-gozer`, flat in `~/code` like other personal repos).
tt-home remains the machine-to-machine transport and gains only pointers.

| Path | Purpose |
|------|---------|
| `tt-gozer/gozer/` | the package (Python 3, stdlib only) |
| `tt-gozer/bin/gozer` | entry point shim |
| `tt-gozer/tests/` | hardware-free test suite |
| `tt-gozer/skills/gozer-keymaster/SKILL.md` | lifecycle skill |
| `tt-gozer/skills/gozer-gatekeeper/SKILL.md` | triage skill |
| `tt-gozer/install.sh` | symlink CLI into `~/.local/bin`, skills into `~/.claude/skills` |
| `tt-gozer/README.md` | protocol + state-format docs for non-Claude consumers |
| `tt-gozer/CLAUDE.md` | project log per the global convention |

Changes in tt-home:

* `clone_my_repos.sh` — add `tsingletaryTT/tt-gozer` to `REPOS`.
* `dotfiles/CLAUDE.md` — add the "lease first" rule.
* `dotfiles/install.sh` — add a `--- TT gozer ---` section that runs
  `~/code/tt-gozer/install.sh` when the repo is present, and says so when it is not.

The two skill directories are linked **individually** into `~/.claude/skills/` — never the
parent, which already holds ~30 unrelated skills.

## Testing

`GOZER_ROOT` overrides the state directory and `GOZER_SYSFS_ROOT` points at a fixture tree,
so the entire suite runs with no hardware and no `/tmp` pollution. Reset is stubbed via
`GOZER_RESET_CMD`. Test-driven per the global CLAUDE.md.

Coverage required:

* Concurrent `acquire` from N processes grants disjoint chip sets, never overlapping.
* Grain derivation: 2-chip boards → board grain; 1-chip and 4-chip boards → chip grain.
* `--chips 1` on a 2-chip board grants 2 and reports the expansion.
* All six reconciliation states, each reached deliberately.
* Stale reap fires only when pid is dead **and** no fd; a live pid past `expect_done`
  yields `OVERSTAYED`, not a reap.
* Queue FIFO order; claim-window expiry moves the ticket to the back; dead waiter's ticket
  is dropped.
* Elastic `LO-HI` grants within bounds and does not starve behind small requests.
* `adopt` converts BUSY-UNTRACKED to HELD.
* `gozer run` releases on normal exit, non-zero exit, and SIGTERM/SIGINT.
* Release refuses while an fd is open; `--force` overrides with a warning.
* `status` opens no file under `/dev/tenstorrent/` (assert via fixture instrumentation).

## Enforcement and its limits

State these plainly in the README rather than implying safety that is not there.

* **Advisory, not enforced.** Any process can ignore gozer and open any chip; the device
  nodes are `crw-rw-rw-`. What the tool guarantees is that ignoring it is *visible*, via
  the BUSY-UNTRACKED state.
* **fd truth is same-user.** `/proc/<pid>/fd` is readable only by its owner, so ground
  truth covers your own processes — the common case here, where every agent runs as
  `ttuser`. Cross-user detection degrades to cooperation through the world-readable lease
  files. `reconcile --sudo` recovers full truth where sudo is available.
* **State is per-machine.** `/tmp/tt-gozer` is local. The repo syncs the *tooling* between
  machines, never the lock state.
* **Real isolation belongs elsewhere.** Device-cgroup or container enforcement
  (`docker --device /dev/tenstorrent/N`) is the enforceable answer; a related internal
  design sketch for per-chip leasing on a single box already covers this ground against
  an agentd-style lease table. The state format here is deliberately plain JSON so a
  similar agent daemon could read or adopt it later, and DRA/`tt-dra-driver` remains the
  path if the box ever joins a cluster.

## Observability: the append-only history log

*Added after real use, 2026-08-15/16.* Everything above is present-tense: `gate/` and
`leases/` describe the gate *right now*, and forget a lease the moment it is released or
reaped. There was no way to answer "who had the chips two hours ago", "how long do leases
actually run", or "how often do we contend" -- the founding requirement (who holds the device
is legible) held only in the present tense.

`gozer/history.py` adds `<GOZER_ROOT>/history.jsonl`: one JSON object per line, appended
forever, never rewritten. It lives inside `GOZER_ROOT` (not a per-user path like
`~/.local/state`) because the point is one shared timeline across every agent on the box; a
per-user path would fragment it. Like the rest of `/tmp/tt-gozer`, it does not survive reboot.

Each write is a single `os.write()` to an fd opened `O_APPEND`; on Linux that is atomic under
`PIPE_BUF` (4096 bytes), so concurrent agents cannot interleave a torn line. Every write is
best-effort -- an `OSError` from `open()` or `write()` is swallowed -- because a full disk or
permissions problem must never fail an `acquire` or, worse, a `release`.

Six events, logged at their one true source: `granted`/`queued` in `Keymaster.acquire`,
`released`/`refused` in `Keymaster.release`, `reaped` in `Gatekeeper.reconcile`'s reap loop,
`adopted` in `cli.cmd_adopt`. `gozer history [-n N] [--json]` reads it back: the last 20 events
by default, human-readable and newest-last, or raw records with `--json`.

## Optional periodic reconciliation

*Added after real use, 2026-08-15/16.* Nothing reconciles unless a `gozer` command runs. On a
real box, a crashed lease's locks sat for over two hours because nothing invoked the tool in
between -- the gate *looked* held with nobody holding it.

`contrib/gozer-reconcile.{service,timer}` are a systemd **user** unit pair: a oneshot service
running `gozer reconcile`, triggered by a timer every 5 minutes (`Persistent=false` -- there is
nothing to "catch up" on a missed tick; whatever is stale now will still be stale in 5 more
minutes). `install.sh` never enables this: it only prints the two commands, because enabling a
systemd unit changes the user's machine in a way they did not ask for. The service is harmless
if `gozer` is absent or the topology is unreadable -- it checks for the binary before invoking
it, swallows `gozer reconcile`'s own stdout/stderr, and never restarts on failure, so a broken
box produces one quiet failed-unit transition per tick rather than a noisy journal.

## Open items

* Confirm on hardware whether resetting one ASIC perturbs a live neighbour's ethernet
  links. Until then the warning stands rather than a block. Needs an idle window.
* Fix the `tt-hardware-primer` skill's incorrect "QB2 has no inter-chip Ethernet" claim
  (separate change, not a dependency).
* If two concurrent tenants proves too few in practice, revisit per-chip leases carrying a
  documented "TTNN will widen you to the board" caveat.
