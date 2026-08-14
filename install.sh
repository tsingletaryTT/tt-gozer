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

# CLI
ln -sfn "$REPO/bin/gozer" "$BIN_DIR/gozer"
echo "  $BIN_DIR/gozer -> $REPO/bin/gozer"

# Skills. Link each directory INDIVIDUALLY: ~/.claude/skills already holds
# ~30 unrelated skills, so linking the parent would clobber them.
for skill in gozer-keymaster gozer-gatekeeper; do
    ln -sfn "$REPO/skills/$skill" "$SKILL_DIR/$skill"
    echo "  $SKILL_DIR/$skill -> $REPO/skills/$skill"
done

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "  note: $BIN_DIR is not on PATH — add it to your shell profile" ;;
esac

echo "  done. Try: gozer status"
