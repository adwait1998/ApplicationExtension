#!/usr/bin/env bash
# Builds the macOS (Apple Silicon) friend bundle.
#   build/friend/macos/ApplyPilotCopilot-mac/   ApplyPilotCopilot/ (app), extension/, SETUP.txt, *.command
#   dist/ApplyPilotCopilot-macOS-arm64-<version>.zip
# Usage (on an arm64 Mac, from the repo root):  PYTHON=python3 bash packaging/build_macos.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ "$(uname -s)" = "Darwin" ] || { echo "build_macos.sh must run on macOS" >&2; exit 1; }
[ "$(uname -m)" = "arm64" ] || { echo "this build targets Apple Silicon; run it on an arm64 Mac or runner" >&2; exit 1; }

PYTHON="${PYTHON:-python3}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/applypilot/__init__.py)"
[ -n "$VERSION" ] || { echo "could not read __version__ from src/applypilot/__init__.py" >&2; exit 1; }
echo "ApplyPilot Copilot friend build $VERSION (macOS arm64)"

VENV="$ROOT/packaging/.venv-mac"
[ -x "$VENV/bin/python" ] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r packaging/requirements-friend.txt

OUT="$ROOT/build/friend/macos/ApplyPilotCopilot-mac"
rm -rf "$ROOT/build/friend/macos"
mkdir -p "$OUT"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean --distpath "$OUT" \
  --workpath "$ROOT/build/pyinstaller-mac" packaging/applypilot_copilot.spec
"$VENV/bin/python" packaging/stage_extension.py --dest "$OUT/extension"
cp docs/FRIEND_SETUP.md "$OUT/SETUP.txt"
cp "packaging/macos/Install ApplyPilot Copilot.command" "packaging/macos/Uninstall ApplyPilot Copilot.command" "$OUT/"
chmod +x "$OUT"/*.command

# PyInstaller ad-hoc signs the app; an unsigned arm64 binary won't run at all.
codesign --verify --verbose "$OUT/ApplyPilotCopilot/ApplyPilotCopilot"

"$VENV/bin/python" scripts/verify_friend_bundle.py --bundle "$OUT"

mkdir -p "$ROOT/dist"
ZIP="$ROOT/dist/ApplyPilotCopilot-macOS-arm64-$VERSION.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$OUT" "$ZIP"
echo "Built $ZIP"
