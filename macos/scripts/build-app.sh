#!/usr/bin/env bash
# Build Neo.app and a DMG: the Swift app, a standalone Python with neo's dependencies, and neo's code.
#
#   macos/scripts/build-app.sh                 # this Mac's architecture
#   ARCH=x86_64 macos/scripts/build-app.sh     # Intel build (on Apple silicon)
#   SIGN_IDENTITY="Developer ID Application: …" macos/scripts/build-app.sh   # signed (default: ad-hoc)
#
# Needs Xcode (or the Command Line Tools) and uv. Output: macos/dist/Neo.app and macos/dist/Neo-<version>-<arch>.dmg
set -euo pipefail

MACOS="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$MACOS/.." && pwd)"
ARCH="${ARCH:-$(uname -m)}"
PY_VERSION="${PY_VERSION:-3.12}"
VERSION="${VERSION:-$(git -C "$ROOT" describe --tags --match 'app-v*' 2>/dev/null | sed 's/^app-v//' || true)}"
VERSION="${VERSION:-0.1.0}"
# CFBundle versions must be dot-separated numbers: 1.2.0-3-gabc -> 1.2.0 (short) and 1.2.0.3 (build).
SHORT_VERSION="$(printf '%s' "${VERSION%%-*}" | tr -cd '0-9.')"
BUILD_VERSION="$SHORT_VERSION$(printf '%s' "$VERSION" | sed -nE 's/^[^-]+-([0-9]+)-g.*/.\1/p')"
DIST="$MACOS/dist"
APP="$DIST/Neo.app"
WORK="$MACOS/.build/bundle-$ARCH"
say() { printf '\n==> %s\n' "$*"; }

case "$ARCH" in
  arm64) UV_ARCH=aarch64 ;;
  x86_64) UV_ARCH=x86_64 ;;
  *) echo "ARCH must be arm64 or x86_64"; exit 1 ;;
esac
command -v uv >/dev/null || { echo "uv is required: brew install uv"; exit 1; }

say "Swift app ($ARCH)"
swift build -c release --arch "$ARCH" --package-path "$MACOS"
BIN="$(swift build -c release --arch "$ARCH" --package-path "$MACOS" --show-bin-path)/Neo"

rm -rf "$APP" "$WORK"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$WORK"
cp "$BIN" "$APP/Contents/MacOS/Neo"
sed -e "s/__VERSION__/$BUILD_VERSION/" -e "s/__SHORT_VERSION__/$SHORT_VERSION/" "$MACOS/Info.plist" > "$APP/Contents/Info.plist"

say "Icon"
swift "$MACOS/scripts/make_icon.swift" "$WORK/Neo.iconset" >/dev/null
iconutil -c icns "$WORK/Neo.iconset" -o "$APP/Contents/Resources/Neo.icns"

say "Python $PY_VERSION ($ARCH, standalone)"
PY_REQUEST="cpython-$PY_VERSION-macos-$UV_ARCH-none"
uv python install --no-bin "$PY_REQUEST" >/dev/null
# Located by folder, not `uv python find`: that runs the interpreter, which fails cross-architecture.
PY_HOME="$(ls -d "$(uv python dir)"/cpython-"$PY_VERSION".*-macos-"$UV_ARCH"-none | sort -V | tail -1)"
[ -x "$PY_HOME/bin/python$PY_VERSION" ] || { echo "Python $PY_VERSION for $ARCH not found under $(uv python dir)"; exit 1; }
PY="$APP/Contents/Resources/python"
cp -R "$PY_HOME" "$PY"
# Trim what neo never uses (tests, IDLE, Tk, headers): roughly halves the size.
rm -rf "$PY/include" "$PY/share" "$PY/lib/tcl"* "$PY/lib/tk"* "$PY/lib/itcl"* "$PY/lib/thread"* "$PY/lib/libtcl"* "$PY/lib/libtk"* \
       "$PY/lib/python$PY_VERSION/test" "$PY/lib/python$PY_VERSION/idlelib" "$PY/lib/python$PY_VERSION/tkinter" \
       "$PY/lib/python$PY_VERSION/turtledemo" "$PY/lib/python$PY_VERSION/ensurepip" \
       "$PY/lib/python$PY_VERSION/lib-dynload/_tkinter"* "$PY/lib/python$PY_VERSION/EXTERNALLY-MANAGED"
find "$PY" -name '__pycache__' -type d -prune -exec rm -rf {} +

say "neo's Python dependencies"
# Wheels only: a source build would compile for this Mac's architecture, not the target's.
uv pip install --quiet --only-binary :all: --python-platform "$UV_ARCH-apple-darwin" --python-version "$PY_VERSION" \
  --target "$PY/lib/python$PY_VERSION/site-packages" -r "$MACOS/requirements.txt"

say "neo"
CODE="$APP/Contents/Resources/neo"
mkdir -p "$CODE"
# Only what runs; never runtime state, secrets or private notes (they are gitignored, and not copied).
for item in app.py commands.py sessions.py chief.py cos prompts LICENSE; do
  cp -R "$ROOT/$item" "$CODE/"
done
cp "$ROOT/.env.example" "$CODE/env.example"
find "$CODE" -name '__pycache__' -type d -prune -exec rm -rf {} +

say "Every binary is $ARCH"
WRONG=0
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q 'Mach-O' && ! lipo -archs "$f" 2>/dev/null | tr ' ' '\n' | grep -qx "$ARCH"; then
    echo "  wrong architecture: $f ($(lipo -archs "$f" 2>/dev/null))"; WRONG=1
  fi
done < <(find "$APP" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0)
[ "$WRONG" = 0 ] || exit 1
echo "  ok"

say "Smoke test: bundled Python imports neo"
if [ "$ARCH" = "$(uname -m)" ]; then
  (cd "$CODE" && PYTHONDONTWRITEBYTECODE=1 COS_HOME="$WORK/state" "$PY/bin/python3" -c \
    'import cos.server, cos.runner, slack_bolt, aiohttp, dotenv; print("  ok")')
else
  echo "  skipped (cross-architecture build)"
fi

say "Sign"
IDENTITY="${SIGN_IDENTITY:--}"
# Inside out: every Mach-O in Python first, then the app.
find "$PY" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 | xargs -0 codesign --force --timestamp=none -s "$IDENTITY" 2>&1 | { grep -v 'replacing existing signature' || true; }
codesign --force --timestamp=none -s "$IDENTITY" "$PY/bin/python$PY_VERSION" 2>&1 | { grep -v 'replacing existing signature' || true; }
if [ "$IDENTITY" = "-" ]; then
  codesign --force -s - "$APP" 2>&1 | { grep -v 'replacing existing signature' || true; }
else
  codesign --force --options runtime --timestamp --entitlements "$MACOS/Neo.entitlements" -s "$IDENTITY" "$APP"
fi
codesign --verify --deep --strict "$APP"
codesign --verify --strict "$PY/bin/python$PY_VERSION"
echo "  signature ok ($IDENTITY)"
# Notarization (Developer ID) also needs --options runtime on the Python binaries; not done yet.

say "DMG"
DMG="$DIST/Neo-$VERSION-$ARCH.dmg"
STAGE="$WORK/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "Neo" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
(cd "$DIST" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
echo "  $DMG ($(du -h "$DMG" | cut -f1))"
echo "  app: $(du -sh "$APP" | cut -f1)"
