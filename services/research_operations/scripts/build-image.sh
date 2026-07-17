#!/bin/sh
set -eu

: "${UV_PYTHON_IMAGE:?set an immutable Python 3.12 + uv image with @sha256 digest}"
: "${OUTPUT_IMAGE:?set the local output image tag}"
case "$UV_PYTHON_IMAGE" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) exit 64 ;; esac

workspace=$(CDPATH= cd -- "$(dirname "$0")/../../.." && pwd)
docker build \
  --build-arg "UV_PYTHON_IMAGE=$UV_PYTHON_IMAGE" \
  --file "$workspace/services/research_operations/Dockerfile" \
  --tag "$OUTPUT_IMAGE" \
  "$workspace"
# A freshly built local image has no registry RepoDigest yet. The image ID is
# already an immutable sha256 content identifier and is always available.
docker image inspect --format '{{.Id}}' "$OUTPUT_IMAGE"
