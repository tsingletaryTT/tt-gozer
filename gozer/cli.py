# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Command-line surface for gozer.

Verbs stay plain and guessable because agents and scripts have to discover them.
The metaphor gets exactly two aliases: `summon` for acquire, `banish` for release.
"""

from __future__ import annotations

import argparse
import json as jsonlib
import urllib.parse as urllib_parse
import os
import sys
import time

from gozer import __version__, history, procfd, report
from gozer import names as names_mod
from gozer.gatekeeper import DETACHED_GRACE_SECONDS, Gatekeeper
from gozer.keymaster import Grant, Keymaster, TicketNotFound, parse_duration
from gozer.topology import TopologyError, all_chips

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
# The allocator mutex could not be taken. Reachable from any command whose
# reconcile finds something to reap -- the reap takes the mutex to do it (so
# it cannot delete a lock another process just created) -- which today means
# `acquire`/`run`/`wait` (via free_units) and the explicit `reconcile`. NOT
# `status`: status runs reconcile(reap=False), precisely so it never mutates
# state or takes the mutex, so a stuck mutex cannot make the one command
# documented as "safe to run any time" hang or fail. (This was reachable from
# `status` too for one release, back when status still reaped as a side
# effect of looking -- see cmd_status's comment.) A cross-user wedged mutex
# therefore surfaces here rather than as a traceback, from whichever of the
# commands above the gatekeeper skill sends someone to run.
EXIT_GATE_STUCK = 16
# `gozer reset` ran tt-smi and it failed. The lease is untouched.
EXIT_RESET_FAILED = 17
# `gozer reset` ran, and afterwards the lease was gone or another lease held the
# unit. The reset may have hit chips that were no longer ours. Distinct from 15,
# which means nothing was done.
EXIT_RESET_CHANGED_HANDS = 18
# `gozer release` tore the lease down, but its reset failed: the lease is gone,
# the units are not marked clean (so --fresh refuses them) and the chips may be
# unreadable until reset. The output carries the recovery commands. Distinct
# from 0 (clean), 13 (no such lease) and 15 (nothing was done).
EXIT_RELEASE_RESET_FAILED = 19
# `gozer reset-all` ran tt-smi and it succeeded, but some chips still read all
# ones. gozer has nothing further to try; the output says what a human must do.
EXIT_RESET_ALL_INCOMPLETE = 20
# A names-file or name problem (see gozer/names.py): the file does not load, or a
# name is unknown, ambiguous, reserved, or points outside its board. Nothing was
# changed: names are resolved before any lease or ticket is touched.
EXIT_NAMES = 21
# Conventional 128 + SIGINT, for `gozer run` interrupted by a signal.
EXIT_INTERRUPTED = 130


def _make(args) -> tuple[Gatekeeper, Keymaster]:
    gk = Gatekeeper(
        root=os.environ.get("GOZER_ROOT"),
        sysfs_root=os.environ.get("GOZER_SYSFS_ROOT"),
        proc_root=os.environ.get("GOZER_PROC_ROOT", "/proc"),
        history_root=os.environ.get("GOZER_HISTORY_ROOT"),
    )
    return gk, Keymaster(gk)


def _emit(payload: dict, human: str, as_json: bool) -> None:
    print(jsonlib.dumps(payload, indent=2, sort_keys=True) if as_json else human)


# ---- commands -------------------------------------------------------------

UNREADABLE_HINT = ("not responding (reads all ones), so it is not grantable; "
                   "a failed `gozer release` reset leaves this. A human must "
                   "reset or power-cycle it: do not run tt-smi -r by hand "
                   "while other leases are live")


OTHER_USERS_HINT = ("processes of other users (a container) are not visible "
                    "to this scan, so a container may still hold the chip; "
                    "`gozer reconcile --sudo` checks theirs too")

DEGRADED_HINT = ("its board partner is unreadable, so a grant would hand you "
                 "the dead chip too; run `gozer reset-all` when the whole box "
                 "is free")


def _warn_names(gk) -> None:
    """Say on stderr that the names file was not applied. Read-only commands
    carry on and show boards without names, so a typo in the file does not stop
    `status`."""
    if gk.names_error is not None:
        print(f"gozer: names not applied: {gk.names_error.message}",
              file=sys.stderr)


def _name_fields(gk, chip) -> dict:
    """The JSON keys status and topology add for a chip."""
    return {"name": gk.names.chip_name(chip),
            "board_name": gk.names.board_name(chip.serial),
            "position": gk.names.position_of(chip)}


def _names_payload(gk) -> dict:
    return {"names_error": (gk.names_error.message if gk.names_error else None),
            "names_missing": [{"name": e.name, "serial": e.serial}
                              for e in gk.names.missing]}


def _display_order(gk, items, chip_of):
    """Order chips as gozer says first and last. Without a names file that is
    device-index order, which is what status has always printed; with one it is
    the names order (file boards, then the rest by serial; positions inside)."""
    if not gk.names.file.boards or gk.names_error is not None:
        return sorted(items, key=lambda x: chip_of(x).dev_index)
    rank = {c.bdf: (bi, ci) for bi, b in enumerate(gk.boards)
            for ci, c in enumerate(b.chips)}
    return sorted(items, key=lambda x: rank.get(chip_of(x).bdf, (10**6, 0)))


def _refuse_unreadable_exact(gk, exact, as_json) -> int | None:
    """Exit code if --exact names an unreadable or degraded chip, else None.
    Without this the request would match no free unit and sit in the queue for
    good."""
    if not exact:
        return None
    chip = gk.unreadable_chip(exact)
    if chip is not None:
        msg = (f"chip {chip.dev_index} ({chip.bdf}) is unreadable: "
               f"{UNREADABLE_HINT}")
    else:
        chip = next((c for c in all_chips(gk.boards)
                     if exact in (c.bdf, str(c.dev_index))
                     and gk.short_board_of(c)), None)
        if chip is None:
            return None
        have, want = gk.short_board_of(chip)
        msg = (f"chip {chip.dev_index} ({chip.bdf}) is degraded: its board has "
               f"{have} of {want} chips readable; {DEGRADED_HINT}")
    _emit({"granted": False, "error": msg}, f"gozer: {msg}", as_json)
    return EXIT_UNAVAILABLE


def _queue_view(gk, states) -> list[dict]:
    """Queue entries plus what each is doing now: `active` (its waiter polled
    recently enough to hold units) and `holding` (free units held back for it,
    see Gatekeeper.plan_queue). Built from `states` -- a reap=False reconcile
    -- so looking at the queue never reaps anything."""
    by_chip = {s.chip.bdf: s.state for s in states}
    free = [u for u in gk.all_units()
            if gk.chips_in_unit(u)
            and all(by_chip.get(c.bdf) == "FREE" for c in gk.chips_in_unit(u))]
    reserved, _ = gk.plan_queue(free=free)
    out = []
    for e in gk.queue_entries():
        holding = sorted(u for u, t in reserved.items() if t == e["ticket"])
        out.append({**e, "active": gk.queue.is_active(e), "holding": holding})
    return out


def _queue_line(gk, i: int, e: dict) -> str:
    line = f"{i}) {e['who']}  wants {e['min_chips']} chip(s)"
    if e.get("holding"):
        names = []
        for u in e["holding"]:
            chips = gk.chips_in_unit(u)
            bname = gk.names.board_name(chips[0].serial) if chips else None
            names.append(bname or u)
        line += f"  holding {', '.join(names)}"
    elif not e.get("active", True):
        line += "  (idle: not holding boards)"
    return line


# No "/": a router reaches a service as /svc/<name>/<path>. fullmatch, so a
# trailing newline is not accepted the way "$" would.
SERVICE_NAME_RE = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
LIVE_STATES = ("HELD", "CLAIMED")


def _add_service_args(parser) -> None:
    parser.add_argument("--service", default=None,
                        help="name of what this lease serves (e.g. llama-3b); "
                        "listed by `gozer services` for a router to find")
    parser.add_argument("--port", type=int, default=None,
                        help="port the service listens on (with --service)")
    parser.add_argument("--model", default=None,
                        help="model id the service answers to, as in "
                        "/v1/models (with --service; default: the service name)")


def _service_from_args(args, current: dict | None = None
                       ) -> tuple[dict | None, str | None]:
    """(service record, None) or (None, error). No --service, no record.

    `current` is the lease's existing record (renew): given fields replace
    its fields, the rest are kept, so `renew L --port 9001` moves only the
    port."""
    name = getattr(args, "service", None)
    port = getattr(args, "port", None)
    model = getattr(args, "model", None)
    if current and name is None and (port is not None or model is not None):
        name = current.get("name")
    if name is None:
        if port is not None or model is not None:
            return None, "--port and --model describe a --service; name it"
        return None, None
    if not SERVICE_NAME_RE.fullmatch(name):
        return None, f"--service {name!r}: use letters, digits and . _ : -"
    if port is not None and not 1 <= port <= 65535:
        return None, f"--port {port}: not a TCP port"
    if model is not None and not model.strip():
        return None, "--model must not be empty"
    if current and current.get("name") == name:
        port = port if port is not None else current.get("port")
        model = model or current.get("model")
    return {"name": name, "port": port, "model": model or name}, None


def _service_clash(gk, service: dict | None, lease_id: str | None = None) -> str | None:
    """Another live lease already serving this name or port? Two leases
    behind one name or port would send a router's requests to whichever it
    found first."""
    if not service:
        return None
    for lease in gk.held_units().values():
        other = lease.get("service")
        if not other or lease.get("borrowed_for_reset") or \
                lease.get("lease_id") == lease_id:
            continue
        if other.get("name") == service["name"]:
            return (f"service {service['name']!r} is already served by lease "
                    f"{lease.get('lease_id')} ({lease.get('who')})")
        if service.get("port") and other.get("port") == service["port"]:
            return (f"port {service['port']} is already used by service "
                    f"{other.get('name')!r} (lease {lease.get('lease_id')})")
    return None


def cmd_services(args) -> int:
    """What is being served, where, on which chips -- for a router.

    Read-only, like status: leases are judged with reap=False. `--check`
    asks each service with a port for GET /v1/models (1 s timeout)."""
    gk, _ = _make(args)
    states = gk.reconcile(reap=False)
    state_of = {}
    for st in states:
        if st.lease and not st.lease.get("borrowed_for_reset"):
            state_of.setdefault(st.lease.get("lease_id"), st.state)
    seen, out = set(), []
    for lease in gk.held_units().values():
        lid = lease.get("lease_id")
        svc = lease.get("service")
        if not svc or lid in seen or lease.get("borrowed_for_reset"):
            continue
        seen.add(lid)
        entry = {"service": svc.get("name") or "?",
                 "model": svc.get("model") or svc.get("name") or "?",
                 "port": svc.get("port"),
                 "url": (f"http://127.0.0.1:{svc['port']}"
                         if svc.get("port") else None),
                 "lease_id": lid, "who": lease.get("who"),
                 "chips": lease.get("chips", []),
                 "state": state_of.get(lid)}
        # `live`: the lease's owner is alive (HELD or CLAIMED). A STALE lease
        # (owner gone, not yet reaped) is listed for humans but must not be
        # routed to.
        entry["live"] = entry["state"] in LIVE_STATES
        if args.live and not entry["live"]:
            continue
        if args.check and entry["url"]:
            entry["up"] = _probe(entry["url"] + "/v1/models")
        out.append(entry)
    out.sort(key=lambda e: (e["service"] or "", e["lease_id"] or ""))
    human = "\n".join(
        f"{e['service']:<20} model {e['model']:<28} "
        f"{(':' + str(e['port'])) if e['port'] else '(no port)':<8} "
        f"chips {','.join(e['chips'])}  {e['state'] or '?'}"
        + (("  up" if e.get("up") else "  DOWN") if "up" in e else "")
        + f"  lease {e['lease_id']} ({e['who']})"
        for e in out) or "(no services)"
    _emit({"services": out}, human, args.json)
    return EXIT_OK


def _probe(url: str) -> bool:
    """GET on localhost, directly: no $http_proxy, no redirects followed. Any
    HTTP answer below 500 counts as up (a server behind an API key answers
    401 and is still up)."""
    import http.client
    parts = urllib_parse.urlsplit(url)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=1)
        conn.request("GET", parts.path or "/")
        status = conn.getresponse().status
        conn.close()
        return status < 500
    except Exception:  # noqa: BLE001
        return False


def cmd_status(args) -> int:
    gk, _ = _make(args)
    # reap=False: `status` is documented as the safe, read-only command --
    # the one the gatekeeper skill sends someone to when the gate looks
    # wedged, including to look at evidence after an incident. It must never
    # delete anything as a side effect of being looked at. This was found on
    # a real box: an investigator ran `gozer status` on a two-hour-old lease,
    # and the act of looking reaped it (reconcile's old reap=True default),
    # destroying the record before it could be read.
    #
    # This means `status` can now show STALE chips it used to silently clean
    # up. That is the point -- it is honest about what is actually on disk.
    # `acquire` (via free_units) and `reconcile` still reap with reap=True;
    # that stays load-bearing there (see free_units' comment) and
    # deliberate here (reconcile is the explicit, intentional reaper).
    states = _display_order(gk, gk.reconcile(reap=False), lambda s: s.chip)
    violations = gk.fence_violations()
    _warn_names(gk)
    payload = {
        "grain": gk.grain,
        "grain_setting": gk.grain_setting(),
        "split_boards": sorted(b.serial for b in gk.boards
                               if gk.is_split(b.serial)),
        "chips": [
            {"dev_index": s.chip.dev_index, "bdf": s.chip.bdf,
             "board": s.chip.serial, "card": s.chip.card, "state": s.state,
             "who": (s.lease or {}).get("who"), "pid": (s.lease or {}).get("pid"),
             "reason": (s.lease or {}).get("reason"),
             "pids_holding": s.pids, "overstayed": s.overstayed,
             "owner_dead": s.owner_dead,
             "holder_containers": {
                 str(p): cid for p in s.pids
                 if (cid := procfd.container_id(p, gk.proc_root))},
             **_name_fields(gk, s.chip)}
            for s in states
        ] + [
            {"dev_index": c.dev_index, "bdf": c.bdf, "board": None,
             "card": c.card, "state": "UNREADABLE", "who": None, "pid": None,
             "reason": None, "pids_holding": [], "overstayed": False,
             "owner_dead": False, "holder_containers": {},
             "name": None, "board_name": None, "position": None}
            for c in gk.unreadable
        ],
        "queue": _queue_view(gk, states),
        "fence_violations": violations,
        **_names_payload(gk),
    }
    # `states` is already in display order and the unreadable chips follow it.
    # Without a names file that order is device index, so merge them as before.
    if not gk.names.file.boards or gk.names_error is not None:
        payload["chips"].sort(key=lambda c: c["dev_index"])
    setting = gk.grain_setting()
    lines = [f"grain: {gk.grain}" + (f" (set)" if setting != "auto" else "")
             + f"   ({len(gk.boards)} boards, "
             f"{len(states) + len(gk.unreadable)} chips)"]
    current_board = None
    for s in states:
        if s.chip.serial != current_board:
            current_board = s.chip.serial
            bname = gk.names.board_name(current_board)
            split = ("  — dies reset separately: a mesh over both needs a "
                     "whole-board reset" if gk.is_split(current_board) else "")
            lines.append(f"board {bname + '  ' if bname else ''}"
                         f"{current_board}  ({s.chip.card}){split}")
        who = (s.lease or {}).get("who", "")
        pid = (s.lease or {}).get("pid", "")
        extra = f"  {who} pid {pid}" if who else ""
        if s.state == "BUSY-UNTRACKED":
            extra = f"  pid {','.join(map(str, s.pids))} — no lease; try: gozer adopt"
        elif s.state == "DEGRADED":
            have, want = gk.short_board_of(s.chip)
            extra = (f"  board has {have} of {want} chips readable -- "
                     f"{DEGRADED_HINT}")
        elif s.state == "STALE":
            # status leaves a STALE lease on disk (see above) -- say what
            # clears it, since this is no longer done silently.
            extra += "  — stale; clear it with: gozer reconcile"
            if s.owner_dead:
                extra += (f"  — owner pid {s.lease.get('pid')} is dead; "
                          f"{OTHER_USERS_HINT}")
        if s.state == "HELD-FOREIGN" and s.owner_dead:
            who_holds = ", ".join(
                f"pid {p}" + (f" (container {cid})"
                              if (cid := procfd.container_id(p, gk.proc_root))
                              else "")
                for p in s.pids)
            extra += (f"  — owner pid {s.lease.get('pid')} is dead; "
                      f"device still held by {who_holds}")
        if s.overstayed:
            extra += "  OVERSTAYED"
        svc = (s.lease or {}).get("service")
        if svc:
            extra += (f"  serving {svc.get('name')}"
                      + (f" on :{svc['port']}" if svc.get("port") else ""))
        for v in violations:
            if v["dev_index"] == s.chip.dev_index:
                extra += (f"  — pid {v['pid']} of lease {v['lease_id']} "
                          f"({v['who']}) is outside its grant")
        cname = gk.names.chip_name(s.chip)
        label = f"{cname:<8}" if cname else ""
        lines.append(f"  chip {s.chip.dev_index}  {s.chip.bdf}  "
                     f"{label}{s.state:<15}{extra}")
    if gk.unreadable:
        lines.append("unreadable (not grantable):")
        for c in gk.unreadable:
            lines.append(f"  chip {c.dev_index}  {c.bdf}  UNREADABLE")
        lines.append(f"  {UNREADABLE_HINT}")
    for e in gk.names.missing:
        lines.append(f"named but not on this machine: {e.name} "
                     f"(serial {e.serial})")
    q = payload["queue"]
    if q:
        lines.append("queue:")
        for i, e in enumerate(q, 1):
            lines.append(f"  {_queue_line(gk, i, e)}  since {e['since']}")
    _emit(payload, "\n".join(lines), args.json)
    return EXIT_OK


def cmd_topology(args) -> int:
    gk, _ = _make(args)
    _warn_names(gk)
    payload = {
        "grain": gk.grain,
        "boards": [
            {"serial": b.serial, "card": b.card,
             "name": gk.names.board_name(b.serial),
             "chips": [{"dev_index": c.dev_index, "bdf": c.bdf,
                        "asic_id": c.asic_id, **_name_fields(gk, c)}
                       for c in b.chips]}
            for b in gk.boards
        ],
        "unreadable": [{"dev_index": c.dev_index, "bdf": c.bdf}
                       for c in gk.unreadable],
        **_names_payload(gk),
    }
    lines = [f"grain: {gk.grain}"]
    for b in gk.boards:
        bname = gk.names.board_name(b.serial)
        lines.append(f"board {bname + '  ' if bname else ''}{b.serial}  "
                     f"({b.card})")
        for c in b.chips:
            cname = gk.names.chip_name(c)
            label = f"{cname:<8}" if cname else ""
            lines.append(f"  chip {c.dev_index}  {c.bdf}  {label}"
                         f"asic {c.asic_id}")
    for e in gk.names.missing:
        lines.append(f"named but not on this machine: {e.name} "
                     f"(serial {e.serial})")
    _emit(payload, "\n".join(lines), args.json)
    return EXIT_OK


def _die_notes(gk, units: list[str]) -> list[str]:
    """What a chip-grain grant on a multi-die board needs the tenant to know."""
    notes = []
    if gk.grain != "chip":
        return notes
    boards: dict[str, list[str]] = {}
    for u in units:
        board = gk.board_of(u)
        if board is not None and len(board.chips) > 1:
            boards.setdefault(board.serial, []).append(u)
    for serial, mine in boards.items():
        board = next(b for b in gk.boards if b.serial == serial)
        if len(mine) < len(board.chips):
            notes.append(
                f"one die of {board.card} board {serial}: open it with "
                "TT_METAL_VISIBLE_DEVICES=0 and TT_MESH_GRAPH_DESC_PATH set to "
                "ttnn's p150_mesh_graph_descriptor.textproto (a v6 bundle's "
                "run.sh does both)")
        elif gk.is_split(serial):
            notes.append(
                f"board {serial} had its dies reset separately; a mesh over "
                "both dies needs a whole-board reset first: gozer reset <lease>")
    return notes


def cmd_grain(args) -> int:
    gk, _ = _make(args)
    if args.value is not None:
        why = gk.set_grain(args.value)
        if why:
            _emit({"ok": False, "reason": why}, f"gozer: not changed: {why}",
                  args.json)
            return EXIT_UNAVAILABLE
    from gozer.topology import lease_grain
    payload = {"grain": gk.grain, "setting": gk.grain_setting(),
               "derived": lease_grain(gk.boards)}
    human = (f"grain: {payload['grain']}   setting: {payload['setting']}   "
             f"derived from the boards: {payload['derived']}")
    _emit(payload, human, args.json)
    return EXIT_OK


def _render_grant(grant: Grant, gk, owner_pid: int | None = None) -> str:
    idx = ",".join(str(i) for i in grant.dev_indices)
    lines = [f"granted: chips {idx}  (units {', '.join(grant.units)})"]
    if grant.expanded:
        lines.append(f"  note: asked for {grant.requested} — leases here are "
                     "whole boards, so you get every chip on it")
    for note in _die_notes(gk, grant.units):
        lines.append(f"  note: {note}")
    if grant.unreset:
        lines.append(f"  note: {', '.join(grant.unreset)} came back from a crashed lease "
                     f"without a reset; for clean silicon run: gozer reset {grant.lease_id}")
    lines.append(f"  export TT_VISIBLE_DEVICES={','.join(grant.bdfs)}")
    lines.append(f"  lease {grant.lease_id}   release with: "
                 f"gozer release {grant.lease_id}")
    if owner_pid is not None:
        lines.append(f"  owner pid {owner_pid}  (not detached -- held for as "
                     "long as that pid lives, no grace window)")
    for bdf, who in grant.neighbours.items():
        lines.append(f"  ! live eth neighbour {bdf} held by {who} — "
                     "your reset on release may perturb it")
    return "\n".join(lines)


def cmd_acquire(args) -> int:
    gk, km = _make(args)
    # A name (`left`, `right.1`) becomes the serial or BDF the rest of gozer
    # uses, here, before anything can be leased or queued. A name that does not
    # resolve raises NamesError and nothing has been touched. The resolved value
    # is what a queue ticket stores, so a replay by `wait` does not depend on
    # the names file.
    args.exact = gk.resolve_target(args.exact)
    refused = _refuse_unreadable_exact(gk, args.exact, args.json)
    if refused is not None:
        return refused
    service, bad = _service_from_args(args)
    bad = bad or _service_clash(gk, service)
    if bad:
        _emit({"granted": False, "error": bad}, f"gozer: {bad}", args.json)
        return EXIT_UNAVAILABLE
    # --owner-pid lets a *supervisor* (tt-station-agentd, holding a lease on
    # behalf of a container it manages) claim non-detached semantics: the
    # lease is judged by pid_alive(owner_pid) instead of the detached grace
    # window, which matters because a root-owned process inside a container
    # holds fds gozer -- running unprivileged -- can never see. Validate
    # before it ever reaches Keymaster.acquire: a pid that is already dead
    # would produce a lease that is reapable the instant it is made, which is
    # worse than refusing outright, and a non-positive pid is never a real
    # process id. Checked against the *configured* proc root (gk.proc_root),
    # never the real /proc, so this validation is hermetic under tests.
    if args.owner_pid is not None:
        if args.owner_pid <= 0:
            msg = f"--owner-pid must be a positive pid, got {args.owner_pid}"
            _emit({"granted": False, "error": msg}, f"gozer: {msg}", args.json)
            return EXIT_UNAVAILABLE
        if not procfd.pid_alive(args.owner_pid, gk.proc_root):
            msg = (f"--owner-pid {args.owner_pid} is not alive -- acquiring a "
                  "lease for a dead process would be reapable the instant "
                  "it was made; refusing instead of handing back a lease "
                  "that would evaporate")
            _emit({"granted": False, "error": msg}, f"gozer: {msg}", args.json)
            return EXIT_UNAVAILABLE

    # No `pid` is passed for the default (--owner-pid omitted) path: a bare
    # `gozer acquire` has no process for gozer to supervise (it prints its
    # export line and exits immediately), so Keymaster.acquire records this
    # as a *detached* lease. See its docstring and Gatekeeper.reconcile's
    # DETACHED_GRACE_SECONDS handling -- a fixed sentinel pid (e.g.
    # always-alive init) was tried and rejected, since that makes a detached
    # lease immortal instead of just outliving the one-shot CLI process that
    # created it. --owner-pid opts out of all that: it passes a real,
    # validated, live pid through as `pid`, producing a non-detached lease
    # that Keymaster.acquire/Gatekeeper.reconcile judge by ordinary
    # process-death detection instead.
    result = km.acquire(args.chips, who=args.who, reason=args.reason,
                        exact=args.exact, fresh=args.fresh, expect=args.expect,
                        pid=args.owner_pid, owner_pid=args.owner_pid,
                        ticket=args.ticket, no_queue=args.no_queue,
                        service=service)
    if isinstance(result, Grant):
        payload = {
            "granted": True, "lease_id": result.lease_id, "units": result.units,
            "chips": result.bdfs, "dev_indices": result.dev_indices,
            "expanded": result.expanded, "requested": result.requested,
            "neighbours": result.neighbours,
            "unreset": result.unreset,
            "owner_pid": args.owner_pid,
            "env": km.env_for(result),
        }
        _emit(payload, _render_grant(result, gk, owner_pid=args.owner_pid),
              args.json)
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
    last_touch = None
    while time.monotonic() < deadline:
        entry = next((e for e in gk.queue_entries()
                      if e["ticket"] == args.ticket), None)
        if entry is None:
            _emit({"granted": False, "ticket": args.ticket, "reason": "gone"},
                  f"ticket {args.ticket} is no longer queued", args.json)
            return EXIT_NO_LEASE
        # A waiting ticket is a live one: refresh `seen_at` (at most every
        # 30 s, not every poll) so it neither ages out behind a long lease
        # nor loses the units it holds back for itself.
        if last_touch is None or time.monotonic() - last_touch >= 30:
            gk.touch_ticket(args.ticket)
            last_touch = time.monotonic()
        # Always try: an open claim window no longer blocks other waiters
        # (acquire reserves the window holder's units and lets everyone else
        # take the rest), so polling only when may_claim() is true would make
        # a waiter sit out someone else's 90-second window for nothing.
        if True:
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
            # --owner-pid is replayed too: the supervisor was named when the
            # ticket was taken, and it is that pid (not this waiter, which
            # exits) the lease must live and die with. It is checked again
            # now, since it may have died while queued. A dead pid is never
            # used: the lease would be reapable the instant it was created.
            # The grant falls back to a detached lease and says so.
            owner = entry.get("owner_pid")
            note = None
            if owner is not None and not procfd.pid_alive(owner, gk.proc_root):
                note = (f"note: --owner-pid {owner} was not alive at grant "
                        f"time, so this lease is detached (reaped after "
                        f"{DETACHED_GRACE_SECONDS} s without an open device)")
                owner = None
            result = km.acquire(
                entry.get("chips_spec")
                or f"{entry['min_chips']}-{entry['max_chips']}",
                who=entry["who"], reason=entry.get("reason"),
                exact=entry.get("exact"), fresh=entry.get("fresh", False),
                expect=entry.get("expect"), ticket=args.ticket, pid=owner,
                service=entry.get("service"))
            if isinstance(result, Grant):
                payload = {"granted": True, "lease_id": result.lease_id,
                           "chips": result.bdfs, "env": km.env_for(result),
                           "owner_pid": owner}
                human = _render_grant(result, gk, owner_pid=owner)
                if note:
                    human += "\n" + note
                _emit(payload, human, args.json)
                return EXIT_OK
        time.sleep(1)

    pos = gk.queue_position(args.ticket)
    _emit({"granted": False, "ticket": args.ticket, "position": pos},
          f"still queued at position {pos}; run "
          f"`gozer wait {args.ticket}` again", args.json)
    return EXIT_WAIT_TIMEOUT


def cmd_queue(args) -> int:
    gk, _ = _make(args)
    entries = _queue_view(gk, gk.reconcile(reap=False))
    human = "\n".join(
        f"{_queue_line(gk, i, e)}  ticket {e['ticket']}"
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
    """Renew a lease: a sign of life, and optionally a new advisory deadline.

    Every renew stamps `renewed_at`, which restarts a detached lease's grace
    window (see Gatekeeper.reconcile), so a holder whose work will not open the
    device for a while can keep its chips by renewing more often than every
    DETACHED_GRACE_SECONDS. `--expect` also moves the advisory deadline.
    """
    gk, _ = _make(args)
    lease = gk.read_lease(args.lease)
    if lease is None:
        _emit({"renewed": False}, f"lease {args.lease} not found", args.json)
        return EXIT_NO_LEASE
    from gozer.gatekeeper import utcnow
    from gozer.keymaster import _deadline
    service, bad = _service_from_args(args, current=lease.get("service"))
    bad = bad or _service_clash(gk, service, lease_id=args.lease)
    if bad:
        _emit({"renewed": False, "error": bad}, f"gozer: {bad}", args.json)
        return EXIT_UNAVAILABLE
    lease["renewed_at"] = utcnow()
    if args.expect:
        lease["expect_done"] = _deadline(args.expect)
    if service:
        lease["service"] = service
    gk.write_lease(lease)
    for unit in lease.get("units", []):
        gk.update_unit_lease(unit, lease)
    _emit({"renewed": True, "renewed_at": lease["renewed_at"],
           "expect_done": lease.get("expect_done")},
          f"lease {args.lease} renewed at {lease['renewed_at']}"
          + (f", now expected done {lease['expect_done']}" if args.expect else ""),
          args.json)
    return EXIT_OK


def cmd_release(args) -> int:
    _, km = _make(args)
    r = km.release_result(args.lease, no_reset=args.no_reset, force=args.force)
    payload = {"released": r.ok, "status": r.status, "message": r.message}
    if r.status == "reset-failed":
        payload["units"] = r.units
        payload["recovery"] = r.recovery
    if r.reaped_at:
        payload["reaped_at"] = r.reaped_at
        payload["reaped_why"] = r.reaped_why
    _emit(payload, r.message, args.json)
    # The exit code comes from the structured status, never from the wording of
    # the message: it can carry embedded tt-smi output.
    if r.status == "released":
        return EXIT_OK
    if r.status == "reset-failed":
        return EXIT_RELEASE_RESET_FAILED
    if r.status == "not-found":
        return EXIT_NO_LEASE
    return EXIT_RELEASE_REFUSED     # "refused" and "changed-hands": nothing torn down


def cmd_reset(args) -> int:
    _, km = _make(args)
    result = km.reset(args.lease, force=args.force)
    _emit({"reset": result.ok, "status": result.status, "message": result.message},
          result.message, args.json)
    if result.ok:
        return EXIT_OK
    if result.status == "not-found":
        return EXIT_NO_LEASE
    if result.status == "failed":
        return EXIT_RESET_FAILED
    if result.status == "changed-hands":
        return EXIT_RESET_CHANGED_HANDS
    return EXIT_RELEASE_REFUSED     # "refused": nothing was done


def cmd_reset_all(args) -> int:
    _, km = _make(args)
    r = km.reset_all(args.who, args.reason)
    _emit({"reset": r.ok, "status": r.status, "message": r.message,
           "unreadable": r.unreadable, "lease_id": r.lease_id},
          r.message, args.json)
    return {"reset": EXIT_OK, "busy": EXIT_UNAVAILABLE,
            "failed": EXIT_RESET_FAILED,
            "reset-incomplete": EXIT_RESET_ALL_INCOMPLETE}[r.status]


def cmd_reconcile(args) -> int:
    gk, _ = _make(args)
    if args.sudo:
        gk.use_sudo = True
    states = gk.reconcile(reap=True)
    reaped = getattr(gk, "last_reaped", [])
    violations = gk.fence_violations()
    payload = {"chips": [{"dev_index": s.chip.dev_index, "state": s.state}
                         for s in states],
               "sudo": bool(args.sudo), "reaped": reaped,
               "fence_violations": violations}
    human = "\n".join(f"chip {s.chip.dev_index}  {s.state}" for s in states)
    # Log each breach once, not on every run of the optional 5-minute timer.
    logged = {(r.get("lease_id"), r.get("pid"), r.get("dev_index"))
              for r in history.read(gk.history_root)[-2000:]
              if r.get("event") == "fence_violation"} if violations else set()
    for v in violations:
        human += (f"\noutside its grant: pid {v['pid']} of lease {v['lease_id']} "
                  f"({v['who']}) holds device {v['dev_index']} ({v['bdf']})")
        if (v["lease_id"], v["pid"], v["dev_index"]) in logged:
            continue
        history.log(gk.history_root, "fence_violation", lease_id=v["lease_id"],
                    who=v["who"], pid=v["pid"], dev_index=v["dev_index"],
                    bdf=v["bdf"], held_by=v["held_by"], seen_by="reconcile")
    for r in reaped:
        human += f"\nreaped lease {r['lease_id']} ({r['who']}): {r['why']}"
        if r["owner_dead"] and not args.sudo:
            human += f"\n  owner pid {r['owner_pid']} is dead; {OTHER_USERS_HINT}"
    if args.sudo and not procfd.sudo_available():
        human += ("\n! --sudo requested but passwordless sudo is unavailable; "
                  "results cover your own processes only")
    _emit(payload, human, args.json)
    return EXIT_OK


def cmd_adopt(args) -> int:
    gk, km = _make(args)
    from gozer import procfd
    from gozer.gatekeeper import utcnow
    asked = args.target
    args.target = gk.resolve_target(args.target)
    fd_map = procfd.holders(gk.proc_root)
    target = gk._resolve_exact(args.target)
    if target is None:
        _emit({"adopted": False}, f"unknown chip or board: {asked}", args.json)
        return EXIT_NO_LEASE

    chips = gk.chips_in_unit(target)
    pids = sorted({p for c in chips for p in fd_map.get(c.dev_index, [])})
    if not pids:
        _emit({"adopted": False}, f"{asked} has no process holding it",
              args.json)
        return EXIT_UNAVAILABLE
    service, bad = _service_from_args(args)
    bad = bad or _service_clash(gk, service)
    if bad:
        _emit({"adopted": False, "error": bad}, f"gozer: {bad}", args.json)
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
    if service:
        lease["service"] = service
    if not gk.claim_unit(target, lease):
        _emit({"adopted": False}, f"{asked} is already leased", args.json)
        return EXIT_UNAVAILABLE
    gk.write_lease(lease)
    history.log(gk.history_root, "adopted", lease_id=lease_id, who=args.who,
               chips=lease["chips"], pids=pids)
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


def _format_history_line(rec: dict) -> str:
    """One line per event: timestamp, event name, then every other field as
    key=value. Generic on purpose -- events carry different fields (see
    gozer/history.py's module docstring for the full list per event type),
    and a formatter that has to be taught about each one is exactly the kind
    of thing that drifts out of sync with what's actually logged."""
    ts = rec.get("ts", "?")
    event = rec.get("event", "?")
    extra = "  ".join(f"{k}={rec[k]}" for k in rec if k not in ("ts", "event"))
    # Pad to 9 and always add a space: identical columns for the short event
    # names, and a long one (fence_violation, release_record_mismatch) no
    # longer runs into its first field.
    return f"{ts}  {event:<9} {extra}"


def cmd_history(args) -> int:
    gk, _ = _make(args)
    records = history.read(gk.history_root)
    n = args.n if args.n and args.n > 0 else 20
    tail = records[-n:]
    human = "\n".join(_format_history_line(r) for r in tail) or "(no history)"
    _emit({"history": tail}, human, args.json)
    return EXIT_OK


def cmd_report(args) -> int:
    gk, _ = _make(args)
    records = report.merge_events(report.load_archive(), history.read(gk.history_root))
    if not records:
        print("gozer report: no history yet -- nothing to report on", file=sys.stderr)
        return EXIT_UNAVAILABLE

    stats = report.compute_stats(records)
    if args.json:
        print(jsonlib.dumps(stats))
        return EXIT_OK

    html = report.render(records, source_note=gk.history_root)
    if args.out:
        out_path = args.out
    else:
        root = _repo_root()
        if root is None:
            print("gozer report: not inside a git checkout; pass --out PATH",
                  file=sys.stderr)
            return EXIT_UNAVAILABLE
        out_path = os.path.join(root, "docs", "log.html")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html)
    print(f"gozer report: wrote {out_path}")
    return EXIT_OK

# ---- names ----------------------------------------------------------------

def _names_path(gk) -> str:
    return gk.names_path or names_mod.default_path()


def _require_names(gk) -> None:
    """Raise the names-file error, for the commands that are about the file."""
    if gk.names_error is not None:
        raise gk.names_error


def cmd_names(args) -> int:
    gk, _ = _make(args)
    _require_names(gk)
    nm = gk.names
    path = _names_path(gk)
    boards = []
    for b in gk.boards:
        name = nm.board_name(b.serial)
        boards.append({
            "name": name, "serial": b.serial, "card": b.card,
            "chips": [{"name": nm.chip_name(c), "position": nm.position_of(c),
                       "dev_index": c.dev_index, "bdf": c.bdf,
                       "asic_id": c.asic_id} for c in b.chips]})
    payload = {
        "file": path, "exists": os.path.lexists(path), "boards": boards,
        "missing": [{"name": e.name, "serial": e.serial} for e in nm.missing],
        "unmatched_pins": [{"board": p.board, "position": p.position,
                            "asic": p.asic} for p in nm.unmatched_pins],
    }
    lines = [f"names file: {path}"
             + ("" if payload["exists"] else "  (does not exist)")]
    for b in boards:
        head = b["name"] if b["name"] else "(no name)"
        lines.append(f"board {head}  serial {b['serial']}  ({b['card']})")
        for c in b["chips"]:
            label = c["name"] or f"chip {c['dev_index']}"
            lines.append(f"  {label:<10} index {c['dev_index']}  {c['bdf']}  "
                         f"asic {c['asic_id']}")
        if not b["name"]:
            lines.append(f"  name it with: gozer names set <name> {b['serial']}")
    for e in nm.missing:
        lines.append(f"missing: {e.name} (serial {e.serial}) is named in the "
                     "file but is not on this machine")
    for p in nm.unmatched_pins:
        lines.append(f"unmatched pin: {p.board}.{p.position} asic {p.asic} is "
                     "not a chip of that board")
    _emit(payload, "\n".join(lines), args.json)
    return EXIT_OK


def cmd_names_udev(args) -> int:
    gk, _ = _make(args)
    _require_names(gk)
    text = names_mod.udev_rules(gk.names)
    _emit({"rules": text, "install": names_mod.UDEV_RULES_PATH}, text.rstrip("\n"),
          args.json)
    return EXIT_OK


def _find_board(gk, token: str):
    """The board a name, serial, BDF, device index or ASIC id belongs to."""
    for b in gk.boards:
        if gk.names.board_name(b.serial) == token or token.casefold() == \
                b.serial.casefold():
            return b
        for c in b.chips:
            if token in (c.bdf, str(c.dev_index)) or (
                    c.asic_id and token.casefold() == c.asic_id.casefold()):
                return b
    raise names_mod.NamesError(
        "unknown-board",
        f"{token!r} is not a board name, serial, PCI address or device index "
        "on this machine (see `gozer names`)")


def _board_is_leased(gk, board) -> bool:
    held = gk.held_units()
    return any(gk.unit_key_for(c) in held for c in board.chips)


def cmd_names_set(args) -> int:
    gk, _ = _make(args)
    _require_names(gk)
    path = _names_path(gk)
    board = _find_board(gk, args.board)
    names_mod.check_name(args.name)
    if args.name in names_mod.literal_tokens(gk.boards, gk.unreadable):
        raise names_mod.NamesError(
            "ambiguous",
            f"{args.name!r} is the serial or ASIC id of a chip on this "
            "machine, so it could not be told from the name; pick another")
    # A board under a lease keeps the name its holder was told. Re-pointing an
    # existing name takes it off the board that has it now, so that board counts.
    affected = [board]
    old = next((e for e in gk.names.file.boards if e.name == args.name), None)
    if old is not None:
        prev = gk.names.by_name.get(old.name)
        if prev is not None:
            affected.append(prev.board)
    # The lease check and the write happen under the gate mutex, so an acquire
    # cannot take the board between them.
    with gk.critical_section():
        for b in affected:
            if _board_is_leased(gk, b):
                raise names_mod.NamesError(
                    "board-leased",
                    f"board {b.serial} is under a lease; release it before "
                    "changing its name")
        new = names_mod.set_name(path, args.name, board.serial)
        names_mod.write_text(path, new)
    _emit({"set": True, "name": args.name, "serial": board.serial, "file": path},
         f"named board {board.serial} {args.name!r} in {path}", args.json)
    return EXIT_OK


def cmd_names_unset(args) -> int:
    gk, _ = _make(args)
    _require_names(gk)
    path = _names_path(gk)
    new = names_mod.unset_name(path, args.name)
    names_mod.write_text(path, new)
    _emit({"unset": True, "name": args.name, "file": path},
         f"removed the name {args.name!r} from {path}", args.json)
    return EXIT_OK


def _repo_root() -> str | None:
    """The git work tree containing the current directory, or None."""
    import subprocess
    try:
        return subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True,
            text=True, check=True).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def cmd_run(args) -> int:
    gk, km = _make(args)
    args.exact = gk.resolve_target(args.exact)
    refused = _refuse_unreadable_exact(gk, args.exact, False)
    if refused is not None:
        return refused
    if not args.command:
        print("nothing to run: put the command after --", file=sys.stderr)
        return EXIT_UNAVAILABLE
    service, bad = _service_from_args(args)
    bad = bad or _service_clash(gk, service)
    if bad:
        print(f"gozer: {bad}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        return km.run(args.command, chips_spec=args.chips, who=args.who,
                      reason=args.reason, exact=args.exact, fresh=args.fresh,
                      expect=args.expect, service=service)
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
    gp = add("grain", cmd_grain,
             "show the lease unit; `gozer grain chip|board|auto` sets it for the box")
    gp.add_argument("value", nargs="?", choices=["auto", "board", "chip"],
                   help="set the grain (refused while anything is leased or queued)")

    nm = add("names", cmd_names,
             "give boards names that survive reboots; list them")
    nsub = nm.add_subparsers(dest="names_cmd")
    nu = nsub.add_parser("udev", help="print udev rules for "
                                      "/dev/tenstorrent/by-name (writes nothing)")
    nu.add_argument("--json", action="store_true")
    nu.set_defaults(func=cmd_names_udev)
    ns = nsub.add_parser("set", help="name a board")
    ns.add_argument("name")
    ns.add_argument("board", help="a serial, PCI address, device index, or "
                                  "an existing name")
    ns.add_argument("--json", action="store_true")
    ns.set_defaults(func=cmd_names_set)
    nx = nsub.add_parser("unset", help="remove a board's name")
    nx.add_argument("name")
    nx.add_argument("--json", action="store_true")
    nx.set_defaults(func=cmd_names_unset)

    for name, aliases in (("acquire", ("summon",)),):
        a = add(name, cmd_acquire, "take a lease on chips", aliases=aliases)
        a.add_argument("--chips", default="1", help="N, all, or LO-HI")
        a.add_argument("--who", required=True, help="e.g. claude:ttm-optimize")
        a.add_argument("--reason", default=None)
        a.add_argument("--exact", default=None, help="a name (left, right.1), BDF, device index, or unit")
        a.add_argument("--fresh", action="store_true", help="require reset chips")
        a.add_argument("--expect", default=None, help="advisory duration, e.g. 45m")
        a.add_argument("--ticket", default=None, help="claim against a queue ticket")
        a.add_argument("--no-queue", action="store_true",
                       help="fail instead of queueing")
        _add_service_args(a)
        a.add_argument("--owner-pid", type=int, default=None, dest="owner_pid",
                       help="a supervisor's pid to own this lease; the lease "
                       "is non-detached and judged by that pid's liveness "
                       "instead of the detached grace window (e.g. a "
                       "container-launching daemon whose child fds gozer "
                       "cannot see)")

    w = add("wait", cmd_wait, "block until a ticket is granted")
    w.add_argument("ticket")
    w.add_argument("--timeout", default="8m",
                   help="bounded so it fits inside an agent tool call")

    add("queue", cmd_queue, "list waiting requests")
    c = add("cancel", cmd_cancel, "drop a queue ticket")
    c.add_argument("ticket")

    sv = add("services", cmd_services,
             "what is being served, where, on which chips (for a router)")
    sv.add_argument("--check", action="store_true",
                    help="ask each service for GET /v1/models (1 s timeout)")
    sv.add_argument("--live", action="store_true",
                    help="only leases whose owner is alive (what a router should use)")

    r = add("renew", cmd_renew, "heartbeat a lease (restarts a detached lease's grace window); --expect also extends its advisory deadline")
    r.add_argument("lease")
    r.add_argument("--expect", default=None,
                   help="also move the advisory deadline; without it renew is a "
                        "heartbeat only")
    _add_service_args(r)

    for name, aliases in (("release", ("banish",)),):
        rel = add(name, cmd_release, "give chips back (resets them)", aliases=aliases)
        rel.add_argument("lease")
        rel.add_argument("--no-reset", action="store_true")
        rel.add_argument("--force", action="store_true",
                         help="release even while a device is open")

    rs = add("reset", cmd_reset,
             "reset a held lease's chips in place; the lease stays held")
    rs.add_argument("lease")
    rs.add_argument("--force", action="store_true",
                    help="reset even if a device is still open")

    ra = add("reset-all", cmd_reset_all,
             "host-wide tt-smi -r all, under a lease on the whole box; refuses "
             "unless every chip is free")
    ra.add_argument("--who", required=True)
    ra.add_argument("--reason", default=None)

    rec = add("reconcile", cmd_reconcile, "re-derive state from kernel truth")
    rec.add_argument("--sudo", action="store_true",
                     help="use sudo for cross-user fd visibility")

    ad = add("adopt", cmd_adopt, "wrap a lease around untracked running work")
    ad.add_argument("target", help="a name (left, right.1), BDF, device index, "
                                   "or unit key")
    ad.add_argument("--who", required=True)
    ad.add_argument("--reason", default=None)
    _add_service_args(ad)

    e = add("env", cmd_env, "print export lines for a lease")
    e.add_argument("lease")

    h = add("history", cmd_history, "past leases: who held what, when, "
                                    "for how long")
    h.add_argument("-n", type=int, default=20,
                   help="how many recent events to show (default 20)")

    rp = add("report", cmd_report, "regenerate the containment-log page "
                                   "(docs/log.html) from history.jsonl")
    rp.add_argument("--out", default=None,
                    help="output path (default: docs/log.html at the repo root)")

    rn = add("run", cmd_run, "acquire, run a command, always release")
    rn.add_argument("--chips", default="1")
    rn.add_argument("--who", default="shell:gozer-run")
    rn.add_argument("--reason", default=None)
    rn.add_argument("--exact", default=None,
                    help="a name (left, right.1), BDF, device index, or unit")
    rn.add_argument("--fresh", action="store_true")
    rn.add_argument("--expect", default=None)
    _add_service_args(rn)
    rn.add_argument("command", nargs=argparse.REMAINDER)

    return p


def main(argv: list[str] | None = None) -> int:
    # argv defaults to sys.argv[1:] because the console script that setuptools
    # generates (and so the Debian package) calls main() with no arguments.
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    try:
        return args.func(args)
    except TopologyError as e:
        print(f"gozer: {e}", file=sys.stderr)
        return EXIT_NO_TOPOLOGY
    except names_mod.NamesError as e:
        print(f"gozer: {e.message}", file=sys.stderr)
        return EXIT_NAMES
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
