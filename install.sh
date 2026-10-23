#!/bin/bash
# Install/update goodnotes-sync: copy the script to ~/Library/Application Support/goodnotes-sync/app and
# (re)load the launchd agent. Safe to re-run; never touches settings.json, state or Keychain.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
BASE="$HOME/Library/Application Support/goodnotes-sync"
APP="$BASE/app"
LABEL=local.goodnotes-sync
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$APP" "$HOME/Library/Logs/goodnotes-sync" "$HOME/Library/LaunchAgents"
chmod 700 "$BASE"

if [ ! -f "$BASE/settings.json" ]; then
  cp "$SRC/settings.example.json" "$BASE/settings.json"
  echo "Created $BASE/settings.json. Fill in materials, gmail, goodnotes_email and courses, then re-run ./install.sh"
  exit 1
fi
if grep -q '"you@gmail.com"' "$BASE/settings.json"; then
  echo "Edit $BASE/settings.json first (gmail is still the example), then re-run ./install.sh"
  exit 1
fi
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" command -v gs >/dev/null || echo "warning: gs not found (brew install ghostscript)"
ls -d "$HOME"/Library/CloudStorage/GoogleDrive-* >/dev/null 2>&1 ||
  echo "warning: Google Drive for desktop is not set up (brew install --cask google-drive, then sign in)"

cp "$SRC/goodnotes_sync.py" "$APP/"
# The launcher is what you grant Full Disk Access to. It is rebuilt only when launcher.c changes, because
# macOS ties the grant to this exact binary.
if [ ! -x "$APP/goodnotes-sync" ] || ! cmp -s "$SRC/launcher.c" "$APP/launcher.c"; then
  cc -O2 -Wall -o "$APP/goodnotes-sync" "$SRC/launcher.c"
  cp "$SRC/launcher.c" "$APP/launcher.c"
  echo "built $APP/goodnotes-sync (allow its iCloud Drive access prompt, see README step 5)"
fi
sed -e "s|__APP__|$APP|g" -e "s|__HOME__|$HOME|g" "$SRC/launchd/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $APP; agent $LABEL loaded (imports every 5 min, exports after lectures and at export_times)"
echo "First install: click Allow when macOS asks whether goodnotes-sync may access iCloud Drive (README step 5)."
