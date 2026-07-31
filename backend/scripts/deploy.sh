#!/usr/bin/env bash
# Build and push the ARM64 image, then report what still needs doing by hand.
# Run from the repository root: bash backend/scripts/deploy.sh <ecr-repo-uri> <tag>
#
# The tag is REQUIRED, and it used to default to `dev`. That default made rollback quietly
# impossible: the repository's tags are immutable now, but before that a second push moved `dev` onto
# the new image, and the Runtime records a *tag* rather than a digest -- so every one of the 15
# recorded Runtime versions pointed at `:dev` and "roll back to the previous version" would have
# returned the same broken image. Recovering meant finding an untagged digest by push timestamp,
# which is not a thing to learn during an incident.
set -euo pipefail

REPO="${1:?usage: deploy.sh <ecr-repo-uri> <tag>}"
TAG="${2:?a tag is required; name this build (e.g. agent-autonomy, 20260731-audit-fixes). Do not reuse an existing tag: the repository is IMMUTABLE and the push would be rejected.}"
REGION="${IELTS_MODEL_REGION:-${AWS_REGION:-us-east-1}}"

# GPT-5.6 has no cross-region inference, so a Runtime in the wrong region cannot reach the model
# at all. Checked here rather than discovered as a confusing 4xx after deployment.
case "$REGION" in
    us-east-1|us-east-2) ;;
    *) echo "ERROR: region $REGION cannot serve openai.gpt-5.6-*; use us-east-1 or us-east-2"
       exit 1 ;;
esac

cd "$(dirname "$0")/../.."

# Checked before the build, not at push time. The repository is IMMUTABLE, so a reused tag is
# rejected -- but that rejection would otherwise arrive after several minutes of building.
REPO_NAME="${REPO##*/}"
if aws ecr describe-images --repository-name "$REPO_NAME" --region "$REGION" \
       --image-ids imageTag="$TAG" >/dev/null 2>&1; then
    echo "ERROR: tag '$TAG' already exists in $REPO_NAME and tags are immutable."
    echo "       Pick a new name. Existing tags:"
    aws ecr describe-images --repository-name "$REPO_NAME" --region "$REGION" \
        --query 'sort_by(imageDetails,&imagePushedAt)[*].imageTags[]' --output text 2>/dev/null \
        | tr '\t' '\n' | sed 's/^/         /'
    exit 1
fi

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
  1. Point the AgentCore Runtime at this tag, in ${REGION}:
       bash deploy/runtime.sh ${TAG}
     Rollback is pointing it back at a known-good TAG, and only works because tags are immutable
     and this build did not touch any existing one. Note what rollback is NOT: switching Runtime
     version. Every version records a tag, so several of them name the same tag and differ in
     nothing that matters.
       bash deploy/runtime.sh known-good-20260730     # the pre-refactor image
     That image predates the agent-autonomy rewrite, so rolling back gives up agent self-execution
     and the blind-audit isolation with it. It is an escape hatch, not an A/B switch.
  2. Runtime needs bedrock:InvokeModel plus permission to mint bearer tokens, since
     IELTS_MODEL_AUTH defaults to mantle and Strands signs with the task role.
  3. Calibrate against real timings (backend/docs/timing.md). ONE invocation now carries ONE
     material -- the web tier fans a batch out into N of them (web/fanout.py) -- so what has to
     fit inside 15 minutes is a single material, not a batch. Time one material end to end; if
     it does not finish, the fix is the model call count, not the batch size, and never the
     revise or re-audit stages, which would make the quality loop decorative.
  4. Then check throughput rather than batch size: submit ~12 sets and watch for 429s. On
     throttling, lower WEB_FANOUT_CONCURRENCY on the web tier (default 6). There is no
     max_batch to lower any more, deliberately.
  5. While a batch runs, poll /ping; it must answer Healthy within 1s throughout. This matters
     more than it used to: N concurrent invocations mean N warm microVMs, each with its own
     health check.
EOF
