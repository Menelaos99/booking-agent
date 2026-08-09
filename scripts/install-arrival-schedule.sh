#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.menelaos.booking-agent.arrivals"
SOURCE="$PROJECT_ROOT/deploy/$LABEL.plist"
DESTINATION="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$PROJECT_ROOT/state" "$HOME/Library/LaunchAgents"
chmod 700 "$PROJECT_ROOT/state"
plutil -lint "$SOURCE"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL"
fi

install -m 600 "$SOURCE" "$DESTINATION"
launchctl bootstrap "$DOMAIN" "$DESTINATION"
launchctl enable "$DOMAIN/$LABEL"

echo "Installed $LABEL. It checks hourly and runs once per Athens day after 09:00."

