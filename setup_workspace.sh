#!/usr/bin/env bash
# Fetch chemotionapi (tag v312.0.1, with its skills/) into the agent's
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
TAG="v312.0.1"
CHECKOUT="$WORKSPACE/chemotionapi"

mkdir -p "$WORKSPACE"
# An interrupted clone leaves a .git without commits ("No commits yet");
# re-clone in that case instead of updating.
if [[ -d "$CHECKOUT/.git" ]] && git -C "$CHECKOUT" rev-parse --verify --quiet HEAD >/dev/null 2>&1; then
  echo "Updating chemotionapi ($TAG)..."
  git -C "$CHECKOUT" fetch --quiet --depth 1 origin tag "$TAG"
  git -C "$CHECKOUT" reset --quiet --hard FETCH_HEAD
else
  echo "Cloning chemotionapi ($TAG)..."
  rm -rf "$CHECKOUT"
  git clone --quiet --depth 1 --branch "$TAG" "$REPO_URL" "$CHECKOUT"
fi

"$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check "$CHECKOUT"
"$VENV_PYTHON" "$APP_DIR/gen_api_reference.py" "$CHECKOUT" "$WORKSPACE/CHEMOTION_API.md"
echo "chemotionapi $(git -C "$CHECKOUT" rev-parse --short HEAD) installed, reference: $WORKSPACE/CHEMOTION_API.md"
