#!/usr/bin/env bash
# Fetch a release tag of chemotionapi (with its skills/) into the agent's
# workspace, install it into the given venv and generate the API reference
# the agent reads.
#
# Usage: setup_workspace.sh <app dir> <venv python> [<tag> | --branch <branch>]
# The app dir is where app.py lives; the workspace is <app dir>/workspace.
# Without a tag: the installed one if it is still released, else the newest
# (api_versions.py; CHEMOTION_API_TAG preselects one). --branch installs the
# head of a branch instead (the settings' developer mode).
set -euo pipefail

APP_DIR="$1"
VENV_PYTHON="$2"
WORKSPACE="$APP_DIR/workspace"
REPO_URL="https://github.com/Chemotion/chemotionapi.git"
CHECKOUT="$WORKSPACE/chemotionapi"

if [[ "${3:-}" == "--branch" ]]; then
  REF="${4:?Error: --branch needs a branch name.}"
  if ! git check-ref-format --branch "$REF" >/dev/null 2>&1; then
    echo "Error: \"$REF\" is no branch name." >&2
    exit 1
  fi
  REFSPEC="refs/heads/$REF"
  LABEL="branch $REF"
else
  REF="${3:-$("$VENV_PYTHON" "$APP_DIR/api_versions.py" default "$WORKSPACE")}"
  # Only release tags the app supports (api_versions.TAG_PATTERN).
  if [[ ! "$REF" =~ ^v[0-9]{3,} ]]; then
    echo "Error: \"$REF\" is no chemotionapi version (they start with v and three digits)." >&2
    exit 1
  fi
  REFSPEC="refs/tags/$REF"
  LABEL="$REF"
fi

mkdir -p "$WORKSPACE"
# An interrupted clone leaves a .git without commits ("No commits yet");
# re-clone in that case instead of updating.
if [[ -d "$CHECKOUT/.git" ]] && git -C "$CHECKOUT" rev-parse --verify --quiet HEAD >/dev/null 2>&1; then
  echo "Updating chemotionapi ($LABEL)..."
  git -C "$CHECKOUT" fetch --quiet --depth 1 origin "$REFSPEC"
  git -C "$CHECKOUT" reset --quiet --hard FETCH_HEAD
else
  echo "Cloning chemotionapi ($LABEL)..."
  rm -rf "$CHECKOUT"
  git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$CHECKOUT"
fi

"$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check "$CHECKOUT"
"$VENV_PYTHON" "$APP_DIR/gen_api_reference.py" "$CHECKOUT" "$WORKSPACE/CHEMOTION_API.md" "$LABEL"
# A branch has no tag file (api_versions.installed_tag() is then "") and vice versa.
if [[ "$LABEL" == branch* ]]; then
  echo "$REF" >"$WORKSPACE/chemotionapi.branch"
  rm -f "$WORKSPACE/chemotionapi.tag"
else
  echo "$REF" >"$WORKSPACE/chemotionapi.tag"
  rm -f "$WORKSPACE/chemotionapi.branch"
fi
echo "chemotionapi $LABEL ($(git -C "$CHECKOUT" rev-parse --short HEAD)) installed, reference: $WORKSPACE/CHEMOTION_API.md"
