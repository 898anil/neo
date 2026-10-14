#!/usr/bin/env bash
# Install or update the neo Mac app from the latest GitHub release.
#
#   curl -fsSL https://raw.githubusercontent.com/898anil/neo/main/macos/install.sh | bash
#
# Downloads the DMG for this Mac (Apple silicon or Intel), checks its SHA-256 against the one
# published with the release, copies Neo.app to /Applications and opens it.
set -euo pipefail

main() {
  local repo="${NEO_REPO:-898anil/neo}" arch url tmp mount expected actual
  case "$(uname -m)" in
    arm64) arch=arm64 ;;
    x86_64) arch=x86_64 ;;
    *) echo "Unsupported Mac: $(uname -m)"; exit 1 ;;
  esac
  [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 14 ] || { echo "neo needs macOS 14 (Sonoma) or later."; exit 1; }

  echo "==> Finding the latest neo app release"
  # Newest first; app releases are tagged app-v*.
  url="$(curl -fsSL "https://api.github.com/repos/$repo/releases?per_page=30" \
    | grep -o "https://github.com/$repo/releases/download/app-v[^\"]*/Neo-[^\"]*-$arch\.dmg" | head -1 || true)"
  [ -n "$url" ] || { echo "No neo app release for $arch at https://github.com/$repo/releases"; exit 1; }

  tmp="$(mktemp -d)"
  mount="$tmp/mount"
  trap 'hdiutil detach -quiet "'"$mount"'" 2>/dev/null || true; rm -rf "'"$tmp"'"' EXIT

  echo "==> Downloading $(basename "$url")"
  curl -fL --progress-bar -o "$tmp/Neo.dmg" "$url"
  expected="$(curl -fsSL "$url.sha256" | awk '{print $1}')"
  actual="$(shasum -a 256 "$tmp/Neo.dmg" | awk '{print $1}')"
  if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
    echo "Checksum mismatch for $(basename "$url"): expected ${expected:-none}, got $actual. Not installing."
    exit 1
  fi
  echo "    checksum ok"

  echo "==> Installing to /Applications"
  mkdir -p "$mount"
  hdiutil attach -quiet -nobrowse -readonly -mountpoint "$mount" "$tmp/Neo.dmg"
  [ -x "$mount/Neo.app/Contents/MacOS/Neo" ] || { echo "The download has no Neo.app; nothing changed."; exit 1; }
  if pgrep -qf "/Neo.app/Contents/MacOS/Neo"; then
    osascript -e 'quit app id "dev.neo.mac"' >/dev/null 2>&1 || true
    sleep 2
  fi
  # Copy next to the old app first, then swap, so a failed copy never leaves you without one.
  rm -rf "/Applications/.Neo.app.new"
  cp -R "$mount/Neo.app" "/Applications/.Neo.app.new"
  rm -rf /Applications/Neo.app
  mv "/Applications/.Neo.app.new" /Applications/Neo.app
  xattr -dr com.apple.quarantine /Applications/Neo.app 2>/dev/null || true

  echo "==> Opening neo"
  open /Applications/Neo.app
  echo "Done. neo lives in your menu bar."
}

# Everything runs from main, so a download cut off half way through does nothing.
main "$@"
