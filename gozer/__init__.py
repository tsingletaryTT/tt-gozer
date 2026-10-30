# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""tt-gozer — cooperative Tenstorrent chip leasing.

The keymaster must meet the gatekeeper for the coming of Gozer.
"""

# 0.2.0 covers everything landed since the initial 0.1.0 implementation:
# read-only `status`, the append-only history log, the optional reconcile
# timer, and `acquire --owner-pid`. Minor rather than patch because 0.1.0
# also shipped the exit 12 -> 15 change, which is a breaking contract change
# for anything matching on "device still open" (see README's exit-code table).
#
# 0.3.0 adds `gozer report`: turns history.jsonl into the GH Pages
# containment-log page, computed rather than hand-typed.
#
# 0.3.1: `gozer report` no longer embeds the literal --reason text of every
# lease into the published page -- it maps each reason to a small public-safe
# theme label instead (see gozer/report.py's theme_for_reason()).
#
# 0.3.2: history.jsonl can live under a persistent GOZER_HISTORY_ROOT instead
# of GOZER_ROOT, so it survives a reboot.
#
# 0.3.3: `gozer report` blends the checked-in `history_archive.jsonl` --
# the 2026-09-03..09-10 window recovered from the last report rendered
# before this box's history log started resetting to empty on every reboot
# -- into every render, via report.load_archive()/merge_events(). The
# published page reads as one continuous ledger again instead of snapping
# back to whatever the live log happens to hold since the last reboot.
#
# 0.3.4: `gozer run`'s release waits up to 10 s for a device to close before
# it refuses (keymaster.RELEASE_SETTLE_SECONDS). A command that exited fast
# could leave a forked worker holding the device for a second or two; the
# release refused, left a stale lease and skipped the reset (tt-orchard issue
# 117). `gozer release` by hand is unchanged.
#
# 0.3.5: a queued `acquire --owner-pid N` keeps N through `gozer wait` (the
# ticket records it; a dead pid falls back to a detached grant with a note).
# `release` returns a structured result: a reset that failed now exits 19
# instead of 0, prints the recovery commands, and `cmd_release` no longer picks
# its exit code by matching "not found" in the message text. Exit 0 -> 19 is a
# contract change for anything that treated a failed-reset release as success
# (both reported by tt-orchard).
#
# 0.3.6: a chip whose tt_serial reads all ones (it has fallen off the bus) is
# `UNREADABLE` in `status`/`topology`, kept out of boards and the lease grain,
# and refused by `--exact`. Before, it passed the empty-serial check, formed a
# board of its own and showed as FREE (tt-orchard, 2026-10-30).
#
# 0.3.7: `gozer reset-all` (a host-wide `tt-smi -r all` under a lease on every
# unit; refuses unless the whole box is free) and the DEGRADED state: a free
# chip on a board missing a chip its card type should have is not grantable.
# Exit 20 = reset-all ran and a chip still reads all ones.
__version__ = "0.3.7"
