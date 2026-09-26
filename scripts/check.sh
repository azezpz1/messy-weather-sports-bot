#!/usr/bin/env bash
# Runs the same checks CI runs (lint, format check, type check, tests).
# Usage: scripts/check.sh [pytest args...]
#   scripts/check.sh                       # full suite, matches CI exactly
#   scripts/check.sh -m "not integration"  # skip network-dependent tests

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "-> ruff check"
uv run ruff check .

echo "-> ruff format --check"
uv run ruff format --check .

echo "-> ty check"
uv run ty check

echo "-> pytest $*"
uv run pytest "$@"
