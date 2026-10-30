#!/usr/bin/env bash
# Build agent/dist/agent.zip for AgentCore Runtime direct code deployment:
# Linux arm64 wheels for Python 3.13 plus app.py at the zip root.
#
#   ./agent/package.sh && tofu apply
#
# Uses uv when installed (as in the AgentCore docs), otherwise pip.
set -euo pipefail
cd "$(dirname "$0")"

BUILD=build
rm -rf "$BUILD" dist
mkdir -p "$BUILD" dist

if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python-platform aarch64-manylinux2014 --python-version 3.13 \
    --only-binary=:all: --target "$BUILD" -r requirements.txt
else
  python3 -m pip install --quiet --disable-pip-version-check --platform manylinux2014_aarch64 \
    --implementation cp --python-version 3.13 --only-binary=:all: --target "$BUILD" -r requirements.txt
fi

cp app.py "$BUILD/"
# Bytecode built on this machine may not match the runtime; AgentCore needs 644/755.
find "$BUILD" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$BUILD" -type d -exec chmod 755 {} +
find "$BUILD" -type f -exec chmod 644 {} +

(cd "$BUILD" && zip -qr -X ../dist/agent.zip .)
rm -rf "$BUILD"

SIZE=$(wc -c < dist/agent.zip | tr -d ' ')
echo "dist/agent.zip: $((SIZE / 1024 / 1024)) MB (limit 250 MB zipped, 750 MB unzipped)"
