# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Tests for the explainer page build (site_build/build.py, scripts/build_site.py).

The page is built once per module into a temporary file, and every test reads
that file: the checks apply to the page a visitor gets.
"""
import html
import json
import os
import re
import subprocess
import sys

import pytest

from site_build.afternoon import captions, contrast, facts
from site_build.style import SWEEP
from site_build.roster import load_users
from site_build.transcripts import run_session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "site", "data", "afternoon-2026-10-26.jsonl")
USERS = os.path.join(ROOT, "site", "data", "users.json")
CSS = os.path.join(ROOT, "site", "style.css")
SCRIPT = os.path.join(ROOT, "scripts", "build_site.py")
LIVE_LOGS = [os.path.expanduser("~/.local/state/tt-gozer/history.jsonl"),
             os.path.expanduser("~/.local/state/tt-gozer/history-backup-2026-10-30.jsonl")]

SECTION_ORDER = ["hero", "session", "afternoon", "how", "install", "example-log", "users"]
PALETTE = ["#092221", "#1B8EB1", "#F1F8F8", "#C7D9D8",
           "#74C5DF", "#6FABA0", "#F6BC42", "#FF9E8A"]


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    from site_build.build import build
    out = tmp_path_factory.mktemp("site") / "index.html"
    build(str(out))
    return out.read_text()


def _records():
    with open(DATA) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _sections(page):
    """{id: inner html} for each <section id=...>, in page order."""
    found = re.findall(r'<section id="([^"]+)"[^>]*>(.*?)</section>', page, re.S)
    return dict(found), [i for i, _ in found]


def _visible_prose(page):
    """Visible text of the prose written for the page.

    Drops the style block, terminal blocks and every region marked as data
    (<!--data--> ... <!--/data-->: captions, timeline, roster table), then
    strips tags and unescapes entities.
    """
    t = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
    t = re.sub(r"<pre.*?</pre>", " ", t, flags=re.S)
    t = re.sub(r"<!--data-->.*?<!--/data-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t))


# ----- the ten tests named by the brief ------------------------------------

def test_sections_appear_in_the_spec_order(page):
    ids = re.findall(r'<section id="([^"]+)"', page)
    assert ids == SECTION_ORDER


def test_install_section_names_apt_and_install_sh_and_not_pip(page):
    sections, _ = _sections(page)
    inst = html.unescape(re.sub(r"<[^>]+>", "", sections["install"]))
    assert "sudo apt install ./tt-gozer_" in inst
    assert "_all.deb" in inst
    assert "./install.sh" in inst
    assert "gozer-install-skills" in inst
    assert "gozer status" in inst
    low = page.lower()
    assert "pip install" not in low and "pypi" not in low


def test_every_session_line_on_the_page_is_real_cli_output(page):
    text = html.unescape(page)
    steps = run_session()
    assert steps
    for s in steps:
        assert s.output in text, s.note_key
        assert "$ " + s.command in html.unescape(re.sub(r"<[^>]+>", "", page)), s.command


def test_afternoon_captions_come_from_facts(page):
    sections, _ = _sections(page)
    aft = html.unescape(sections["afternoon"])
    caps = captions(facts(_records()))
    assert caps
    for c in caps:
        assert c in aft


def test_users_section_is_last_and_lists_users_json(page):
    sections, order = _sections(page)
    assert order[-1] == "users"
    body = sections["users"]
    for u in load_users(USERS):
        assert f'href="{html.escape(u["repo"], quote=True)}"' in body
        assert html.escape(u["name"]) in body
        assert html.escape(u["how"], quote=False) in body


def test_no_task_text(page):
    reasons = set()
    for path in LIVE_LOGS:
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line).get("reason")
                    if r:
                        reasons.add(r)
    if not reasons:
        pytest.skip("no live history log on this machine")
    text = html.unescape(page)
    # Reasons of a single short word ('exp', 'hold', 'coder') also occur as
    # parts of ordinary words, and on their own they say nothing about a task.
    # Every reason of six or more characters must be absent.
    leaked = sorted(r for r in reasons if len(r) >= 6 and r in text)
    assert leaked == []


def test_palette_tokens_present():
    css = open(CSS).read().upper()
    for hexcode in PALETTE:
        assert hexcode in css, hexcode


def test_viewport_meta_and_no_script_needed(page):
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert "<script src=" not in page
    assert "<link " not in page          # CSS is inlined, nothing external
    assert "<style>" in page


def test_build_is_byte_identical_across_runs(tmp_path):
    from site_build.build import build
    a, b = tmp_path / "a.html", tmp_path / "b.html"
    build(str(a))
    build(str(b))
    assert a.read_bytes() == b.read_bytes()


def test_check_flag_exits_1_when_docs_differ(tmp_path):
    from site_build.build import build
    target = tmp_path / "index.html"
    build(str(target))
    same = subprocess.run([sys.executable, SCRIPT, "--check", "--out", str(target)],
                          capture_output=True, text=True)
    assert same.returncode == 0, same.stdout + same.stderr
    target.write_text(target.read_text().replace("<title>", "<title>CHANGED ", 1))
    diff = subprocess.run([sys.executable, SCRIPT, "--check", "--out", str(target)],
                          capture_output=True, text=True)
    assert diff.returncode == 1
    assert "CHANGED" in diff.stdout
    assert diff.stdout.lstrip().startswith("---")


# ----- extra checks -----------------------------------------------------------

def test_page_text_passes_the_style_sweep(page):
    prose = _visible_prose(page)
    assert len(prose) > 1500, "the prose extraction found too little text"
    for phrase in SWEEP:
        assert phrase.lower() not in prose.lower(), phrase


def test_page_makes_no_platform_testing_claim(page):
    # No CI has run a platform matrix yet; the page must not claim one.
    assert "tested on" not in html.unescape(page).lower()


def test_phone_text_sizes():
    """In the phone rules, table cells are at least 14px and terminals at least 13.5px."""
    css = open(CSS).read()
    phone = css[css.index("@media (max-width: 480px)"):]
    sizes = {sel.strip(): float(px) for sel, px in
             re.findall(r"([^{}]+)\{[^}]*?font-size:\s*([\d.]+)px", phone)}
    for sel, px in sizes.items():
        if any(k in sel for k in ("table", "td", "th")):
            assert px >= 14, (sel, px)
        if ".term" in sel:
            assert px >= 13.5, (sel, px)


def _css_vars():
    css = open(CSS).read()
    root = re.search(r":root\s*\{(.*?)\}", css, re.S).group(1)
    return dict(re.findall(r"--([\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})", root))


# Every text colour/background pair the stylesheet uses, by variable name.
TEXT_PAIRS = [
    ("ink", "bg"), ("ink", "panel"), ("ink", "line"), ("ink", "alt"),
    ("muted", "bg"), ("muted", "panel"), ("muted", "alt"),
    ("link", "bg"), ("link", "panel"), ("link", "alt"),
    ("term-fg", "term-bg"), ("term-prompt", "term-bg"), ("term-muted", "term-bg"),
    ("bar-fg", "bar-bg"), ("bar-link", "bar-bg"),
    ("ink", "teal"), ("ink", "green"), ("ink", "yellow"), ("ink", "red"),
]


def test_contrast_of_page_colours():
    v = _css_vars()
    for fg, bg in TEXT_PAIRS:
        ratio = contrast(v[bg], v[fg])
        assert ratio >= 4.5, (fg, v[fg], bg, v[bg], round(ratio, 2))


def test_contrast_pairs_cover_every_colour_property():
    """Each `color:` in the CSS uses a variable that appears as a checked foreground."""
    css = open(CSS).read()
    used = set(re.findall(r"(?<![-\w])color\s*:\s*var\(--([\w-]+)\)", css))
    checked = {fg for fg, _ in TEXT_PAIRS}
    assert used and used <= checked, used - checked
    assert not re.search(r"(?<![-\w])color\s*:\s*#", css), "colour literal outside :root"


def test_no_horizontal_overflow_rules():
    css = re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", open(CSS).read(), flags=re.S))
    # the rule whose whole selector is `.term` (not `.card .term` and the like)
    term = re.search(r"(?:^|\})\.term\{([^}]*)\}", css).group(1)
    assert "overflow-x:auto" in term
    for sel in (".figure", ".table-wrap"):
        body = re.search(r"(?:^|\})" + re.escape(sel) + r"\{([^}]*)\}", css).group(1)
        assert "max-width:100%" in body, sel
        assert "overflow-x:auto" in body, sel


def test_every_section_has_one_h2(page):
    sections, order = _sections(page)
    for sid in order:
        assert len(re.findall(r"<h2[\s>]", sections[sid])) == 1, sid
    assert page.count("<h1") == 1


def test_svg_diagram_is_labelled_and_large_enough(page):
    sections, _ = _sections(page)
    svgs = re.findall(r"<svg.*?</svg>", sections["how"], re.S)
    assert len(svgs) == 1
    svg = svgs[0]
    assert 'role="img"' in svg and "<title" in svg and "aria-labelledby" in svg
    sizes = [float(x) for x in re.findall(r'font-size="([\d.]+)"', svg)]
    assert sizes and min(sizes) >= 14
    assert "http" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")


def test_grace_window_on_the_page_comes_from_the_constant(monkeypatch):
    """The prose's grace figure is formatted from DETACHED_GRACE_SECONDS."""
    from gozer import gatekeeper
    from site_build.build import render
    monkeypatch.setattr(gatekeeper, "DETACHED_GRACE_SECONDS", 600)
    text = html.unescape(render())
    assert "600 seconds (10 minutes)" in text
    assert "900" not in _visible_prose(text)


def test_page_has_the_lease_table_inside_the_afternoon_section(page):
    secs, _ = _sections(page)
    sec = secs["afternoon"]
    assert '<details class="lease-table">' in sec
    assert sec.index('class="timeline"') < sec.index('<details class="lease-table">')
    granted = [r for r in _records() if r["event"] == "granted"]
    assert sec.count("<tr>") - 1 == len(granted)
    assert "<details class=\"lease-table\" open" not in sec     # collapsed by default


def test_lease_table_prose_passes_the_style_sweep():
    from site_build.afternoon import lease_table_html
    from site_build.build import WINDOW_END, WINDOW_START
    frag = lease_table_html(_records(), WINDOW_START, WINDOW_END)
    text = " ".join(re.findall(r"<(?:summary|caption)>(.*?)</(?:summary|caption)>", frag))
    assert text
    for phrase in SWEEP:
        assert phrase.lower() not in text.lower(), phrase
