# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Tests for bin/gozer-install-skills, the per-user skill linker.

The Debian package installs skills read-only under /usr/share/tt-gozer/skills
and cannot touch home directories, so the user runs this helper themselves.
Every test runs with HOME pointed at a temp dir and an explicit --from so the
real /usr/share and ~/.claude are never read or written.
"""
import os
import pytest
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "bin", "gozer-install-skills")
SKILLS = ("gozer-keymaster", "gozer-gatekeeper", "gozer-park")


def _src(tmp_path):
    """A fake --from directory: three gozer-* skills plus a non-gozer dir."""
    src = tmp_path / "src"
    for name in SKILLS + ("other-skill",):
        (src / name).mkdir(parents=True)
        (src / name / "SKILL.md").write_text(name + "\n")
    return src


def _run(home, *args):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([SCRIPT, *args], env=env,
                          capture_output=True, text=True)


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_links_the_three_skills(tmp_path):
    home, src = tmp_path / "home", _src(tmp_path)
    home.mkdir()
    r = _run(home, "--from", str(src))
    assert r.returncode == 0, r.stderr
    for name in SKILLS:
        link = home / ".claude" / "skills" / name
        assert os.path.islink(link)
        assert os.path.realpath(link) == os.path.realpath(src / name)
    # Only gozer-* directories are linked.
    assert not (home / ".claude" / "skills" / "other-skill").exists()


def test_unrelated_skill_survives(tmp_path):
    home, src = tmp_path / "home", _src(tmp_path)
    (home / ".claude" / "skills" / "unrelated").mkdir(parents=True)
    r = _run(home, "--from", str(src))
    assert r.returncode == 0, r.stderr
    assert (home / ".claude" / "skills" / "unrelated").is_dir()


def test_second_run_is_a_no_op_exit_0(tmp_path):
    home, src = tmp_path / "home", _src(tmp_path)
    home.mkdir()
    assert _run(home, "--from", str(src)).returncode == 0
    r = _run(home, "--from", str(src))
    assert r.returncode == 0, r.stderr
    for name in SKILLS:
        link = home / ".claude" / "skills" / name
        assert os.path.islink(link)
        # No nested link was made inside the source directory.
        assert not (src / name / name).exists()


def test_refuses_when_target_is_a_real_directory(tmp_path):
    home, src = tmp_path / "home", _src(tmp_path)
    target = home / ".claude" / "skills" / "gozer-park"
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("keep me\n")
    r = _run(home, "--from", str(src))
    assert r.returncode == 1
    assert str(target) in r.stderr
    assert not os.path.islink(target)
    assert sorted(os.listdir(target)) == ["leftover.txt"]
    assert (target / "leftover.txt").read_text() == "keep me\n"


def test_creates_the_target_dir_when_absent(tmp_path):
    home, src = tmp_path / "home", _src(tmp_path)
    home.mkdir()
    assert not (home / ".claude").exists()
    r = _run(home, "--from", str(src))
    assert r.returncode == 0, r.stderr
    assert (home / ".claude" / "skills").is_dir()


def test_from_and_to_flags(tmp_path):
    home, src = tmp_path / "home", _src(tmp_path)
    home.mkdir()
    dest = tmp_path / "elsewhere" / "skills"
    r = _run(home, "--from", str(src), "--to", str(dest))
    assert r.returncode == 0, r.stderr
    for name in SKILLS:
        assert os.path.islink(dest / name)
    assert not (home / ".claude").exists()


def test_default_from_falls_back_to_repo_skills(tmp_path):
    # With no --from and no /usr/share/tt-gozer/skills, the script uses
    # <script dir>/../skills. Reached through a symlink, as ~/.local/bin does.
    if os.path.isdir("/usr/share/tt-gozer/skills"):
        pytest.skip("the package is installed here; the fallback is not reachable")
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    os.symlink(SCRIPT, home / "bin" / "gozer-install-skills")
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([str(home / "bin" / "gozer-install-skills")],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name in SKILLS:
        assert os.path.realpath(home / ".claude" / "skills" / name) == \
            os.path.realpath(os.path.join(REPO, "skills", name))


def test_help_ends_with_a_complete_sentence():
    r = subprocess.run([SCRIPT, "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    last = r.stdout.rstrip("\n").splitlines()[-1]
    assert last.endswith(".")
