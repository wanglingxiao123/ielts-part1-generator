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
source "$(dirname "$0")/../../deploy/config.sh"
require_creds
REGION="$AWS_REGION"
case "$REPO" in
    "$ECR_REGISTRY/"*) ;;
    *) echo "ERROR: destination repository must belong to $ECR_REGISTRY" >&2; exit 1 ;;
esac
cd "$(dirname "$0")/../.."

# Checked before the build, not at push time. The repository is IMMUTABLE, so a reused tag is
# rejected -- but that rejection would otherwise arrive after several minutes of building.
REPO_NAME="${REPO#"$ECR_REGISTRY/"}"
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
if aws ecr describe-images --repository-name "$REPO_NAME" --region "$REGION" \
       --image-ids imageTag="$TAG" > /dev/null 2>"$WORKDIR/image.err"; then
    echo "ERROR: tag '$TAG' already exists in $REPO_NAME and tags are immutable."
    echo "       Pick a new name. Existing tags:"
    aws ecr describe-images --repository-name "$REPO_NAME" --region "$REGION" \
        --query 'sort_by(imageDetails,&imagePushedAt)[*].imageTags[]' --output text 2>/dev/null \
        | tr '\t' '\n' | sed 's/^/         /'
    exit 1
elif ! grep -q '(ImageNotFoundException)' "$WORKDIR/image.err"; then
    cat "$WORKDIR/image.err" >&2
    echo 'ERROR: could not verify that the image tag is unused; refusing to build.' >&2
    exit 1
fi

echo "== building linux/arm64 =="
docker build --platform linux/arm64 -f backend/Dockerfile -t "ielts-backend:${TAG}" .

SIZE_BYTES=$(docker image inspect "ielts-backend:${TAG}" --format '{{.Size}}')
SIZE_GB=$(awk -v b="$SIZE_BYTES" 'BEGIN {printf "%.2f", b/1024/1024/1024}')
echo "image size: ${SIZE_GB} GB"
if awk -v b="$SIZE_BYTES" 'BEGIN {exit (b > 2*1024*1024*1024) ? 0 : 1}'; then
    echo "ERROR: image exceeds the 2GB limit"
    exit 1
fi

echo "== pushing =="
aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${REPO%%/*}"
docker tag "ielts-backend:${TAG}" "${REPO}:${TAG}"
docker push "${REPO}:${TAG}"

cat <<EOF

Pushed ${REPO}:${TAG} (region ${REGION}).

China runtime deployment remains pending a provider decision and implementation.
The image build does not enable model inference. deploy/runtime.sh enforces that gate.
EOF
