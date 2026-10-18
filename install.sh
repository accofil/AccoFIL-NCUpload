#!/usr/bin/env bash
# AccoFIL NCUpload – interaktív Linux-telepítő.
# Használat: bash install.sh [--install-system] [--ncup-dir /teljes/út/NCUP]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10+ szükséges. Debian/Ubuntu: sudo apt-get install python3 python3-venv python3-pip" >&2
  exit 2
fi
exec python3 -m ncore_up.installer "$@"
