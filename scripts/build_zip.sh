#!/usr/bin/env bash
# Build the release archives:
#   dist/kasa_tpap-<version>.zip  manual install; contains the kasa_tpap/ folder
#                                 to copy into /config/custom_components/
#   dist/kasa_tpap.zip            HACS release asset (hacs.json "filename");
#                                 integration files at the archive root
set -euo pipefail
cd "$(dirname "$0")/.."
version=$(python3 -c 'import json;print(json.load(open("custom_components/kasa_tpap/manifest.json"))["version"])')
exclude=(-x '*/__pycache__/*' '*.pyc' '*.DS_Store')
mkdir -p dist
rm -f "dist/kasa_tpap-${version}.zip" dist/kasa_tpap.zip dist/*.sha256

(cd custom_components && zip -qrX "../dist/kasa_tpap-${version}.zip" kasa_tpap "${exclude[@]}")
(cd custom_components/kasa_tpap && zip -qrX ../../dist/kasa_tpap.zip . "${exclude[@]}")

cd dist
for f in "kasa_tpap-${version}.zip" kasa_tpap.zip; do
  shasum -a 256 "$f" > "$f.sha256"
  cat "$f.sha256"
done
