#!/usr/bin/env bash
# Updates the local checkout to the latest published GitHub release tag,
# then re-syncs dependencies and restarts the dashboard if it is running.
# Intended to be run from the repo root so the deployment tracks releases.
#
# See the "Updating to the latest release" section of the README for
# details on why this checks the latest *published release* rather than the
# newest tag.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v git >/dev/null 2>&1; then
  echo "error: git is required" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl is required" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

remote_url=$(git remote get-url origin)
repo=$(echo "$remote_url" | sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##')

echo "Checking latest release for $repo..."

api_url="https://api.github.com/repos/$repo/releases/latest"

if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  # Pass the token via a curl config read from stdin rather than -H/argv, so
  # it doesn't show up in `ps` output for other local users to see.
  release_json=$(curl -fsSL --config - <<EOF
url = "$api_url"
header = "Authorization: Bearer ${GITHUB_TOKEN//\"/\\\"}"
EOF
)
else
  release_json=$(curl -fsSL "$api_url")
fi

# sed prints nothing (exit 0) rather than failing when tag_name is absent,
# so a missing tag falls through to the check below instead of tripping
# `set -e`/`pipefail` with no explanation.
latest_tag=$(printf '%s\n' "$release_json" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)

if [[ -z "$latest_tag" ]]; then
  echo "error: could not determine latest release tag for $repo" >&2
  echo "$release_json" >&2
  exit 1
fi

echo "Latest published release: $latest_tag"

git fetch --tags

current_tag=$(git describe --tags --exact-match 2>/dev/null || true)
head_detached=true
if git symbolic-ref -q HEAD >/dev/null; then
  head_detached=false
fi

if [[ "$current_tag" == "$latest_tag" && "$head_detached" == true ]]; then
  echo "Already on $latest_tag, nothing to do."
else
  # Check out the tag ref explicitly (detached) so a local branch sharing
  # the tag's name can't be selected instead.
  git checkout --detach "refs/tags/$latest_tag"
  echo "Checked out $latest_tag"
fi

uv sync

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet messy-weather-dashboard; then
  echo "Restarting messy-weather-dashboard..."
  sudo systemctl restart messy-weather-dashboard
fi
