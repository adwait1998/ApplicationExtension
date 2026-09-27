#!/bin/bash
# Removes ApplyPilot Copilot for this Mac user. Your profile and answers in ~/.applypilot are kept.
set -euo pipefail
DEST="$HOME/Applications/ApplyPilot Copilot"

if [ -x "$DEST/app/ApplyPilotCopilot" ]; then
  "$DEST/app/ApplyPilotCopilot" uninstall-host || true
fi
pkill -f "$DEST/app/ApplyPilotCopilot" 2>/dev/null || true
rm -rf "$DEST"

echo "ApplyPilot Copilot is removed. Also remove the extension in chrome://extensions."
echo "Your data (profile, answers) is still in ~/.applypilot. Delete that folder to remove it too."
read -n 1 -s -r -p "Press any key to close this window."
