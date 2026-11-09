#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY="${CORET_PYTHON:-python3}"
BUILD="${CORET_PACKAGE_BUILD:-$ROOT/.packaging}"
export PYINSTALLER_CONFIG_DIR="$BUILD/cache"
export LD_LIBRARY_PATH="$ROOT/vendor${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$PY" -m PyInstaller --noconfirm --onedir --windowed --name CoreT-Desktop \
  --distpath "$BUILD/dist" --workpath "$BUILD/build" --specpath "$BUILD" \
  --hidden-import importlib.resources \
  --exclude-module matplotlib --exclude-module PIL --exclude-module pytest \
  --exclude-module torch --exclude-module tensorflow --exclude-module jax --exclude-module cupy \
  --paths "$ROOT" --add-data "$ROOT/assets:assets" \
  --add-data "$ROOT/coret/qc_algorithm/profile.json:coret/qc_algorithm" \
  --add-data "$ROOT/coret/qc_algorithm/continuous.json:coret/qc_algorithm" \
  --add-binary "$ROOT/vendor/libxcb-cursor.so.0:." "$ROOT/main.py"
"$PY" - "$BUILD/dist/CoreT-Desktop" "$ROOT" <<'PY'
from pathlib import Path
import shutil,sys
src,dst=map(Path,sys.argv[1:])
shutil.copytree(src/'_internal',dst/'_internal',dirs_exist_ok=True)
shutil.copy2(src/'CoreT-Desktop',dst/'CoreT-Desktop')
print('Ready:',dst/'CoreT-Desktop')
PY
