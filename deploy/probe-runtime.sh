#!/usr/bin/env bash
# Create, inspect and delete the THROWAWAY timing-probe Runtime (task 08-06-stage0).
#
#   bash deploy/probe-runtime.sh create <image-tag>
#   bash deploy/probe-runtime.sh status
#   bash deploy/probe-runtime.sh delete
#
# Why a separate Runtime instead of an extra action on the production one: `deploy/runtime.sh`
# updates the existing Runtime IN PLACE, which mints a new version and moves the DEFAULT endpoint
# onto it. Probing that way would put debug code in front of live traffic for the 35 minutes the
# measurements take. This creates its own Runtime, and `delete` removes it.
#
# Three subcommands rather than one run-and-clean script, deliberately. If a measurement comes out
# unexpected -- probe A not being cut off, say -- the next step is a longer sleep and another try, and
# an automatic delete would have thrown away a Runtime that takes minutes to recreate and reach READY.
# The cost is that `delete` is manual, so `create` prints the exact command and `status` says out
# loud that the Runtime is still alive.
#
# IAM: reuses the production runtime's execution role, `${PROJECT}-runtime`. That role's trust policy
# already covers any agentcore resource in this account and region, so nothing needs changing. It also
# carries Bedrock and S3 permissions the probe never uses -- and that is fine rather than sloppy: the
# probe process makes no AWS calls at all, so a permission it never invokes grants nothing. A
# dedicated empty role would be marginally tidier and would add a second thing to remember to delete.
source "$(dirname "$0")/config.sh"

PROBE_RUNTIME_NAME="${PROBE_RUNTIME_NAME:-ielts_part1_probe}"

# The one irreversible mistake available here is `delete` against the production Runtime, so it is
# blocked before anything else runs -- including before credentials are checked, so that even a
# misconfigured shell cannot reach the AWS calls below. This guards the environment variable, not a
# typo in the argument list: PROBE_RUNTIME_NAME is overridable, and overriding it to the production
# name has no undo.
if [ "$PROBE_RUNTIME_NAME" = "$RUNTIME_NAME" ]; then
    echo "ERROR: PROBE_RUNTIME_NAME is '$PROBE_RUNTIME_NAME', which is the PRODUCTION runtime." >&2
    echo "       This script creates and DELETES throwaway runtimes. Refusing." >&2
    exit 1
fi

require_creds
require_region

CMD="${1:-}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT}-runtime"

# `|| true` because config.sh sets `-e`: without it, an AWS call that fails for an unrelated reason
# (expired token mid-run, a throttle) would abort the script at an assignment, and during the DELETE
# path that reads as "already gone" to whoever is watching the terminal.
probe_arn() {
    aws bedrock-agentcore-control list-agent-runtimes \
        --query "agentRuntimes[?agentRuntimeName=='$PROBE_RUNTIME_NAME'].agentRuntimeArn" \
        --output text 2>/dev/null || true
}

probe_status() {
    aws bedrock-agentcore-control list-agent-runtimes \
        --query "agentRuntimes[?agentRuntimeName=='$PROBE_RUNTIME_NAME'].status" \
        --output text 2>/dev/null || true
}

case "$CMD" in
create)
    TAG="${2:?usage: probe-runtime.sh create <image-tag>; build it with backend/probe.Dockerfile}"
    IMAGE="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_BACKEND}:${TAG}"

    if ! aws ecr describe-images --repository-name "$ECR_BACKEND" --image-ids imageTag="$TAG" \
         >/dev/null 2>&1; then
        echo "ERROR: $IMAGE not found. Build and push the probe image first:" >&2
        echo "  docker build --platform linux/arm64 -f backend/probe.Dockerfile -t ielts-probe:$TAG ." >&2
        echo "  aws ecr get-login-password | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" >&2
        echo "  docker tag ielts-probe:$TAG $IMAGE && docker push $IMAGE" >&2
        exit 1
    fi

    # No update branch, on purpose. `deploy/runtime.sh` updates in place because it manages a
    # long-lived Runtime; this one is disposable, and an in-place update would silently reuse a
    # Runtime whose configuration nobody re-read. Delete and recreate instead.
    existing="$(probe_arn)"
    if [ -n "$existing" ] && [ "$existing" != "None" ]; then
        echo "ERROR: $PROBE_RUNTIME_NAME already exists: $existing" >&2
        echo "       Delete it first: bash deploy/probe-runtime.sh delete" >&2
        exit 1
    fi

    # Same lifecycle values as production. The probe is measuring the INVOCATION limits, which are
    # separate quotas (L-3ED45A13 / L-C91AC63F) from these session-lifecycle parameters; matching
    # production keeps the lifecycle side from becoming an unmeasured difference between the probe and
    # the thing it is standing in for.
    echo "creating probe runtime $PROBE_RUNTIME_NAME from $IMAGE"
    aws bedrock-agentcore-control create-agent-runtime \
        --agent-runtime-name "$PROBE_RUNTIME_NAME" \
        --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$IMAGE\"}}" \
        --role-arn "$ROLE_ARN" \
        --network-configuration '{"networkMode":"PUBLIC"}' \
        --protocol-configuration '{"serverProtocol":"HTTP"}' \
        --lifecycle-configuration '{"idleRuntimeSessionTimeout":900,"maxLifetime":28800}' \
        --query 'agentRuntimeArn' --output text

    echo "waiting for READY..."
    for _ in $(seq 1 60); do
        status="$(probe_status)"
        [ "$status" = "READY" ] && break
        if [ "$status" = "CREATE_FAILED" ] || [ "$status" = "UPDATE_FAILED" ]; then
            echo "ERROR: probe runtime status $status; see CloudWatch /aws/bedrock-agentcore/runtimes/" >&2
            exit 1
        fi
        sleep 10
    done

    arn="$(probe_arn)"
    echo "READY: $arn"
    cat <<EOF

Measure, then DELETE. Probe A takes ~15-17 min, probe B ~20 min:
  .venv-backend/bin/python backend/scripts/probe_runtime_timing.py --action probe_sync   --arn $arn
  .venv-backend/bin/python backend/scripts/probe_runtime_timing.py --action probe_stream --arn $arn

  bash deploy/probe-runtime.sh delete
EOF
    ;;

status)
    arn="$(probe_arn)"
    if [ -z "$arn" ] || [ "$arn" = "None" ]; then
        echo "$PROBE_RUNTIME_NAME does not exist (nothing to clean up)."
    else
        echo "$PROBE_RUNTIME_NAME is STILL ALIVE: $arn ($(probe_status))"
        echo "It costs nothing idle, but it is an invocable endpoint running debug code."
        echo "  bash deploy/probe-runtime.sh delete"
    fi
    # Printed on every status call: the whole reason this script exists is that production must be
    # untouched, and "I believe it was untouched" is not the same as having looked.
    echo
    echo "production, for comparison (must be unchanged by any of this):"
    aws bedrock-agentcore-control list-agent-runtimes \
        --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].[agentRuntimeName,agentRuntimeId,status]" \
        --output text
    ;;

delete)
    arn="$(probe_arn)"
    if [ -z "$arn" ] || [ "$arn" = "None" ]; then
        echo "$PROBE_RUNTIME_NAME does not exist; nothing to delete."
        exit 0
    fi
    id="${arn##*/}"
    echo "deleting $PROBE_RUNTIME_NAME ($id)"
    aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$id" \
        --query 'status' --output text
    echo "gone. Verify:  bash deploy/probe-runtime.sh status"
    ;;

*)
    echo "usage: bash deploy/probe-runtime.sh {create <image-tag>|status|delete}" >&2
    exit 1
    ;;
esac
