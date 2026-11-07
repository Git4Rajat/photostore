#!/usr/bin/env sh
# Regenerate the hash-locked requirement closures for the production images
# (Python 3.11, linux x86_64). Requires `uv`. Run from backend/.
set -eu
cd "$(dirname "$0")/.."
OPTS="--python-version 3.11 --python-platform x86_64-manylinux_2_28 --generate-hashes --no-header --quiet"
uv pip compile $OPTS requirements.in requirements-clustering.in -o requirements.txt
uv pip compile $OPTS --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match \
  requirements.in requirements-ipworker.in -o requirements-ipworker.txt
