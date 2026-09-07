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
__version__ = "0.3.0"
