#!/usr/bin/env bash
# Build and push the ARM64 image, then report what still needs doing by hand.
# Run from the repository root: bash backend/scripts/deploy.sh <ecr-repo-uri> [tag]
set -euo pipefail

REPO="${1:?usage: deploy.sh <ecr-repo-uri> [tag]}"
TAG="${2:-dev}"
REGION="${IELTS_MODEL_REGION:-${AWS_REGION:-us-east-1}}"

# GPT-5.6 has no cross-region inference, so a Runtime in the wrong region cannot reach the model
# at all. Checked here rather than discovered as a confusing 4xx after deployment.
case "$REGION" in
    us-east-1|us-east-2) ;;
    *) echo "ERROR: region $REGION cannot serve openai.gpt-5.6-*; use us-east-1 or us-east-2"
       exit 1 ;;
esac

cd "$(dirname "$0")/../.."

echo "== building linux/arm64 =="
docker build --platform linux/arm64 -f backend/Dockerfile -t "ielts-backend:${TAG}" .

SIZE_BYTES=$(docker image inspect "ielts-backend:${TAG}" --format '{{.Size}}')
SIZE_GB=$(awk -v b="$SIZE_BYTES" 'BEGIN {printf "%.2f", b/1024/1024/1024}')
echo "image size: ${SIZE_GB} GB"
awk -v b="$SIZE_BYTES" 'BEGIN {exit (b > 2*1024*1024*1024) ? 0 : 1}' \
    && { echo "ERROR: image exceeds the 2GB limit"; exit 1; } || true

echo "== pushing =="
aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${REPO%%/*}"
docker tag "ielts-backend:${TAG}" "${REPO}:${TAG}"
docker push "${REPO}:${TAG}"

cat <<EOF

Pushed ${REPO}:${TAG} (region ${REGION}).

Remaining steps, and what to watch:
  1. Create or update the AgentCore Runtime pointing at this image tag, in ${REGION}.
     Rollback is a tag switch: keep the previous tag in place.
  2. Runtime needs bedrock:InvokeModel plus permission to mint bearer tokens, since
     IELTS_MODEL_AUTH defaults to mantle and Strands signs with the task role.
  3. Calibrate against real timings (backend/docs/timing.md): run 1, then 3, then 6
     materials. If 6 does not finish inside 15 minutes, lower max_batch in
     config/scenarios.yaml -- do not remove the revise or re-audit stages, which would make
     the quality loop decorative.
  4. While a batch runs, poll /ping; it must answer Healthy within 1s throughout.
EOF
