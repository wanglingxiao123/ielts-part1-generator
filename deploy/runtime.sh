#!/usr/bin/env bash
# Create or update the AgentCore Runtime that runs the generation loop.
#
#   bash deploy/runtime.sh [image-tag]
#
# Prerequisite: the backend image is in ECR (bash backend/scripts/deploy.sh ...).
# The Runtime has no public ingress; only the web tier's task role may invoke it.

source "$(dirname "$0")/config.sh"
require_creds
require_region

TAG="${1:-dev}"
IMAGE="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_BACKEND}:${TAG}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT}-runtime"

if ! aws ecr describe-images --repository-name "$ECR_BACKEND" --image-ids imageTag="$TAG" \
     >/dev/null 2>&1; then
    echo "ERROR: $IMAGE not found. Push it first:" >&2
    echo "  bash backend/scripts/deploy.sh ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_BACKEND} $TAG" >&2
    exit 1
fi

existing=$(aws bedrock-agentcore-control list-agent-runtimes \
           --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeArn" \
           --output text 2>/dev/null)

# Session lifecycle, from the documented ranges (60-28800s each, idle <= max):
#   idle 900s  -- default; a demo session left alone for 15 min should release its microVM
#   max 28800s -- 8h default. A stopped microVM does NOT end the session; the next invoke
#                 provisions a fresh one, so this is a ceiling on one instance, not on usability.
LIFECYCLE='{"idleRuntimeSessionTimeout":900,"maxLifetime":28800}'

if [ -n "$existing" ] && [ "$existing" != "None" ]; then
    id="${existing##*/}"
    echo "updating existing runtime $RUNTIME_NAME"
    aws bedrock-agentcore-control update-agent-runtime \
        --agent-runtime-id "$id" \
        --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$IMAGE\"}}" \
        --role-arn "$ROLE_ARN" \
        --network-configuration '{"networkMode":"PUBLIC"}' \
        --protocol-configuration '{"serverProtocol":"HTTP"}' \
        --lifecycle-configuration "$LIFECYCLE" \
        --environment-variables "IELTS_AUDIO_BUCKET=${S3_BUCKET},AWS_REGION=${AWS_REGION}" \
        --query 'agentRuntimeArn' --output text
else
    echo "creating runtime $RUNTIME_NAME"
    aws bedrock-agentcore-control create-agent-runtime \
        --agent-runtime-name "$RUNTIME_NAME" \
        --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$IMAGE\"}}" \
        --role-arn "$ROLE_ARN" \
        --network-configuration '{"networkMode":"PUBLIC"}' \
        --protocol-configuration '{"serverProtocol":"HTTP"}' \
        --lifecycle-configuration "$LIFECYCLE" \
        --environment-variables "IELTS_AUDIO_BUCKET=${S3_BUCKET},AWS_REGION=${AWS_REGION}" \
        --query 'agentRuntimeArn' --output text
fi

echo "waiting for READY..."
for _ in $(seq 1 60); do
    status=$(aws bedrock-agentcore-control list-agent-runtimes \
             --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].status" --output text)
    [ "$status" = "READY" ] && break
    [ "$status" = "CREATE_FAILED" ] || [ "$status" = "UPDATE_FAILED" ] && {
        echo "ERROR: runtime status $status; check CloudWatch /aws/bedrock-agentcore/runtimes/" >&2
        exit 1
    }
    sleep 10
done

arn=$(aws bedrock-agentcore-control list-agent-runtimes \
      --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeArn" --output text)
echo "READY: $arn"
echo
echo "pass this to the web tier as IELTS_RUNTIME_ARN."
