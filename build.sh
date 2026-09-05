#!/usr/bin/env bash
set -euo pipefail

STAMP="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)-${BUILD_NUMBER:-0}"
ZIP_NAME="app-${STAMP}.zip"

rm -f ./*.zip
zip -qr "${ZIP_NAME}" . -x '.git/*' -x '*.zip' -x 'dist/*' -x 'semgrep.json' -x 'gitleaks.json' -x 'trivy.json' -x 'gate-result.json'
ls -lh "${ZIP_NAME}"
