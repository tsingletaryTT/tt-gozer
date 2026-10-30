# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Wording rules shared by the page build and the users-list check."""

# Phrases the plain-English rules (global CLAUDE.md) keep out of the prose
# written for this page, and out of the `how` text of the users list.
# tests/test_site_build.py sweeps the page's visible prose (terminal blocks
# and data regions excluded) for each of them.
SWEEP = (
    ", not ", "rather than", "instead of", "reads as", "honest",
    "the one that", "which is why", "This is what", "load-bearing",
    "open the door", "carries the story", "on-ramp", "the tell",
)
