#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export LD_LIBRARY_PATH="$ROOT/vendor${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "${CORET_PYTHON:-python3}" "$ROOT/main.py" "$@"
