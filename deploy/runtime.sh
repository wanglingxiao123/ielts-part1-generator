#!/usr/bin/env bash
# Create or update the AgentCore Runtime that runs the generation loop.
#
#   bash deploy/runtime.sh <image-tag>
#
# Prerequisite: the backend image is in ECR (bash backend/scripts/deploy.sh ...).
# The Runtime has no public ingress; only the web tier's task role may invoke it.
#
# This is the script that switches live traffic, so the tag is required rather than defaulted. A
# default of `dev` meant an argument-less run silently repointed production at whatever `dev` happened
# to be. Rolling back means naming a known-good tag here:
#
#   bash deploy/runtime.sh known-good-20260730     # the last image before the agent-autonomy rewrite
#
# Switching Runtime *version* is not a rollback: each version records an image tag, so versions that
# name the same tag are indistinguishable.

source "$(dirname "$0")/config.sh"
require_creds
require_region

TAG="${1:?a tag is required; this switches live traffic. Use known-good-20260730 to roll back.}"
IMAGE="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_BACKEND}:${TAG}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT}-runtime"
MODEL_ID="${IELTS_MODEL_ID:-openai.gpt-5.6-luna}"
MODEL_AUTH="${IELTS_MODEL_AUTH:-bearer}"
MODEL_REGION="${IELTS_MODEL_REGION:-${AWS_REGION}}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/runtime-env.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT INT TERM
umask 077

if [ "$MODEL_AUTH" = "bearer" ] && [ -z "${AWS_BEARER_TOKEN_BEDROCK:-}" ]; then
    echo "ERROR: IELTS_MODEL_AUTH=bearer requires AWS_BEARER_TOKEN_BEDROCK in the environment." >&2
    echo "       Export it before running deploy/runtime.sh." >&2
    exit 1
fi

MODEL_ID="$MODEL_ID" MODEL_AUTH="$MODEL_AUTH" MODEL_REGION="$MODEL_REGION" \
python3 - "$WORKDIR" <<'PY'
import json, os, sys
workdir = sys.argv[1]
env = {
    "IELTS_AUDIO_BUCKET": os.environ["S3_BUCKET"],
    "AWS_REGION": os.environ["AWS_REGION"],
    "IELTS_MODEL_ID": os.environ["MODEL_ID"],
    "IELTS_MODEL_AUTH": os.environ["MODEL_AUTH"],
    "IELTS_MODEL_REGION": os.environ["MODEL_REGION"],
}
token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
if os.environ["MODEL_AUTH"] == "bearer" and token:
    env["AWS_BEARER_TOKEN_BEDROCK"] = token
with open(os.path.join(workdir, "env.json"), "w", encoding="utf-8") as fh:
    json.dump(env, fh)
PY

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
        --environment-variables "file://$WORKDIR/env.json" \
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
        --environment-variables "file://$WORKDIR/env.json" \
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
