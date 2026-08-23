#!/usr/bin/env sh
# Install the repository's git hooks into the local clone.
# Pure-Python repo with no npm step — this is the substitute for
# Homestead's `npm run hooks:install`. Idempotent; safe to re-run.
set -eu
REPO_ROOT=$(git rev-parse --show-toplevel)
git config core.hooksPath .githooks
printf 'hooksPath -> %s/.githooks\n' "$REPO_ROOT"
