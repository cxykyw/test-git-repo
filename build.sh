#!/usr/bin/env bash
set -euo pipefail

STAMP="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)-${BUILD_NUMBER:-0}"
ZIP_NAME="app-${STAMP}.zip"

rm -f "${ZIP_NAME}"
zip -qr "${ZIP_NAME}" . -x '.git/*' -x '*.zip' -x 'dist/*'
ls -lh "${ZIP_NAME}"
