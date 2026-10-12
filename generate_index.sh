#!/usr/bin/env bash
set -euo pipefail

# Publish DATA's contents at the site root. Default output directory: dist.
node "$(dirname "$0")/scripts/generate-index.mjs" "$@"
