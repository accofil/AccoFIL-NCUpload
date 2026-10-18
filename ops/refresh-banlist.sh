#!/bin/sh
# AccoFIL NCUpload: kizárólag a tiltólista frissítése.
set -eu
exec "$HOME/.local/bin/ncupload" --refresh-banlist --json
