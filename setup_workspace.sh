#!/usr/bin/env bash
# Fetch chemotionapi (branch version-3, with its skills/) into the agent's
# workspace, install it into the given venv and generate the API reference
# the agent reads.
#
# Usage: setup_workspace.sh <app dir> <venv python>
# The app dir is where app.py lives; the workspace is <app dir>/workspace.
set -euo pipefail

APP_DIR="$1"
VENV_PYTHON="$2"
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
