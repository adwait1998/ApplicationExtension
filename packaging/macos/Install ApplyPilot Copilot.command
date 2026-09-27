#!/bin/bash
# Installs ApplyPilot Copilot for this Mac user.
# First time: right-click this file -> Open -> Open (macOS asks because it isn't from the App Store).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Applications/ApplyPilot Copilot"

echo "Installing ApplyPilot Copilot to: $DEST"
pkill -f "$DEST/app/ApplyPilotCopilot" 2>/dev/null || true   # stop an older copy that's running
rm -rf "$DEST/app" "$DEST/extension"
mkdir -p "$DEST"
ditto "$HERE/ApplyPilotCopilot" "$DEST/app"
ditto "$HERE/extension" "$DEST/extension"
cp "$HERE/SETUP.txt" "$DEST/SETUP.txt"
cp "$HERE/Uninstall ApplyPilot Copilot.command" "$DEST/"
chmod +x "$DEST/Uninstall ApplyPilot Copilot.command"

# Downloaded files carry a quarantine flag. Chrome starts the helper directly and
# macOS would silently refuse to run it, so clear the flag on these files only.
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

"$DEST/app/ApplyPilotCopilot" install-host --extension-dir "$DEST/extension"

echo
echo "Done. Next, in Chrome:"
echo "  1. Open chrome://extensions and turn on Developer mode (top right)."
echo "  2. Click 'Load unpacked' and choose this folder:"
echo "       $DEST/extension"
echo "     (in the file picker: Home > Applications > ApplyPilot Copilot > extension)"
echo "The setup guide is opening now."
open "$DEST"
open -e "$DEST/SETUP.txt" || true
echo
read -n 1 -s -r -p "Press any key to close this window."
