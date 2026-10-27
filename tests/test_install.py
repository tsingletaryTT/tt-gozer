# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
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
    assert os.path.islink(home / ".claude" / "skills" / "gozer-park")
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


PARK_SKILL = os.path.join(REPO, "skills", "gozer-park", "SKILL.md")


def test_gozer_park_skill_exists_and_names_the_commands_it_teaches():
    text = open(PARK_SKILL).read()
    assert text.startswith("---\nname: gozer-park\n")
    for needle in ("--owner-pid", "gozer reset", "gozer env", "HELD-FOREIGN",
                   "gozer release"):
        assert needle in text, needle


def _subparsers():
    """Map each gozer verb (and alias) to its argparse parser, built from the
    real CLI so the skill is checked against what gozer actually has."""
    import argparse
    from gozer.cli import build_parser
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("gozer has no subcommands")


def _park_bash_lines():
    """Logical command lines from the skill's fenced bash blocks, with
    backslash continuations joined."""
    import re
    text = open(PARK_SKILL).read()
    lines = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.S):
        lines.extend(block.replace("\\\n", " ").splitlines())
    return lines


def test_gozer_park_skill_shows_only_verbs_and_flags_gozer_has():
    import re
    verbs = _subparsers()
    seen = set()
    for line in _park_bash_lines():
        for m in re.finditer(r"gozer\s+([a-z][\w-]*)([^|;&)]*)", line):
            verb, rest = m.group(1), m.group(2)
            assert verb in verbs, f"skill shows unknown verb: {line}"
            seen.add(verb)
            flags = {opt for a in verbs[verb]._actions for opt in a.option_strings}
            for flag in re.findall(r"(?<![\w-])(--[a-z][\w-]*)", rest):
                assert flag in flags, f"gozer {verb} has no {flag}: {line}"
    # Commands named in prose, in backticks, must exist too. Flags in prose
    # are checked the same way.
    text = open(PARK_SKILL).read()
    for span in re.findall(r"`(gozer\s[^`]*)`", text):
        m = re.match(r"gozer\s+([a-z][\w-]*)(.*)", span)
        verb = m.group(1)
        assert verb in verbs, f"skill names unknown verb in prose: {span}"
        flags = {opt for a in verbs[verb]._actions for opt in a.option_strings}
        for flag in re.findall(r"(?<![\w-])(--[a-z][\w-]*)", m.group(2)):
            assert flag in flags, f"gozer {verb} has no {flag}: {span}"
    # A standalone backticked flag such as `--ticket` is checked against the
    # flags of every verb the skill uses. It must belong to at least one of
    # them. (The skill does not say which verb a prose flag goes with, so a
    # per-verb check is not practical.)
    used_flags = set()
    for v in seen:
        used_flags |= {opt for a in verbs[v]._actions for opt in a.option_strings}
    for span in re.findall(r"`(--[^`\s]+)[^`]*`", text):
        assert span in used_flags, f"skill names unknown flag in prose: {span}"
    # The block must really exercise the commands the skill teaches.
    assert {"acquire", "env", "reset", "release"} <= seen, seen


# Verbs of the real tt-model CLI. Taken from `tt-model --help` on 2026-10-02.
# Refresh this list whenever tt-model changes.
TT_MODEL_VERBS = {
    "package", "package-thin", "publish", "unpublish", "push", "pull",
    "info", "list", "search", "profiles", "login", "serve", "curl",
    "stop", "logs", "rm", "version",
}


def test_gozer_park_skill_names_only_tt_model_verbs_that_exist():
    """The gozer check above does not cover tt-model. A backticked
    `tt-model <verb>` span must use a verb from the list above."""
    import re
    text = open(PARK_SKILL).read()
    spans = re.findall(r"`(tt-model\s[^`]*)`", text)
    assert spans, "skill no longer names any tt-model command"
    for span in spans:
        verb = span.split()[1]
        assert verb in TT_MODEL_VERBS, f"skill names unknown tt-model verb: {span}"


# Verbs of docker the skill may name in backticks. Keep it small.
DOCKER_VERBS = {"ps", "inspect", "logs", "stop", "images", "info"}
# Tools the skill names in backticked command lines. Each must be installed
# for the install check below to run.
SKILL_TOOLS = ["docker", "ps", "pgrep", "ss", "curl", "setsid", "grep", "xargs"]


def test_gozer_park_skill_names_only_real_docker_verbs():
    """Every `docker <verb>` anywhere inside a backticked span, including
    inside a pipeline, must use a verb from DOCKER_VERBS."""
    import re
    text = open(PARK_SKILL).read()
    found = []
    for span in re.findall(r"`([^`]*)`", text):
        for m in re.finditer(r"\bdocker\s+([a-z][\w-]*)", span):
            found.append(m.group(0))
            assert m.group(1) in DOCKER_VERBS, f"unknown docker verb in: {span}"
    assert found, "skill no longer names any docker command"


def test_gozer_park_skill_command_spans_parse_and_tools_exist():
    """Each backticked span that starts with a tool the skill uses must parse
    with shlex (balanced quotes). This does not prove the flags are valid; the
    commands were run by hand on a real box when the skill was written. The
    tools named in the skill must be installed where the test runs, and the
    check skips with a reason if one is missing."""
    import re
    import shlex
    import shutil
    text = open(PARK_SKILL).read()
    checked = 0
    for span in re.findall(r"`([^`]*)`", text):
        if span.split(" ")[0] in SKILL_TOOLS:
            shlex.split(span.replace("<", "x").replace(">", "x"))
            checked += 1
    assert checked, "no tool command spans found"
    missing = [t for t in SKILL_TOOLS if shutil.which(t) is None]
    if missing:
        pytest.skip(f"tools not installed here: {missing}")
