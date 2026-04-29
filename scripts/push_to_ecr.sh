#!/usr/bin/env bash
set -euo pipefail

# Build + push runner/worker images (Dockerfiles) to ECR.
#
# Requirements:
# - awscli configured (AWS_PROFILE/AWS_REGION or env vars)
# - docker running
#
# Usage:
#   ./scripts/push_to_ecr.sh \
#     --region ap-south-1 \
#     --account-id 123456789012 \
#     --runner-repo invorto-ai-runner \
#     --worker-repo invorto-ai-worker \
#     --tag latest \
#     --platform linux/amd64
#
# Options:
#   --platform   Build platform(s). Default: linux/amd64
#   --multi-arch Build and push a multi-arch manifest (linux/amd64,linux/arm64)

REGION=""
ACCOUNT_ID=""
RUNNER_REPO=""
WORKER_REPO=""
TAG="latest"
PLATFORM="linux/arm64"
MULTI_ARCH="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --account-id) ACCOUNT_ID="$2"; shift 2 ;;
    --runner-repo) RUNNER_REPO="$2"; shift 2 ;;
    --worker-repo) WORKER_REPO="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --multi-arch) MULTI_ARCH="true"; shift 1 ;;
    -h|--help)
      sed -n '1,120p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$REGION" || -z "$ACCOUNT_ID" || -z "$RUNNER_REPO" || -z "$WORKER_REPO" ]]; then
  echo "Missing required args. Run with --help." >&2
  exit 1
fi

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr describe-repositories --region "$REGION" --repository-names "$RUNNER_REPO" >/dev/null 2>&1 || \
  aws ecr create-repository --region "$REGION" --repository-name "$RUNNER_REPO" >/dev/null

aws ecr describe-repositories --region "$REGION" --repository-names "$WORKER_REPO" >/dev/null 2>&1 || \
  aws ecr create-repository --region "$REGION" --repository-name "$WORKER_REPO" >/dev/null

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

RUNNER_IMAGE="${REGISTRY}/${RUNNER_REPO}:${TAG}"
WORKER_IMAGE="${REGISTRY}/${WORKER_REPO}:${TAG}"

if [[ "$MULTI_ARCH" == "true" ]]; then
  PLATFORM="linux/amd64,linux/arm64"
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "docker buildx is required (Docker BuildKit). Please upgrade Docker." >&2
  exit 1
fi

# Ensure we have a buildx builder available
if ! docker buildx inspect >/dev/null 2>&1; then
  docker buildx create --use >/dev/null
fi

echo "Building + pushing runner image: ${RUNNER_IMAGE} (platform=${PLATFORM})"
docker buildx build --platform "$PLATFORM" -f Dockerfile.runner -t "$RUNNER_IMAGE" --push .

echo "Building + pushing worker image: ${WORKER_IMAGE} (platform=${PLATFORM})"
docker buildx build --platform "$PLATFORM" -f Dockerfile.worker -t "$WORKER_IMAGE" --push .

echo "Done."

