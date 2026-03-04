#!/usr/bin/env bash
set -euo pipefail

IMAGE="opendigitalsociety/agent-zero-telegram-proxy"

# Fetch the latest tag from Docker Hub
echo "Checking latest version on Docker Hub..."
TAGS_JSON=$(curl -s "https://hub.docker.com/v2/repositories/${IMAGE}/tags/?page_size=100" 2>/dev/null || echo '{}')

# Extract semver tags (vX.Y.Z or X.Y.Z), pick the highest
LATEST=$(echo "$TAGS_JSON" \
  | grep -oE '"name"\s*:\s*"v?[0-9]+\.[0-9]+\.[0-9]+"' \
  | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' \
  | sort -t. -k1,1n -k2,2n -k3,3n \
  | tail -1 || true)

if [ -z "$LATEST" ]; then
  LATEST="0.0.0"
  echo "No existing version found. Starting at 0.0.0"
else
  echo "Latest version on Docker Hub: $LATEST"
fi

# Increment patch version
MAJOR=$(echo "$LATEST" | cut -d. -f1)
MINOR=$(echo "$LATEST" | cut -d. -f2)
PATCH=$(echo "$LATEST" | cut -d. -f3)
NEW_VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))"

echo "New version: $NEW_VERSION"
echo ""

# Build
echo "Building ${IMAGE}:${NEW_VERSION} ..."
docker build -t "${IMAGE}:${NEW_VERSION}" -t "${IMAGE}:latest" .

# Push
echo ""
echo "Pushing ${IMAGE}:${NEW_VERSION} ..."
docker push "${IMAGE}:${NEW_VERSION}"

echo "Pushing ${IMAGE}:latest ..."
docker push "${IMAGE}:latest"

echo ""
echo "Done! Released ${IMAGE}:${NEW_VERSION}"
