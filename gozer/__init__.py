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
__version__ = "0.3.3"
