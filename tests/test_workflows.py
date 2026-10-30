# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Structure checks for .github/workflows/{ci,release}.yml.

Actions cannot run here, so these tests pin the properties that matter most:
the release is gated on the tag matching the version, only the release job can
write to the repository, lintian fails the build, and actions are pinned.
PyYAML parses the key `on` as the boolean True; `_triggers` handles that.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name):
    return yaml.safe_load((WF / name).read_text())


def _triggers(doc):
    return doc["on"] if "on" in doc else doc[True]


def _run_text(job):
    return "\n".join(s.get("run", "") for s in job["steps"])


def _needs(job):
    n = job.get("needs", [])
    return [n] if isinstance(n, str) else list(n)


def test_release_triggers_on_v_tags():
    trig = _triggers(_load("release.yml"))
    assert trig["push"]["tags"] == ["v*"]
    assert "branches" not in trig["push"]


def test_release_checks_tag_against_version_first():
    jobs = _load("release.yml")["jobs"]
    assert next(iter(jobs)) == "check-tag"
    steps = jobs["check-tag"]["steps"]
    # The checkout has to come first for the script to exist; the check is the
    # first step that runs anything.
    first_run = next(s for s in steps if "run" in s)
    assert "scripts/check_tag.py" in first_run["run"]

    def reaches_check_tag(name, seen=()):
        if name == "check-tag":
            return True
        return any(reaches_check_tag(n) for n in _needs(jobs[name]))

    for name in jobs:
        assert reaches_check_tag(name), f"{name} does not depend on check-tag"


def test_install_matrix_has_the_three_images():
    job = _load("release.yml")["jobs"]["install-test"]
    assert sorted(job["strategy"]["matrix"]["image"]) == [
        "debian:12", "ubuntu:22.04", "ubuntu:24.04"]


def test_only_the_release_job_can_write_contents():
    doc = _load("release.yml")
    assert doc["permissions"] == {"contents": "read"}
    for name, job in doc["jobs"].items():
        perms = job.get("permissions", doc["permissions"])
        if name == "release":
            assert perms.get("contents") == "write"
        else:
            assert perms.get("contents") != "write", name
    ci = _load("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    for name, job in ci["jobs"].items():
        assert job.get("permissions", {}).get("contents") != "write", name
    assert _needs(doc["jobs"]["release"]) == ["install-test"]


def test_site_check_job_runs_build_site_check():
    job = _load("ci.yml")["jobs"]["site-current"]
    assert "python scripts/build_site.py --check" in _run_text(job)
    py = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert py and str(py[0]["with"]["python-version"]) == "3.12"


def test_lintian_fails_on_error():
    assert "--fail-on error" in _run_text(_load("ci.yml")["jobs"]["deb"])
    assert "--fail-on error" in _run_text(_load("release.yml")["jobs"]["build"])


def test_actions_are_pinned_by_tag():
    uses = []
    for name in ("ci.yml", "release.yml"):
        for job in _load(name)["jobs"].values():
            uses += [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+(\.\d+)*", u), u
        assert not u.endswith(("@main", "@master"))


def test_ci_has_no_release_step():
    text = (WF / "ci.yml").read_text()
    assert "gh release" not in text
    assert "tags" not in _triggers(_load("ci.yml")).get("push", {})
