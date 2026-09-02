#!/usr/bin/env bash
# Push staged notebooks/folders (by uuid) to the device, then restart xochitl once.
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -ge 1 ] || { echo "usage: push.sh <uuid> [<uuid>...]" >&2; exit 2; }
args=(); for u in "$@"; do args+=(stage/"$u"*); done
rsync -a "${args[@]}" rm:~/.local/share/remarkable/xochitl/
ssh rm 'systemctl reset-failed xochitl; systemctl restart xochitl && sleep 3 && systemctl is-active xochitl'
