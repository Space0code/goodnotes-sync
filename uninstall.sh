#!/bin/bash
# Stop and remove the launchd agent and installed code. Exported handwritten PDFs are always kept.
#   --purge   also delete settings, state, cache and logs
set -uo pipefail
LABEL=local.goodnotes-sync
BASE="$HOME/Library/Application Support/goodnotes-sync"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "agent $LABEL stopped"
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$BASE/app"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$BASE" "$HOME/Library/Caches/goodnotes-sync" "$HOME/Library/Logs/goodnotes-sync"
  echo "settings/state/logs removed"
fi
echo "uninstalled (handwritten/ PDFs left in place)"
