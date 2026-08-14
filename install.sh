#!/usr/bin/env bash
# Install gozer: the CLI onto PATH, the two skills into ~/.claude/skills.
#
# Idempotent — safe to re-run. Links rather than copies, so a git pull updates
# everything in place.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SKILL_DIR="${HOME}/.claude/skills"

mkdir -p "$BIN_DIR" "$SKILL_DIR"

echo "--- gozer ---"

# `ln -sfn` only replaces cleanly when the target is absent, a broken/stale
# symlink, or a regular file. If the target is a REAL directory, ln treats it
# as a container and links *inside* it instead -- e.g.
#   ~/.claude/skills/gozer-keymaster/gozer-keymaster -> $REPO/skills/gozer-keymaster
# which leaves SKILL.md unreachable at the path Claude Code scans, silently,
# with exit 0. A stale hand-copied directory from another machine is a
# realistic starting condition, so refuse rather than half-install over it.
# Never rm -rf it: something the user put there deliberately may be inside.
refuse_if_real_dir() {
    local target="$1"
    if [ -d "$target" ] && [ ! -L "$target" ]; then
        echo "error: $target already exists as a real directory (not a symlink)." >&2
        echo "  contents: $(ls -A "$target" 2>/dev/null | tr '\n' ' ')" >&2
        echo "  refusing to install here -- ln would link *inside* it and the" >&2
        echo "  installer would silently do nothing useful. Move or remove" >&2
        echo "  $target yourself, then re-run this installer." >&2
        exit 1
    fi
}

# CLI
refuse_if_real_dir "$BIN_DIR/gozer"
ln -sfn "$REPO/bin/gozer" "$BIN_DIR/gozer"
echo "  $BIN_DIR/gozer -> $REPO/bin/gozer"

# Skills. Link each directory INDIVIDUALLY: ~/.claude/skills already holds
# ~30 unrelated skills, so linking the parent would clobber them.
for skill in gozer-keymaster gozer-gatekeeper; do
    refuse_if_real_dir "$SKILL_DIR/$skill"
    ln -sfn "$REPO/skills/$skill" "$SKILL_DIR/$skill"
    echo "  $SKILL_DIR/$skill -> $REPO/skills/$skill"
done

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "  note: $BIN_DIR is not on PATH — add it to your shell profile" ;;
esac

echo "  done. Try: gozer status"
