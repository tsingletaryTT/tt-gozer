"""Command-line surface for gozer.

Verbs stay plain and guessable because agents and scripts have to discover them.
The metaphor gets exactly two aliases: `summon` for acquire, `banish` for release.
"""

from __future__ import annotations

import argparse
import json as jsonlib
import os
import sys
import time

from gozer import __version__, procfd
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Grant, Keymaster, TicketNotFound, parse_duration
from gozer.topology import TopologyError

EXIT_OK = 0
EXIT_QUEUED = 10
EXIT_WAIT_TIMEOUT = 11
EXIT_UNAVAILABLE = 12
# "No such lease or ticket" -- and nothing more. Ownership is deliberately
# NOT checked anywhere: the gatekeeper skill tells a human or a successor
# agent to release someone else's stale lease, which is the recovery path an
# ownership check would break. gozer is advisory, and any local user can
# release any lease; see the README's guarantees section.
EXIT_NO_LEASE = 13
EXIT_NO_TOPOLOGY = 14
# Release refused and did nothing: a device is still open, or the units have
# moved on to another lease. Distinct from 12, which means "no chips for you
# right now" -- overloading that told an agent to go wait for hardware when
# what it actually needed was to stop its own workload first.
EXIT_RELEASE_REFUSED = 15
# The allocator mutex could not be taken. Newly reachable from read-only
# commands: `status` reconciles, and a reconcile that finds something to reap
# now takes the mutex to do it (so it cannot delete a lock another process
# just created). A cross-user wedged mutex therefore surfaces here rather
# than as a traceback -- and `gozer status` is exactly where the gatekeeper
# skill sends someone whose gate looks wedged.
EXIT_GATE_STUCK = 16
# Conventional 128 + SIGINT, for `gozer run` interrupted by a signal.
EXIT_INTERRUPTED = 130


def _make(args) -> tuple[Gatekeeper, Keymaster]:
    gk = Gatekeeper(
        root=os.environ.get("GOZER_ROOT"),
        sysfs_root=os.environ.get("GOZER_SYSFS_ROOT"),
        proc_root=os.environ.get("GOZER_PROC_ROOT", "/proc"),
    )
    return gk, Keymaster(gk)


def _emit(payload: dict, human: str, as_json: bool) -> None:
    print(jsonlib.dumps(payload, indent=2, sort_keys=True) if as_json else human)


# ---- commands -------------------------------------------------------------

def cmd_status(args) -> int:
    gk, _ = _make(args)
    states = gk.reconcile()
    payload = {
        "grain": gk.grain,
        "chips": [
            {"dev_index": s.chip.dev_index, "bdf": s.chip.bdf,
             "board": s.chip.serial, "card": s.chip.card, "state": s.state,
             "who": (s.lease or {}).get("who"), "pid": (s.lease or {}).get("pid"),
             "reason": (s.lease or {}).get("reason"),
             "pids_holding": s.pids, "overstayed": s.overstayed}
            for s in states
        ],
        "queue": gk.queue_entries(),
    }
    lines = [f"grain: {gk.grain}   ({len(gk.boards)} boards, {len(states)} chips)"]
    current_board = None
    for s in states:
        if s.chip.serial != current_board:
            current_board = s.chip.serial
            lines.append(f"board {current_board}  ({s.chip.card})")
        who = (s.lease or {}).get("who", "")
        pid = (s.lease or {}).get("pid", "")
        extra = f"  {who} pid {pid}" if who else ""
        if s.state == "BUSY-UNTRACKED":
            extra = f"  pid {','.join(map(str, s.pids))} — no lease; try: gozer adopt"
        if s.overstayed:
            extra += "  OVERSTAYED"
        lines.append(f"  chip {s.chip.dev_index}  {s.chip.bdf}  {s.state:<15}{extra}")
    q = gk.queue_entries()
    if q:
        lines.append("queue:")
        for i, e in enumerate(q, 1):
            lines.append(f"  {i}) {e['who']}  wants {e['min_chips']} chip(s)"
                         f"  since {e['since']}")
    _emit(payload, "\n".join(lines), args.json)
    return EXIT_OK


def cmd_topology(args) -> int:
    gk, _ = _make(args)
    payload = {
        "grain": gk.grain,
        "boards": [
            {"serial": b.serial, "card": b.card,
             "chips": [{"dev_index": c.dev_index, "bdf": c.bdf,
                        "asic_id": c.asic_id} for c in b.chips]}
            for b in gk.boards
        ],
    }
    lines = [f"grain: {gk.grain}"]
    for b in gk.boards:
        lines.append(f"board {b.serial}  ({b.card})")
        for c in b.chips:
            lines.append(f"  chip {c.dev_index}  {c.bdf}  asic {c.asic_id}")
    _emit(payload, "\n".join(lines), args.json)
    return EXIT_OK


def _render_grant(grant: Grant, gk) -> str:
    idx = ",".join(str(i) for i in grant.dev_indices)
    lines = [f"granted: chips {idx}  (units {', '.join(grant.units)})"]
    if grant.expanded:
        lines.append(f"  note: asked for {grant.requested} — UMD expands "
                     "TT_VISIBLE_DEVICES to the whole board")
    lines.append(f"  export TT_VISIBLE_DEVICES={','.join(grant.bdfs)}")
    lines.append(f"  lease {grant.lease_id}   release with: "
                 f"gozer release {grant.lease_id}")
    for bdf, who in grant.neighbours.items():
        lines.append(f"  ! live eth neighbour {bdf} held by {who} — "
                     "your reset on release may perturb it")
    return "\n".join(lines)


def cmd_acquire(args) -> int:
    gk, km = _make(args)
    # No `pid` is passed here: a bare `gozer acquire` has no process for
    # gozer to supervise (it prints its export line and exits immediately),
    # so Keymaster.acquire records this as a *detached* lease. See its
    # docstring and Gatekeeper.reconcile's DETACHED_GRACE_SECONDS handling --
    # a fixed sentinel pid (e.g. always-alive init) was tried and rejected,
    # since that makes a detached lease immortal instead of just outliving
    # the one-shot CLI process that created it.
    result = km.acquire(args.chips, who=args.who, reason=args.reason,
                        exact=args.exact, fresh=args.fresh, expect=args.expect,
                        ticket=args.ticket, no_queue=args.no_queue)
    if isinstance(result, Grant):
        payload = {
            "granted": True, "lease_id": result.lease_id, "units": result.units,
            "chips": result.bdfs, "dev_indices": result.dev_indices,
            "expanded": result.expanded, "requested": result.requested,
            "neighbours": result.neighbours,
            "env": km.env_for(result),
        }
        _emit(payload, _render_grant(result, gk), args.json)
        return EXIT_OK

    if result is None:
        # --no-queue: acquire refused *before* taking a ticket, so there is
        # nothing left on disk to clean up here.
        _emit({"granted": False, "queued": False}, "unavailable: no chips free",
              args.json)
        return EXIT_UNAVAILABLE

    pos = gk.queue_position(result["ticket"])
    ahead = [e["who"] for e in gk.queue_entries()[:max(pos - 1, 0)]]
    payload = {"granted": False, "queued": True, "ticket": result["ticket"],
               "position": pos, "ahead": ahead}
    human = (f"queued: ticket {result['ticket']}  position {pos}\n"
             f"  ahead: {', '.join(ahead) if ahead else '(none)'}\n"
             f"  do other work, then: gozer wait {result['ticket']}")
    _emit(payload, human, args.json)
    return EXIT_QUEUED


def cmd_wait(args) -> int:
    gk, km = _make(args)
    deadline = time.monotonic() + parse_duration(args.timeout)
    while time.monotonic() < deadline:
        entry = next((e for e in gk.queue_entries()
                      if e["ticket"] == args.ticket), None)
        if entry is None:
            _emit({"granted": False, "ticket": args.ticket, "reason": "gone"},
                  f"ticket {args.ticket} is no longer queued", args.json)
            return EXIT_NO_LEASE
        if gk.may_claim(args.ticket):
            # No explicit pid here either -- same detached lease as
            # cmd_acquire (see its comment). `wait` claims on behalf of
            # whichever CLI invocation originally enqueued the ticket, and
            # that invocation is long gone by now.
            #
            # The whole original request is replayed from the ticket, not
            # just its chip count: --exact, --fresh, --reason and --expect
            # are part of what was asked for, and dropping them hands a
            # queued agent something it did not ask for. `chips_spec` is
            # absent on tickets written by an older gozer still sitting in
            # the queue, hence the min-max fallback.
            result = km.acquire(
                entry.get("chips_spec")
                or f"{entry['min_chips']}-{entry['max_chips']}",
                who=entry["who"], reason=entry.get("reason"),
                exact=entry.get("exact"), fresh=entry.get("fresh", False),
                expect=entry.get("expect"), ticket=args.ticket)
            if isinstance(result, Grant):
                payload = {"granted": True, "lease_id": result.lease_id,
                           "chips": result.bdfs, "env": km.env_for(result)}
                _emit(payload, _render_grant(result, gk), args.json)
                return EXIT_OK
        time.sleep(1)

    pos = gk.queue_position(args.ticket)
    _emit({"granted": False, "ticket": args.ticket, "position": pos},
          f"still queued at position {pos}; run "
          f"`gozer wait {args.ticket}` again", args.json)
    return EXIT_WAIT_TIMEOUT


def cmd_queue(args) -> int:
    gk, _ = _make(args)
    entries = gk.queue_entries()
    human = "\n".join(
        f"{i}) {e['who']}  wants {e['min_chips']} chip(s)  ticket {e['ticket']}"
        for i, e in enumerate(entries, 1)) or "(queue empty)"
    _emit({"queue": entries}, human, args.json)
    return EXIT_OK


def cmd_cancel(args) -> int:
    gk, _ = _make(args)
    if gk.queue_position(args.ticket) is None:
        _emit({"cancelled": False}, f"no such ticket: {args.ticket}", args.json)
        return EXIT_NO_LEASE
    gk.dequeue(args.ticket)
    _emit({"cancelled": True}, f"cancelled {args.ticket}", args.json)
    return EXIT_OK


def cmd_renew(args) -> int:
    gk, _ = _make(args)
    lease = gk.read_lease(args.lease)
    if lease is None:
        _emit({"renewed": False}, f"lease {args.lease} not found", args.json)
        return EXIT_NO_LEASE
    from gozer.keymaster import _deadline
    lease["expect_done"] = _deadline(args.expect)
    gk.write_lease(lease)
    for unit in lease.get("units", []):
        gk.update_unit_lease(unit, lease)
    _emit({"renewed": True, "expect_done": lease["expect_done"]},
          f"lease {args.lease} now expected done {lease['expect_done']}", args.json)
    return EXIT_OK


def cmd_release(args) -> int:
    _, km = _make(args)
    ok, msg = km.release(args.lease, no_reset=args.no_reset, force=args.force)
    _emit({"released": ok, "message": msg}, msg, args.json)
    if ok:
        return EXIT_OK
    # Two failure shapes, two codes: there is no such lease (13), or the
    # release was refused and nothing was done (15).
    return EXIT_NO_LEASE if "not found" in msg else EXIT_RELEASE_REFUSED


def cmd_reconcile(args) -> int:
    gk, _ = _make(args)
    if args.sudo:
        gk.use_sudo = True
    states = gk.reconcile(reap=True)
    payload = {"chips": [{"dev_index": s.chip.dev_index, "state": s.state}
                         for s in states],
               "sudo": bool(args.sudo)}
    human = "\n".join(f"chip {s.chip.dev_index}  {s.state}" for s in states)
    if args.sudo and not procfd.sudo_available():
        human += ("\n! --sudo requested but passwordless sudo is unavailable; "
                  "results cover your own processes only")
    _emit(payload, human, args.json)
    return EXIT_OK


def cmd_adopt(args) -> int:
    gk, km = _make(args)
    from gozer import procfd
    from gozer.gatekeeper import utcnow
    fd_map = procfd.holders(gk.proc_root)
    target = gk._resolve_exact(args.target)
    if target is None:
        _emit({"adopted": False}, f"unknown chip or board: {args.target}", args.json)
        return EXIT_NO_LEASE

    chips = gk.chips_in_unit(target)
    pids = sorted({p for c in chips for p in fd_map.get(c.dev_index, [])})
    if not pids:
        _emit({"adopted": False}, f"{args.target} has no process holding it",
              args.json)
        return EXIT_UNAVAILABLE

    lease_id = gk.new_lease_id()
    lease = {
        "lease_id": lease_id, "chips": [c.bdf for c in chips],
        "dev_indices": [c.dev_index for c in chips], "units": [target],
        "who": args.who, "pid": pids[0], "pgid": pids[0],
        "reason": args.reason or "adopted pre-existing work",
        "since": utcnow(), "expect_done": None,
        "reset_on_release": True, "state": "adopted",
    }
    if not gk.claim_unit(target, lease):
        _emit({"adopted": False}, f"{target} is already leased", args.json)
        return EXIT_UNAVAILABLE
    gk.write_lease(lease)
    _emit({"adopted": True, "lease_id": lease_id, "pids": pids},
          f"adopted {target} for {args.who} (pid {pids[0]}), lease {lease_id}",
          args.json)
    return EXIT_OK


def cmd_env(args) -> int:
    gk, km = _make(args)
    lease = gk.read_lease(args.lease)
    if lease is None:
        _emit({"error": "not found"}, f"lease {args.lease} not found", args.json)
        return EXIT_NO_LEASE
    env = km.env_for(lease)
    _emit(env, "\n".join(f"export {k}={v}" for k, v in env.items()), args.json)
    return EXIT_OK


def cmd_run(args) -> int:
    _, km = _make(args)
    if not args.command:
        print("nothing to run: put the command after --", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        return km.run(args.command, chips_spec=args.chips, who=args.who,
                      reason=args.reason, exact=args.exact, fresh=args.fresh,
                      expect=args.expect)
    except KeyboardInterrupt:
        # run()'s signal forwarder deliberately raises when a signal arrives
        # with no live child to forward it to, so control unwinds through the
        # try/finally that releases the lease. By the time it reaches here the
        # lease is already released; all that is left is to honour run()'s
        # `-> int` contract instead of letting a traceback out of the CLI.
        print("gozer run: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


# ---- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gozer",
        description="Cooperative Tenstorrent chip leasing. The keymaster must "
                    "meet the gatekeeper for the coming of Gozer.")
    p.add_argument("--version", action="version", version=f"gozer {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text, aliases=()):
        sp = sub.add_parser(name, help=help_text, aliases=list(aliases))
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.set_defaults(func=fn)
        return sp

    add("status", cmd_status, "who holds what, and who is waiting")
    # No --refresh: there is no topology cache to bypass (sysfs is read once
    # per process, which is already the freshest thing available). A flag
    # that is accepted and never read is the same defect as the old --sudo
    # placebo, so it is not offered.
    add("topology", cmd_topology, "chips, boards, and the lease grain")

    for name, aliases in (("acquire", ("summon",)),):
        a = add(name, cmd_acquire, "take a lease on chips", aliases=aliases)
        a.add_argument("--chips", default="1", help="N, all, or LO-HI")
        a.add_argument("--who", required=True, help="e.g. claude:ttm-optimize")
        a.add_argument("--reason", default=None)
        a.add_argument("--exact", default=None, help="a BDF, device index, or unit")
        a.add_argument("--fresh", action="store_true", help="require reset chips")
        a.add_argument("--expect", default=None, help="advisory duration, e.g. 45m")
        a.add_argument("--ticket", default=None, help="claim against a queue ticket")
        a.add_argument("--no-queue", action="store_true",
                       help="fail instead of queueing")

    w = add("wait", cmd_wait, "block until a ticket is granted")
    w.add_argument("ticket")
    w.add_argument("--timeout", default="8m",
                   help="bounded so it fits inside an agent tool call")

    add("queue", cmd_queue, "list waiting requests")
    c = add("cancel", cmd_cancel, "drop a queue ticket")
    c.add_argument("ticket")

    r = add("renew", cmd_renew, "extend a lease's advisory deadline")
    r.add_argument("lease")
    r.add_argument("--expect", required=True)

    for name, aliases in (("release", ("banish",)),):
        rel = add(name, cmd_release, "give chips back (resets them)", aliases=aliases)
        rel.add_argument("lease")
        rel.add_argument("--no-reset", action="store_true")
        rel.add_argument("--force", action="store_true",
                         help="release even while a device is open")

    rec = add("reconcile", cmd_reconcile, "re-derive state from kernel truth")
    rec.add_argument("--sudo", action="store_true",
                     help="use sudo for cross-user fd visibility")

    ad = add("adopt", cmd_adopt, "wrap a lease around untracked running work")
    ad.add_argument("target", help="a BDF, device index, or unit key")
    ad.add_argument("--who", required=True)
    ad.add_argument("--reason", default=None)

    e = add("env", cmd_env, "print export lines for a lease")
    e.add_argument("lease")

    rn = add("run", cmd_run, "acquire, run a command, always release")
    rn.add_argument("--chips", default="1")
    rn.add_argument("--who", default="shell:gozer-run")
    rn.add_argument("--reason", default=None)
    rn.add_argument("--exact", default=None)
    rn.add_argument("--fresh", action="store_true")
    rn.add_argument("--expect", default=None)
    rn.add_argument("command", nargs=argparse.REMAINDER)

    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    try:
        return args.func(args)
    except TopologyError as e:
        print(f"gozer: {e}", file=sys.stderr)
        return EXIT_NO_TOPOLOGY
    except TimeoutError as e:
        # critical_section composes a message that names the mutex path, its
        # owning uid when that is the problem, and how to clear it by hand --
        # print it as-is rather than a traceback. Caught before the
        # ValueError arm for the same reason TicketNotFound is: a stuck gate
        # is not a bad argument.
        print(f"gozer: {e}", file=sys.stderr)
        return EXIT_GATE_STUCK
    except TicketNotFound as e:
        # Checked before the ValueError arm below (TicketNotFound is one), so
        # a vanished ticket reports "no such lease or ticket" rather than
        # "unavailable", which an agent would read as "the box is busy".
        print(f"gozer: {e}", file=sys.stderr)
        return EXIT_NO_LEASE
    except ValueError as e:
        print(f"gozer: {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
