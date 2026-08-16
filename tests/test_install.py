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


def test_installer_refuses_a_real_directory_at_a_skill_target(tmp_path):
    # ln -sfn treats an existing real directory as a container and links
    # *inside* it (~/.claude/skills/gozer-keymaster/gozer-keymaster), leaving
    # SKILL.md unreachable at the path Claude Code actually scans -- silently,
    # with exit 0. A stale hand-copied skill directory from another machine
    # is a realistic starting condition (that's what tt-home is for), so the
    # installer must refuse instead of half-installing over it.
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    target = home / ".claude" / "skills" / "gozer-keymaster"
    target.mkdir()
    (target / "leftover.txt").write_text("pre-existing content, not a symlink\n")

    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([os.path.join(REPO, "install.sh")],
                       env=env, capture_output=True, text=True)

    assert r.returncode != 0
    assert str(target) in (r.stdout + r.stderr)
    # The bug this pins: no symlink must appear *inside* the real directory.
    assert not os.path.islink(target)
    assert not (target / "gozer-keymaster").exists()
    assert (target / "leftover.txt").exists()


def test_installer_refuses_a_real_directory_at_the_bin_target(tmp_path):
    # Same trap, same guard, applied to bin/gozer -- a directory there would
    # hit the identical "linked inside instead of replaced" failure mode.
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    target = home / ".local" / "bin" / "gozer"
    target.mkdir()

    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([os.path.join(REPO, "install.sh")],
                       env=env, capture_output=True, text=True)

    assert r.returncode != 0
    assert str(target) in (r.stdout + r.stderr)
    assert not os.path.islink(target)
    assert not (target / "gozer").exists()


def test_skills_have_frontmatter_with_functional_descriptions():
    for name in ("gozer-keymaster", "gozer-gatekeeper"):
        path = os.path.join(REPO, "skills", name, "SKILL.md")
        text = open(path).read()
        assert text.startswith("---\n")
        assert f"name: {name}" in text
        desc = [l for l in text.splitlines() if l.startswith("description:")][0]
        # An agent must match on function, not on the Ghostbusters metaphor.
        assert "chip" in desc.lower() or "hardware" in desc.lower()


def test_contrib_systemd_units_exist_and_are_well_formed():
    service = os.path.join(REPO, "contrib", "gozer-reconcile.service")
    timer = os.path.join(REPO, "contrib", "gozer-reconcile.timer")
    assert os.path.exists(service)
    assert os.path.exists(timer)

    svc_text = open(service).read()
    assert "[Service]" in svc_text
    assert "Type=oneshot" in svc_text
    assert "gozer reconcile" in svc_text
    # Must not restart on failure -- a wedged or absent gozer must not spin.
    assert "Restart=no" in svc_text
    # Harmless if gozer is absent: check for the binary before invoking it,
    # rather than letting a bare `gozer reconcile` fail with "command not
    # found" noise.
    assert "command -v gozer" in svc_text

    timer_text = open(timer).read()
    assert "[Timer]" in timer_text
    assert "5min" in timer_text
    assert "Persistent=false" in timer_text
    assert "[Install]" in timer_text


def test_install_sh_never_enables_the_systemd_unit(tmp_path):
    """Enabling a systemd unit changes the user's machine in a way they did
    not ask for -- install.sh must only ever print the two commands, never
    run them. This also must never touch the real $HOME; the test's fake
    HOME is what proves that."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    env = dict(os.environ, HOME=str(home))

    r = subprocess.run([os.path.join(REPO, "install.sh")],
                       env=env, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert "systemctl" not in " ".join([r.stdout, r.stderr]) or \
        "systemctl --user enable" in r.stdout
    # No unit was actually installed anywhere under the fake HOME.
    assert not (home / ".config" / "systemd" / "user" /
               "gozer-reconcile.timer").exists()


def test_install_sh_prints_the_optional_reconcile_instructions(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    env = dict(os.environ, HOME=str(home))

    r = subprocess.run([os.path.join(REPO, "install.sh")],
                       env=env, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert "gozer-reconcile.timer" in r.stdout
    assert "systemctl --user enable" in r.stdout
