#!/usr/bin/env bash
# Pull the current test doc back from the device into pulled/<n>/
set -euo pipefail
cd "$(dirname "$0")/.."
D=$(head -1 stage/CURRENT); n=$(ls -d pulled/*/ 2>/dev/null | wc -l || true); dst=pulled/$((n+1)); mkdir -p "$dst"
rsync -a rm:~/.local/share/remarkable/xochitl/"$D"* "$dst"/
echo "$dst"
