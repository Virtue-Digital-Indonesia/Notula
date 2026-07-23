#!/usr/bin/env bash
# Package dist/Notula.app into a distributable dist/Notula-<ver>.dmg
# (drag-to-Applications layout). Run after: ./.venv/bin/python setup.py py2app
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/Notula.app"
VER="$(defaults read "$PWD/$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo 1.0)"
DMG="dist/Notula-${VER}.dmg"
STAGE="dist/dmgroot"

[ -d "$APP" ] || { echo "Build the app first: ./.venv/bin/python setup.py py2app"; exit 1; }

rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/Notula.app"
ln -s /Applications "$STAGE/Applications"          # drag-to-install target

hdiutil create -volname "Notula" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "created $DMG ($(du -h "$DMG" | cut -f1))"
