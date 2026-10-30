# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Pins the shape of debian/, the tree that builds the tt-gozer .deb."""
import argparse
import os
import re

from gozer import __version__
from gozer.cli import build_parser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEB = os.path.join(REPO, "debian")


def _read(*parts):
    with open(os.path.join(DEB, *parts), encoding="utf-8") as f:
        return f.read()


def _control_fields():
    """Every field of debian/control (both stanzas merged; none repeat)."""
    fields = {}
    for line in re.sub(r"\n[ \t]+", " ", _read("control")).splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def test_changelog_head_matches_package_version():
    first = _read("changelog").splitlines()[0]
    m = re.match(r"tt-gozer \(([^)]+)\) \S+; urgency=\S+$", first)
    assert m, first
    assert m.group(1) == __version__


def test_changelog_has_a_fixed_signoff_date():
    # The sign-off line is what SOURCE_DATE_EPOCH comes from.
    assert re.search(r"^ -- .+ <.+>  \w{3}, \d{1,2} \w{3} \d{4} \d\d:\d\d:\d\d [+-]\d{4}$",
                     _read("changelog"), re.M)


def test_control_fields():
    f = _control_fields()
    assert f["Architecture"] == "all"
    assert f["Rules-Requires-Root"] == "no"
    for dep in ("debhelper-compat (= 13)", "dh-python", "pybuild-plugin-pyproject"):
        assert dep in f["Build-Depends"]
    assert "${python3:Depends}" in f["Depends"]
    # tt-smi is only suggested. A QB2 already has its own tt-smi in a venv, and
    # a Recommends makes apt install the older apt tt-smi and about thirty
    # Python packages next to it.
    assert "tt-smi" in f["Suggests"]
    assert "tt-smi" not in f.get("Recommends", "")
    assert f["Standards-Version"]
    assert f["Homepage"]
    assert "tsingletary@tenstorrent.com" in f["Maintainer"]


def test_source_format_is_native():
    assert _read("source", "format").strip() == "3.0 (native)"


def test_copyright_is_dep5_apache2():
    text = _read("copyright")
    assert text.splitlines()[0] == \
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
    assert "Apache-2.0" in text


def test_no_maintainer_scripts():
    found = []
    for _, _, files in os.walk(DEB):
        for name in files:
            if re.fullmatch(r"(.+\.)?(preinst|postinst|prerm|postrm)", name):
                found.append(name)
    assert found == []


def test_rules_stop_debhelper_enabling_the_units():
    # Without this override dh_installsystemduser finds the user units and
    # generates a postinst that enables them (seen in a real build).
    rules = _read("rules")
    assert re.search(r"^override_dh_installsystemduser:\n\t@:$", rules, re.M)
    assert "dh_installsystemd " not in rules


def test_man_page_documents_every_command():
    man = _read("gozer.1").replace("\\-", "-")  # roff writes a hyphen as \\-
    parser = build_parser()
    names = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            names = list(action.choices)
    assert names, "no subcommands found"
    missing = [n for n in names if not re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", man)]
    assert missing == []
    for var in ("GOZER_ROOT", "GOZER_HISTORY_ROOT", "GOZER_SYSFS_ROOT",
                "GOZER_PROC_ROOT", "GOZER_RESET_CMD"):
        assert var in man


def _norm(text):
    """Make README markdown and roff text comparable: drop backticks and roff
    escapes, treat the em dash forms alike, ignore the arrow, collapse space."""
    text = text.replace("\\(em", "\u2014").replace("\\-", "-").replace("\\ ", " ")
    text = text.replace("`", "").replace("->", "").replace("\u2192", "")
    text = text.replace("\u2014", "-")
    return re.sub(r"\s+", " ", text).strip()


def test_man_page_exit_codes_match_the_readme_table():
    """Same codes and the same meaning for each, not just the same codes."""
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    table = {c: _norm(t) for c, t in
             re.findall(r"^\| `(\d+)` \| (.*) \|$", readme, re.M)}
    assert table
    man = _read("gozer.1")
    entries = {c: _norm(t) for c, t in
               re.findall(r"^\.B (\d+)\n(.*)$", man, re.M)}
    assert set(entries) == set(table)
    for code, meaning in table.items():
        assert entries[code] == meaning, code


def test_install_file_places_units_under_usr_lib_systemd_user():
    inst = _read("tt-gozer.install")
    assert "contrib/gozer-reconcile.service usr/lib/systemd/user" in inst
    assert "contrib/gozer-reconcile.timer usr/lib/systemd/user" in inst
    assert "skills/* usr/share/tt-gozer/skills" in inst
    assert "bin/gozer-install-skills usr/bin" in inst
    assert "README.md usr/share/doc/tt-gozer" in inst


def test_autopkgtest_is_declared():
    assert "Tests: smoke" in _read("tests", "control")
    assert os.access(os.path.join(DEB, "tests", "smoke"), os.X_OK)


def test_man_page_th_line_matches_package_version():
    th = _read("gozer.1").splitlines()[0]
    assert th.startswith(".TH ")
    assert __version__ in th.replace("\\-", "-")


def test_source_stanza_pins_python_floor_to_pyproject():
    # Read the line with a regex rather than tomllib: tomllib arrived in
    # Python 3.11 and this suite also runs on 3.10, the package's minimum.
    with open(os.path.join(REPO, "pyproject.toml")) as f:
        found = re.search(r'^requires-python\s*=\s*">=\s*([0-9.]+)"\s*$',
                          f.read(), re.M)
    assert found, "pyproject.toml has no requires-python = \">=N\" line"
    minimum = found.group(1)
    source = _read("control").split("\n\n")[0]
    m = re.search(r"^X-Python3-Version:\s*>=\s*([0-9.]+)\s*$", source, re.M)
    assert m, "no X-Python3-Version in the source stanza"
    assert m.group(1) == minimum


def test_man_page_states_the_grace_window_from_the_code():
    from gozer.gatekeeper import DETACHED_GRACE_SECONDS
    assert str(DETACHED_GRACE_SECONDS) in _read("gozer.1")


def test_man_page_report_entry_names_the_default_output():
    man = _read("gozer.1").replace("\\-", "-")
    assert "docs/log.html" in man and "--out" in man
