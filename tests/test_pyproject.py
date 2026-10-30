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
