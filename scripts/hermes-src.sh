#!/usr/bin/env bash
# Fetch the Hermes source at the pinned commit into .cache/hermes-agent (or $1).
# The harvest reads it, and the end-to-end tests import Hermes' own test rig from it:
# Hermes' tests/ are not part of its installed package.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
pin=$(tr -d "[:space:]" < "$root/HERMES_PIN")
dest=${1:-$root/.cache/hermes-agent}
if [ ! -d "$dest/.git" ]; then
  git init -q "$dest"
  git -C "$dest" remote add origin https://github.com/NousResearch/hermes-agent.git
fi
if [ "$(git -C "$dest" rev-parse -q --verify HEAD 2>/dev/null || true)" != "$pin" ]; then
  git -C "$dest" fetch -q --depth 1 origin "$pin"
  git -C "$dest" checkout -q --detach FETCH_HEAD
fi
echo "$dest at $(git -C "$dest" rev-parse --short HEAD)"
