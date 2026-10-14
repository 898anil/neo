#!/usr/bin/env bash
# Install or update the neo Mac app from the latest GitHub release.
#
#   curl -fsSL https://raw.githubusercontent.com/898anil/neo/main/macos/install.sh | bash
#
# Downloads the DMG for this Mac (Apple silicon or Intel), copies Neo.app to /Applications and opens it.
set -euo pipefail

REPO="${NEO_REPO:-898anil/neo}"
case "$(uname -m)" in
  arm64) ARCH=arm64 ;;
  x86_64) ARCH=x86_64 ;;
  *) echo "Unsupported Mac: $(uname -m)"; exit 1 ;;
esac
[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 14 ] || { echo "neo needs macOS 14 (Sonoma) or later."; exit 1; }

echo "==> Finding the latest neo release"
URL="$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=20" \
  | grep -o "https://[^\"]*/Neo-[^\"]*-$ARCH\.dmg" | head -1)"
[ -n "$URL" ] || { echo "No neo app release for $ARCH found at https://github.com/$REPO/releases"; exit 1; }

TMP="$(mktemp -d)"
MOUNT="$TMP/mount"
cleanup() { hdiutil detach -quiet "$MOUNT" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

echo "==> Downloading $(basename "$URL")"
curl -fL --progress-bar -o "$TMP/Neo.dmg" "$URL"

echo "==> Installing to /Applications"
mkdir -p "$MOUNT"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MOUNT" "$TMP/Neo.dmg"
if pgrep -xq Neo; then osascript -e 'quit app "Neo"' >/dev/null 2>&1 || true; sleep 2; fi
rm -rf /Applications/Neo.app
cp -R "$MOUNT/Neo.app" /Applications/
xattr -dr com.apple.quarantine /Applications/Neo.app 2>/dev/null || true

echo "==> Opening neo"
open /Applications/Neo.app
echo "Done. neo lives in your menu bar."
