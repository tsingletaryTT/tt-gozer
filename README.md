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

Project page: <https://tsingletarytt.github.io/tt-gozer/>

### From the Debian package

Download `tt-gozer_<version>_all.deb` from the
[GitHub releases page](https://github.com/tsingletaryTT/tt-gozer/releases), then:

```bash
sudo apt install ./tt-gozer_*_all.deb
gozer-install-skills
gozer status
```

The package installs `gozer` and `gozer-install-skills` under `/usr/bin`, the three skills under
`/usr/share/tt-gozer/skills`, a man page, and this README. `gozer-install-skills` links the three
skills into `~/.claude/skills/` for your user. Package installs run as root and cannot do that
for you.

The package ships the optional reconcile units, `gozer-reconcile.service` and
`gozer-reconcile.timer`, in `/usr/lib/systemd/user`. It installs them and does not enable them.
To turn them on for your user:

```bash
systemctl --user enable --now gozer-reconcile.timer
```

Resets (`gozer release`, `reset`, `reset-all`) run `tt-smi`, so one has to be on your PATH. The
package only suggests it. A QB2 already has `tt-smi` in the venv that `tt-installer` created, and
`apt` would otherwise install an older copy beside it.

Run gozer on the host. Inside a container it can read the host's `/sys/class/tenstorrent`, but
it only sees the container's own processes and keeps its lease records inside the container. It
would report chips as free that a process on the host holds, and agents on the host would not see
its leases.

### From a clone

Requires Python 3.10+. Standard library only: no dependencies, no virtualenv.

```bash
git clone https://github.com/tsingletaryTT/tt-gozer.git ~/code/tt-gozer
~/code/tt-gozer/install.sh
gozer status
```

The installer symlinks rather than copies, so `git pull` updates everything in place:

| What | Where | Why there |
|---|---|---|
| `bin/gozer` -> `~/.local/bin/gozer` | your `PATH` | so any shell, script or agent can call it |
| `skills/gozer-keymaster` -> `~/.claude/skills/` | Claude Code skills dir | teaches agents to lease before they run |
| `skills/gozer-gatekeeper` -> `~/.claude/skills/` | same | teaches agents to diagnose a stuck gate |

Skill directories are linked **individually**, never the parent. `~/.claude/skills/` usually
holds many unrelated skills and linking over it would clobber them.

Lock state lives in `/tmp/tt-gozer/`, created on first use. Nothing needs root. The history log
(`history.jsonl`) defaults to `~/.local/state/tt-gozer/`.

**Optional: periodic reconciliation.** Nothing reconciles unless a `gozer` command runs, so a
crashed agent's lease can sit stale for hours if nobody happens to invoke gozer in the
meantime. A real box hit this: a crashed lease's locks sat for over two hours because nothing
ran a gozer command. `contrib/gozer-reconcile.{service,timer}` run `gozer reconcile` every 5
minutes as a systemd **user** unit, so the gate self-heals even between commands. The installer
never enables this. It only prints the commands, because enabling a systemd unit changes
your machine in a way you did not ask for:

```bash
mkdir -p ~/.config/systemd/user
ln -sfn ~/code/tt-gozer/contrib/gozer-reconcile.service ~/.config/systemd/user/
ln -sfn ~/code/tt-gozer/contrib/gozer-reconcile.timer ~/.config/systemd/user/
systemctl --user enable --now gozer-reconcile.timer
```

The service is harmless if `gozer` is not installed or the sysfs topology is unreadable. It
checks for the binary first, swallows `gozer reconcile`'s own output, and never restarts on
failure, so a broken box gets one quiet failed-unit transition per tick instead of a noisy
journal.

### Releases

A tag named `v<version>` starts the release workflow in `.github/workflows/`. The workflow
checks that the tag matches the package version, builds the `.deb`, lints it with lintian,
install-tests it on Debian 12, Ubuntu 22.04 and Ubuntu 24.04, and publishes the release with
the package attached. There is no apt repository; the package is a release asset.

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
| `topology` [`--json`] | chips, boards, and the lease grain; shows the name of each board and chip when a names file exists |
| `names` [`--json`] | list boards and chips with their names, serials, ASIC ids, PCI addresses and current device indices, and flag boards that have no name and named boards that are missing |
| `names set <name> <board>` / `names unset <name>` | name a board (by serial, PCI address, device index or an existing name), or remove a name. `set` refuses a board that is under a lease |
| `names udev` | print udev rules for `/dev/tenstorrent/by-name/`; writes nothing |
| `acquire` / `summon` `--chips N\|all\|LO-HI --who W` [`--owner-pid N`] [`--service NAME` [`--port P`] [`--model ID`]] | take a lease on chips; the service fields say what it will serve (also on `run`, `adopt` and `renew`) |
| `wait <ticket>` [`--timeout 8m`] | block until a queued ticket is granted; while it waits it keeps the ticket alive and holding its boards |
| `queue` [`--json`] | list waiting requests, and the free boards each active waiter is holding |
| `cancel <ticket>` | drop a queue ticket |
| `renew <lease>` [`--expect D`] | heartbeat: restarts a detached lease's 900 s grace window, so work that will not open the device for a while can keep its chips by renewing at least every 15 minutes. `--expect` also extends the advisory deadline |
| `release` / `banish <lease>` [`--no-reset`] [`--force`] | give chips back (resets them, about 42 s). At chip grain the whole board is reset when the partner die is free. Older `tt-smi` releases opened every device while resetting, so another board showed `BUSY-UNTRACKED`; `tt-smi` 6.3.0 holds only the chips it resets |
| `grain` [`auto`\|`board`\|`chip`] | show the lease unit, or set it for the box (refused while anything is leased or queued) |
| `services` [`--check`] [`--live`] [`--json`] | leases that serve something (`--service`): model, port, chips, state — for a router to find them. Read-only; `--live` leaves out leases whose owner is gone; `--check` asks each for `GET /v1/models` |
| `reset <lease>` [`--force`] | reset the lease's chips in place and keep the lease; refuses while a device is open by one of your own processes (other users' processes are not visible, see "fd truth is same-user" below); the same cross-board window applies, so supervisors should not overlap resets |
| `reset-all --who W` [`--reason R`] | host-wide `tt-smi -r all`, under a lease on every unit. It never queues: it refuses (exit 12, naming what is in use) unless the whole box is free, so it cannot reset anyone's work. It marks units clean only if every chip reads normally afterwards, and it is the reset to use for a chip that stays unreadable. Exit 20 means tt-smi succeeded but a chip still reads all ones |
| `reconcile` [`--sudo`] | re-derive state from kernel truth, reaping what is genuinely dead. It names each lease it reaped, and warns when a reaped lease's owner pid was dead, because a container owned by another user is invisible to an unprivileged scan (`--sudo` checks theirs too) It also reports, and logs as `fence_violation`, any process of a supervised lease holding a device outside its grant |
| `adopt <target> --who W` | wrap a lease around untracked running work |
| `env <lease>` | print export lines for a lease |
| `run --chips N -- <command>` | acquire, run a command, always release. The release waits up to 10 s for the device to close (a fast-exiting command can leave a forked worker holding it for a moment) and refuses only if a process still holds it after that. 5, 30 and 120 s after start it checks that the command opened no device outside the grant, and warns (once per device, never kills) if it did |
| `history` [`-n N`] [`--json`] | who held what, when, for how long -- the last 20 events by default |
| `report` [`--out PATH`] [`--json`] | regenerate the containment-log page (`docs/log.html` by default inside a git checkout; `--out` is needed elsewhere) from `history.jsonl` |

Every command also accepts `--json` for machine-readable output.

`gozer report` turns the whole history log into `docs/log.html` -- the old "Containment Log"
page, which the site no longer links to -- recomputing every roster stat, hero number, and board/chip
mapping from the live log instead of anyone hand-typing them. It never touches hardware. Inside a
git checkout it writes `docs/log.html`; that file is generated, is listed in `.gitignore` and is not
committed. `--out` writes it somewhere else. `docs/index.html` is a different
page: the explainer, built by `scripts/build_site.py`, and `gozer report` never writes it. `--json` prints the
same computed stats without rendering HTML, for scripting.

## Names for boards and chips

`/dev/tenstorrent/N`, the `tt-smi` index and the ttnn device id are positions. The driver
hands them out in probe order, so they change when a chip fails to enumerate, when the driver
reloads, or when the PCI layout changes. gozer lists boards by serial, and on a two-board
Blackhole box the serial order and the device-index order can be opposite. "Board 0" then
means a different board depending on which tool answers.

A names file gives each board a short name once. gozer then accepts that name, and `name.N`
for a chip, anywhere it takes a board or chip: `acquire --exact`, `run --exact` and `adopt`.
The name is turned into the BDF or serial gozer already uses before anything is leased or
queued, so lease records and queue tickets keep storing BDFs and serials.

```toml
# ~/.config/gozer/names.toml
# The order of the [[board]] tables is the board order.
[[board]]
name = "left"
serial = "<board serial>"

[[board]]
name = "right"
serial = "<board serial>"

# Optional: pin a chip to a position inside its board by ASIC id, so a card
# moved to another slot keeps its name.
[[chip]]
board = "right"
position = 0
asic = "<asic id>"
```

`gozer names` lists the serials and ASIC ids on this machine. `gozer names set left <serial>`
writes the file for you and keeps your comments. `names unset` removes the comment lines
directly above the table it removes, since they describe it. Every machine has its own file, because every
machine has its own serials.

**Where the file is.** `$GOZER_NAMES` if set, else `$XDG_CONFIG_HOME/gozer/names.toml`, else
`~/.config/gozer/names.toml`. gozer reads it with a size limit (64 KiB) and does not follow a
symbolic link at the file itself. If the file is a link into a dotfiles repository, set
`GOZER_NAMES` to the file it leads to.

**Rules.**

* A name is a lowercase letter, then up to 31 lowercase letters, digits or hyphens. Names are
  case-sensitive. A name may not be `all`, a whole number, a PCI address, or a run of eight or
  more hex digits (which could be taken for a serial or ASIC id). A name that is also the serial
  or ASIC id of a chip on the machine is refused as ambiguous when it is used.
* A duplicate name, a serial listed twice, an unknown key or table, a `[[chip]]` for a board
  that has no table, and a position outside the board are errors. The message gives the file and
  line. If a file has an error, none of its lines take effect.
  `status` and `topology` still run, print a warning, and show boards without names; `gozer names`
  and any command given a name stop with exit 21.
* With a names file present, a token that is neither a name nor an identifier on this machine
  (`Left`, `left_1`) exits 21 instead of waiting in the queue for ever. With no file, only a
  token shaped like a name is refused; other unknown tokens behave as before.
* A named board that is not on this machine is reported as missing. The other boards still work.

**Order.** gozer prints boards in this order wherever it says "first" and "last": the boards
the file lists, in file order, then the boards without a name, sorted by serial. Chips inside a
board are in position order. Position is the PCI address order (the lowest address is `.0`),
unless a `[[chip]]` pins a chip to a position. `status` and `topology` (and their `--json`)
print in this order and add `name`, `board_name` and `position` keys; the existing keys mean what
they meant. With no names file `status` lists chips by device index as it always did.
Which chips an `acquire --chips N` picks is unchanged: it still prefers clean units, then the
lowest device index.

**What a name leases.** `--exact left` leases that board. `--exact right.1` leases that chip.
On a board of two chips the lease unit is the board, as it is for a BDF (UMD shows both chips of
a board), so `right.1` leases the board that holds the chip and says the grant was expanded. At
chip grain `left.2` leases one chip, and a board name works only for a board of one chip.
`TT_VISIBLE_DEVICES` is unchanged: gozer exports BDFs.

**Device nodes.** `gozer names udev` prints udev rules that create
`/dev/tenstorrent/by-name/<board>-<position>` (for example `left-0`) by matching `tt_asic_id`,
the way the `by-id` rule does. It writes nothing. Installing the rules needs root, so you do it:

```bash
gozer names udev | sudo tee /etc/udev/rules.d/60-tenstorrent-by-name.rules >/dev/null
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=tenstorrent --action=change
```

With those links a container, a script or a note can say `left-0` without gozer.

**Limits.**

* ttnn, tt-metal and `tt-smi` indices and mesh device ids are positions in whatever set of chips
  is visible. They carry no identity. Names choose which chips are visible; the mesh order inside
  a job still follows the fabric topology.
* gozer cannot tell which physical board a serial is. You say so once, with `gozer names set`.
* The kernel driver and UMD know nothing of these names. If this works well, `by-name` can be
  proposed there.
* Nothing in this feature opens `/dev/tenstorrent/*`. Identity comes from sysfs only.

## For agents

Point your agent at the skills and one rule; that's the whole integration.

**The rule**, in a `CLAUDE.md` or equivalent:

> Before any command that opens `/dev/tenstorrent/*` — vLLM, tt-metal, TTNN, on-device
> pytest, `tt-smi -r` — take a lease with `gozer run --chips N --who "claude:<skill>" --
> <command>`, or `gozer acquire` for a longer session. Honour the `TT_VISIBLE_DEVICES` you
> are given. Release when done. Queued? Do non-hardware work, then `gozer wait <ticket>`.

**The skills**, which the installer links for you:

- `gozer-keymaster` — how to ask for chips, read the grant, behave while queued, release
- `gozer-gatekeeper` — what to do when the gate looks stuck, contended, or lying
- `gozer-park` — how to hold a board through a model swap with `--owner-pid` and `gozer reset`

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
| `15` | release or reset refused, nothing done — a device is still open, or the lease's units now belong to someone else; for reset also a lease record that disagrees with its units, or has no units or chips, or a malformed chip id |
| `16` | the allocator mutex is stuck; the message names the path to clear |
| `17` | `reset` ran tt-smi and it failed; the lease is still held |
| `18` | `reset` ran, and afterwards the lease was gone or another lease held its chips; the reset may have hit chips that were no longer yours |
| `19` | `release` tore the lease down but its reset failed; the units are not marked clean (so `--fresh` refuses them) and the chips may be unreadable until reset. The output, and `recovery` in `--json`, give the commands. Before 0.3.5 this exited `0` |
| `20` | `reset-all` ran tt-smi and it succeeded, but some chips still read all ones; gozer has nothing further to try, so a human must power-cycle the box or reseat the card |
| `21` | a names-file or name problem: the file does not load (the message gives the line), or a name is unknown, ambiguous, reserved, or outside its board. Nothing was changed |
| `130` | `gozer run` was interrupted by a signal (128 + SIGINT); the lease was still released |

Waiting is designed around agents: `acquire` never blocks, so an agent gets a ticket and can
go do other work. `gozer wait <ticket>` blocks for a bounded 8 minutes — deliberately under
the 10-minute ceiling on a Claude Code tool call — and returns either a grant or your current
position, so a loop is always safe.

The queue is first come, first served, including for big requests. A waiter that has polled in
the last 10 minutes holds back the free boards it needs, so a `--chips all` request is not
starved by one-board jobs that keep taking each board as it frees. When chips free, the
90-second claim window goes to the first waiter they fit; while it is open, other waiters can
still take boards that waiter does not need. A `--ticket` that is no longer queued exits 13. A waiter that stops polling keeps its
place but stops holding boards, and is dropped after an hour without a poll.

## Several models side by side

gozer leases chips; it does not start models or route requests. To serve several models at once,
lease each one's chips, say what it serves, and let a router find it:

```bash
gozer run --chips 1 --who me:llama --service llama-3b --port 8001 --model meta-llama/Llama-3.2-3B-Instruct -- ./serve-llama.sh --port 8001
gozer run --chips 1 --who me:qwen  --service qwen-7b  --port 8002 -- ./serve-qwen.sh --port 8002
gozer services --check
```

`gozer services --live --json` is the list a router reads (`~/serve-gateway/router.py` on the QB2 routes
`/v1/*` requests by their `model` field this way). A server that opens more chips than it was
granted shows up in `status` as outside its grant, and `gozer run` warns about it.

## The one surprising thing

**By default, asking for one chip on a p300c gets you two.**

A p300c board carries two dies joined by an on-board Ethernet link. gozer leases the whole
board there by default (**board grain when boards hold ≤2 chips, chip grain otherwise**), so
a QuietBox supports two concurrent tenants, a p150 host one per card, and a Galaxy one per
chip, with no configuration. When your grant is wider than your request, the output says so.

The original reason was that UMD might widen `TT_VISIBLE_DEVICES` to the whole board. On
this box that is not what happens (measured 2026-11-11: ttnn 0.80.0, tt-smi 6.3.0, firmware
19.15.0, KMD 2.10.1):

- `TT_VISIBLE_DEVICES` set to one die's BDF opens **only that die**; the process holds only
  that `/dev/tenstorrent/N`. tt-metal then needs `TT_METAL_VISIBLE_DEVICES=0` and
  `TT_MESH_GRAPH_DESC_PATH` pointing at ttnn's `p150_mesh_graph_descriptor.textproto`, which
  a v6 tt-model bundle's `run.sh` sets for you.
- Two processes, one per die of the same board, run side by side.
- Resetting one board does not disturb work on the other, including a two-die mesh with
  fabric traffic, and the other board can be opened while the reset runs.
- Resetting **one die** leaves single-die work on its partner running, but takes the
  die-to-die link down: a mesh over both dies does not form again until the whole board is
  reset. Once, the partner's next fresh open also failed until the partner was reset.

So one lease per die works, as an opt-in for the box:

```bash
gozer grain chip     # lease single dies; refused while anything is leased or queued
gozer grain auto     # back to the derived default
```

At chip grain, releasing a die resets the **whole board** when the partner die is free (it is
locked for the reset and marked clean). When the partner is in use, only your die is reset
and the board is marked split: `status` says so, and a grant of both dies on a split board
says to run `gozer reset <lease>` before forming a mesh. The partner's own release later
resets the whole board and clears the mark.

## What it guarantees, and what it doesn't

**A crashed tenant's chips are flagged.** A lease reaped because its process died gives its
chips back without a reset. The next grant of those chips says so ("came back from a crashed
lease without a reset", and `unreset` in `--json`) and names `gozer reset <lease>` to clean
them; any later reset clears the flag.

**Advisory, not enforced.** Device nodes are `crw-rw-rw-`; any process can ignore gozer
entirely. What gozer guarantees is that ignoring it is *visible* — such a chip shows as
`BUSY-UNTRACKED` and is never handed to anyone else. The same holds inside a lease: when a process of a
supervised lease (`gozer run`, or `--owner-pid`) opens a device outside its grant, `status`
names the lease next to that chip ("pid N of lease L is outside its grant"), `gozer run`
warns, and `reconcile` logs it. This is the usual way side-by-side models collide: a server
configured for more devices than it was granted.

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
| ✓ | by the owner or a descendant of the owner | `HELD` | working as intended |
| ✓ | by a pid that is not the owner, the owner's process-group leader or a descendant of the owner | `HELD-FOREIGN` | the lease is lying — investigate |
| ✓ | none, owner alive (or, for a detached lease, still inside its 900s grace window) | `CLAIMED` | setup phase, or between runs |
| ✓ | none, owner gone (or, for a detached lease, its 900s grace window elapsed) | `STALE` | reaped by `acquire` or `reconcile` — `status` reports it but leaves it on disk; clear it with `gozer reconcile` |
| ✗ | yes | `BUSY-UNTRACKED` | untracked work — never handed out |
| ✗ | none | `FREE` | allocatable |
| ✗ | none | `DEGRADED` | free, but its board has fewer readable chips than its card type has (a p300 with one chip reading all ones); never handed out, because UMD would expand a grant to the dead chip |
| n/a | n/a | `UNREADABLE` | the chip's `tt_serial` reads all ones (it has fallen off the bus); never handed out, and `--exact` on it is refused |

A *detached* lease is one with no supervised process — a bare `gozer acquire` rather than
`gozer run` — so there is no pid whose death reconciliation can watch for. Such a lease is
judged instead by a fixed 900-second grace window from the moment it was acquired: `CLAIMED`
until the window elapses or a fd opens, `HELD` as soon as anything opens the device (any
holder counts, so `HELD-FOREIGN` never applies to it), and reaped as `STALE` if the window
elapses with the device still unopened. `gozer run`'s lease is never detached, since its
process is supervised for the lease's whole lifetime.

**`--owner-pid N`** opts a bare `acquire` out of detachment the same way `gozer run` does,
for a caller that can't exec the workload itself — e.g. `tt-station-agentd`, which takes a
lease, launches a serving container pinned to the granted chips, and releases when the
container stops. A root-owned process inside that container has file descriptors an
unprivileged gozer can never see, so a detached lease taken on its behalf would be reaped
~15 minutes in in a perfectly healthy session. Pass the supervisor's own pid with
`--owner-pid` and the lease is judged by that pid's liveness instead, exactly like `gozer
run`'s: `CLAIMED` for as long as the supervisor lives, no fd or grace window needed. A
non-positive pid, or one that is not alive right now, is refused rather than accepted.
If the `acquire` queues, the pid is kept on the ticket and `gozer wait` grants the lease
under it, so the lease still lives and dies with the supervisor and not with the waiter. The
pid is checked again at grant time; if it died while queued, the grant is detached (with a
note saying so), because a lease owned by a dead pid would be reaped at once.
Pass the supervisor's own pid, not a shell's or pid 1, because every live descendant of the
owner counts as the owner's own. A process that was re-parented to init after its parent
exited does not. For owner pid 1 only the pid itself and its process-group
leader count.

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
  history.jsonl                  (in ~/.local/state/tt-gozer by default) append-only log: granted, queued, released,
                                  reset, reaped, adopted, refused -- one JSON object
                                  per line, forever (see below)
```

All plain files. Inspect with `ls` and `cat`; recover by hand if you ever need to.

`history.jsonl` is the one file here that is never deleted or rewritten, by design: it is the
only place `gozer` remembers anything past-tense. Everything else in this state directory is
present-tense and vanishes the moment a lease is released or reaped, which is exactly why
`gozer history` exists -- to answer "who had the chips two hours ago" after the gate itself has
already forgotten. By default it lives in `~/.local/state/tt-gozer` (or
`$XDG_STATE_HOME/tt-gozer`) and survives a reboot, while leases/gate/queue stay ephemeral under
`/tmp/tt-gozer` (they *should* vanish on reboot, since the chips get reset). Before 0.3.9 the
log lived under `/tmp` with them and was lost at every reboot. If you name a root yourself
(`GOZER_ROOT`), the log stays beside it; `GOZER_HISTORY_ROOT` overrides both. The default is per
user: on a box with several users each sees the commands they ran, so point everyone's
`GOZER_HISTORY_ROOT` at one shared directory for a single timeline. Each line is written
with a single `write()` call on a file opened `O_APPEND`, which is atomic on Linux for writes
under `PIPE_BUF` (4096 bytes), so concurrent agents logging at the same moment cannot interleave
a torn line. Logging is best-effort: a full disk or a permissions problem is swallowed rather
than failing the command that triggered it (never worse for `release`, which has already done
its destructive work by the time it logs).

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

No hardware required. `GOZER_ROOT`, `GOZER_HISTORY_ROOT`, `GOZER_SYSFS_ROOT`, `GOZER_PROC_ROOT`
and `GOZER_RESET_CMD` redirect every external dependency, so the suite runs anywhere and never
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

This project is licensed under the **Apache License 2.0**. See the [LICENSE](LICENSE) file for details.
Source files carry an SPDX header (`Apache-2.0`, copyright Tenstorrent USA, Inc.).

### License understanding

This software assists in programming Tenstorrent products. However, making, using, or selling hardware,
models, or IP may require the license of rights (such as patent rights) from Tenstorrent or others. See
[LICENSE_understanding.txt](LICENSE_understanding.txt) for details.

### Other licenses

gozer uses only the Python standard library. The tools it coordinates (`tt-smi`, tt-metal, vLLM and the
models you run on leased chips) have their own licenses.
