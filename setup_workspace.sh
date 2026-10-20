#!/usr/bin/env bash
# Fetch chemotionapi (branch version-3) into the opencode workspace, install it
# into the given venv and generate the API reference the agent reads. With a
# skills dir, also copy chemotionapi's opencode skills (skills/<name>/) there.
#
# Usage: setup_workspace.sh <app dir> <venv python> [<opencode skills dir>]
# The app dir is where app.py lives; the workspace is <app dir>/workspace.
set -euo pipefail

APP_DIR="$1"
VENV_PYTHON="$2"
SKILLS_DIR="${3:-}"
WORKSPACE="$APP_DIR/workspace"
REPO_URL="https://github.com/Chemotion/chemotionapi.git"
BRANCH="version-3"
CHECKOUT="$WORKSPACE/chemotionapi"

mkdir -p "$WORKSPACE"
if [[ -d "$CHECKOUT/.git" ]]; then
  echo "Updating chemotionapi ($BRANCH)..."
  git -C "$CHECKOUT" fetch --quiet --depth 1 origin "$BRANCH"
  git -C "$CHECKOUT" reset --quiet --hard FETCH_HEAD
else
  echo "Cloning chemotionapi ($BRANCH)..."
  rm -rf "$CHECKOUT"
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$CHECKOUT"
fi

"$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check "$CHECKOUT"
"$VENV_PYTHON" "$APP_DIR/gen_api_reference.py" "$CHECKOUT" "$WORKSPACE/CHEMOTION_API.md"
echo "chemotionapi $(git -C "$CHECKOUT" rev-parse --short HEAD) installed, reference: $WORKSPACE/CHEMOTION_API.md"

# Each skill replaces an older copy of itself; other skills stay untouched.
if [[ -n "$SKILLS_DIR" && -d "$CHECKOUT/skills" ]]; then
  mkdir -p "$SKILLS_DIR"
  for skill in "$CHECKOUT"/skills/*/; do
    name="$(basename "$skill")"
    [[ -f "$skill/SKILL.md" ]] || continue
    rm -rf "${SKILLS_DIR:?}/$name"
    cp -R "$skill" "$SKILLS_DIR/$name"
    echo "Installed the opencode skill $name into $SKILLS_DIR."
  done
fi
