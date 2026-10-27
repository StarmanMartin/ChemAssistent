#!/usr/bin/env bash
# Fetch a release tag of chemotionapi (with its skills/) into the agent's
# workspace, install it into the given venv and generate the API reference
# the agent reads.
#
# Usage: setup_workspace.sh <app dir> <venv python> [<tag>]
# The app dir is where app.py lives; the workspace is <app dir>/workspace.
# Without a tag: the installed one if it is still released, else the newest
# (api_versions.py; CHEMOTION_API_TAG preselects one).
set -euo pipefail

APP_DIR="$1"
VENV_PYTHON="$2"
WORKSPACE="$APP_DIR/workspace"
REPO_URL="https://github.com/Chemotion/chemotionapi.git"
TAG="${3:-$("$VENV_PYTHON" "$APP_DIR/api_versions.py" default "$WORKSPACE")}"
CHECKOUT="$WORKSPACE/chemotionapi"

# Only release tags the app supports (api_versions.TAG_PATTERN).
if [[ ! "$TAG" =~ ^v[0-9]{3,} ]]; then
  echo "Error: \"$TAG\" is no chemotionapi version (they start with v and three digits)." >&2
  exit 1
fi

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
"$VENV_PYTHON" "$APP_DIR/gen_api_reference.py" "$CHECKOUT" "$WORKSPACE/CHEMOTION_API.md" "$TAG"
echo "$TAG" >"$WORKSPACE/chemotionapi.tag"
echo "chemotionapi $TAG ($(git -C "$CHECKOUT" rev-parse --short HEAD)) installed, reference: $WORKSPACE/CHEMOTION_API.md"
