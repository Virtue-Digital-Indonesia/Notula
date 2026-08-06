#!/usr/bin/env bash
# Sign dist/Notula.app with a stable identity, then package a distributable
# dist/Notula-<ver>.dmg (drag-to-Applications).
#
# Signing matters: macOS keys the Screen Recording / Microphone permissions on
# the app's code signature. An ad-hoc signature (py2app's default) has no stable
# identity, so the grant never sticks and the app is re-prompted every launch.
# Signing with a real identity (Developer ID or Apple Development) fixes that.
#
# Override the identity with:  NOTULA_SIGN_IDENTITY="Apple Development: …" ./tools/build_dmg.sh
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/Notula.app"
[ -d "$APP" ] || { echo "Build the app first: ./.venv/bin/python setup.py py2app"; exit 1; }

# ---- code sign (inside-out) ----
IDENTITY="${NOTULA_SIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
  IDENTITY="$(security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/{print $2; exit}')"
  [ -z "$IDENTITY" ] && IDENTITY="$(security find-identity -v -p codesigning | awk -F'"' '/Apple Development/{print $2; exit}')"
fi
if [ -n "$IDENTITY" ]; then
  echo "Signing with: $IDENTITY"
  find "$APP" -type f \( -name "*.dylib" -o -name "*.so" \) \
       -exec codesign --force --timestamp=none -s "$IDENTITY" {} +
  FW="$APP/Contents/Frameworks/Python.framework/Versions/3.13"
  [ -d "$FW" ] && codesign --force --timestamp=none -s "$IDENTITY" "$FW"
  [ -f "$APP/Contents/MacOS/python" ] && codesign --force --timestamp=none -s "$IDENTITY" "$APP/Contents/MacOS/python"
  codesign --force --deep --timestamp=none -s "$IDENTITY" "$APP"
  codesign --verify --deep --strict "$APP" && echo "signature valid ✓"
else
  echo "WARNING: no codesigning identity found — app stays ad-hoc; macOS will NOT"
  echo "         remember its Screen Recording / Microphone permission."
fi

# ---- package DMG ----
# version.py, not the plist: Info.plist can only hold dot-separated integers, so
# it has no idea this is a beta. The DMG people download should say so.
VER="$(python3 version.py 2>/dev/null \
       || defaults read "$PWD/$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null \
       || echo 1.0)"
DMG="dist/Notula-${VER}.dmg"
STAGE="dist/dmgroot"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/Notula.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Notula" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null 2>&1
rm -rf "$STAGE"

echo "created $DMG ($(du -h "$DMG" | cut -f1))"
