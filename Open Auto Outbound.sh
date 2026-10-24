#!/bin/sh
# Resolve the adjacent application even when opened from another directory.
set -eu
release_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "$(uname -s)" in
  Darwin) platform=macos ;;
  Linux) platform=linux ;;
  *) printf '%s\n' 'LAUNCH-PLATFORM: Use the Windows command file on Windows.' >&2; exit 1 ;;
esac
case "$(uname -m)" in
  arm64|aarch64) architecture=arm64 ;;
  x86_64|amd64) architecture=x64 ;;
  *) printf '%s\n' 'LAUNCH-PLATFORM: This computer needs an x64 or ARM64 processor.' >&2; exit 1 ;;
esac
launcher="$release_root/app/launcher/bin/$platform-$architecture/launcher"
if [ ! -x "$launcher" ]; then
  printf '%s\n' 'LAUNCH-ROOT: Extract the complete desktop ZIP, then open this file again.' 'Source checkout: build the desktop launchers with scripts/build_release.py --script-launchers.' >&2
  exit 1
fi
export GOOSNAV_RELEASE_ROOT="$release_root"
exec "$launcher"
