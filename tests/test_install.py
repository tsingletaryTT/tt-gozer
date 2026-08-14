import os
import subprocess
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_installer_is_executable():
    assert os.access(os.path.join(REPO, "install.sh"), os.X_OK)


def test_installer_links_cli_and_skills(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    # A pre-existing unrelated skill must survive: we link individual dirs,
    # never the parent, because ~/.claude/skills already holds ~30 of them.
    (home / ".claude" / "skills" / "unrelated").mkdir()

    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([os.path.join(REPO, "install.sh")],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    assert os.path.islink(home / ".local" / "bin" / "gozer")
    assert os.path.islink(home / ".claude" / "skills" / "gozer-keymaster")
    assert os.path.islink(home / ".claude" / "skills" / "gozer-gatekeeper")
    assert (home / ".claude" / "skills" / "unrelated").is_dir()


def test_installer_is_idempotent(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    env = dict(os.environ, HOME=str(home))
    for _ in range(2):
        r = subprocess.run([os.path.join(REPO, "install.sh")],
                           env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_skills_have_frontmatter_with_functional_descriptions():
    for name in ("gozer-keymaster", "gozer-gatekeeper"):
        path = os.path.join(REPO, "skills", name, "SKILL.md")
        text = open(path).read()
        assert text.startswith("---\n")
        assert f"name: {name}" in text
        desc = [l for l in text.splitlines() if l.startswith("description:")][0]
        # An agent must match on function, not on the Ghostbusters metaphor.
        assert "chip" in desc.lower() or "hardware" in desc.lower()
