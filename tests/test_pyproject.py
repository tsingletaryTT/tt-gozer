# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Pins the shape of pyproject.toml, which Debian's pybuild reads to package gozer."""
import os
import pytest

tomllib = pytest.importorskip("tomllib")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def test_version_is_read_from_the_package():
    data = _load()
    assert data["project"]["dynamic"] == ["version"]
    assert data["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "gozer.__version__"


def test_entry_point_is_gozer_cli_main():
    assert _load()["project"]["scripts"]["gozer"] == "gozer.cli:main"


def test_only_the_gozer_package_ships():
    find = _load()["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["gozer*"]


def test_report_template_and_archive_are_package_data():
    pkg_data = _load()["tool"]["setuptools"]["package-data"]["gozer"]
    assert "report_template.html" in pkg_data
    assert "history_archive.jsonl" in pkg_data


def test_requires_python_310():
    assert _load()["project"]["requires-python"] == ">=3.10"


def test_setuptools_floor_is_old_enough_for_distro_packages():
    # Debian 12 ships setuptools 66.x, Ubuntu 24.04 68.x; the floor must not exceed 66.
    import re
    (req,) = _load()["build-system"]["requires"]
    m = re.fullmatch(r"setuptools>=(\d+)(?:\.\d+)*", req)
    assert m and int(m.group(1)) <= 66


def test_license_is_apache_text_form():
    # The SPDX string form needs setuptools 77+; the table form works from 61.
    assert _load()["project"]["license"] == {"text": "Apache-2.0"}
