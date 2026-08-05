#!/usr/bin/env bash
# Put production back on a known-good version, fast.
#
#   bash deploy/rollback.sh --to prod-20260801          # both tiers, from deploy/RELEASES.md
#   bash deploy/rollback.sh --runtime-version 17        # backend only
#   bash deploy/rollback.sh --taskdef 31                # web only
#   bash deploy/rollback.sh --to prod-20260801 --dry-run
#
# Why this exists rather than re-running the deploy scripts: README once said rolling back the web
# tier means `service.sh <known-good-tag>`. That is false. `service.sh` unconditionally runs
# `docker build && docker push`, and both ECR repositories are IMMUTABLE, so pushing a tag that
# already exists fails. Worse, it needs a working Docker daemon -- at the exact moment a client is
# waiting is the wrong time to discover Docker is not running.
#
# Rolling back needs neither Docker nor a build. Both artefacts already exist in AWS:
#   * the ECS task definition revision pins the old web image
#   * the AgentCore Runtime version pins the old backend image
# So this script only re-points live traffic at them.
#
# What it does NOT touch, by construction:
#   * CloudFront and the ALB -- the delivered URL is a property of the distribution, not of any
#     version, so it cannot change here. Only edge.sh/teardown.sh manage those.
#   * S3 -- no object is read or written. Materials, batches, users and audio are untouched.
#
# One risk this script cannot cover: if the version being rolled back FROM wrote data in a new
# shape (a changed `_candidates/` record, a changed material.json), the old code will read that new
# shape after the rollback. Bucket versioning does not help with an incompatible structure. Ship
# structural changes behind a new key prefix.

source "$(dirname "$0")/config.sh"
require_creds
require_region

RUNTIME_VERSION=""
TASKDEF_REVISION=""
GIT_TAG=""
DRY_RUN=0

die() { echo "ERROR: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --to)               GIT_TAG="${2:?--to needs a tag, e.g. prod-20260801}"; shift 2 ;;
        --runtime-version)  RUNTIME_VERSION="${2:?--runtime-version needs a number}"; shift 2 ;;
        --taskdef)          TASKDEF_REVISION="${2:?--taskdef needs a revision number}"; shift 2 ;;
        --dry-run)          DRY_RUN=1; shift ;;
        -h|--help)          sed -n '2,30p' "$0"; exit 0 ;;
        *)                  die "unknown argument $1 (see --help)" ;;
    esac
done

# `--to <tag>` is a convenience over the two explicit numbers. The mapping lives in
# deploy/RELEASES.md, which is the human record; the numbers are resolved from the git tag's
# annotation so the two cannot drift apart silently.
if [ -n "$GIT_TAG" ]; then
    annotation="$(git -C "$(dirname "$0")/.." tag -n99 "$GIT_TAG" 2>/dev/null)" \
        || die "git tag $GIT_TAG not found; see deploy/RELEASES.md for known anchors"
    [ -n "$annotation" ] || die "git tag $GIT_TAG not found"
    if [ -z "$RUNTIME_VERSION" ]; then
        RUNTIME_VERSION="$(printf '%s' "$annotation" | sed -n 's/.*liveVersion=\([0-9]\{1,\}\).*/\1/p' | head -1)"
    fi
    if [ -z "$TASKDEF_REVISION" ]; then
        TASKDEF_REVISION="$(printf '%s' "$annotation" | sed -n "s|.*${TASK_FAMILY}:\([0-9]\{1,\}\).*|\1|p" | head -1)"
    fi
    [ -n "$RUNTIME_VERSION" ] || die "could not read a Runtime version from tag $GIT_TAG.
       The annotation must contain 'liveVersion=<n>'. Pass --runtime-version explicitly."
    [ -n "$TASKDEF_REVISION" ] || die "could not read a taskdef revision from tag $GIT_TAG.
       The annotation must contain '${TASK_FAMILY}:<n>'. Pass --taskdef explicitly."
fi

if [ -z "$RUNTIME_VERSION" ] && [ -z "$TASKDEF_REVISION" ]; then
    die "nothing to do. Pass --to <git-tag>, or --runtime-version / --taskdef (see --help)."
fi

RUNTIME_ID=$(aws bedrock-agentcore-control list-agent-runtimes \
             --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId | [0]" \
             --output text 2>/dev/null)
[ -n "$RUNTIME_ID" ] && [ "$RUNTIME_ID" != "None" ] || die "runtime $RUNTIME_NAME not found"

# ── show the current state and the target, then verify the target exists ──────
#
# Printed before anything changes so the operator can abort. A rollback is usually run under time
# pressure, which is exactly when an unverified assumption costs the most.

echo "== current =="
CUR_VERSION=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
              --agent-runtime-id "$RUNTIME_ID" \
              --query 'runtimeEndpoints[?name==`DEFAULT`].liveVersion | [0]' --output text)
CUR_TD=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
         --query 'services[0].taskDefinition' --output text)
CUR_IMAGE=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" \
            --agent-runtime-version "$CUR_VERSION" \
            --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text)
echo "  Runtime DEFAULT -> version $CUR_VERSION  (${CUR_IMAGE##*/})"
echo "  ECS service     -> ${CUR_TD##*/}"

echo "== target =="
if [ -n "$RUNTIME_VERSION" ]; then
    # Verify the version exists AND report the image it pins. Two versions can name the same image
    # tag, in which case switching between them is a no-op -- the operator should see that rather
    # than watch a "successful" rollback change nothing.
    TARGET_IMAGE=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" \
                   --agent-runtime-version "$RUNTIME_VERSION" \
                   --query 'agentRuntimeArtifact.containerConfiguration.containerUri' \
                   --output text 2>/dev/null) \
        || die "Runtime version $RUNTIME_VERSION does not exist. Known versions:
       $(aws bedrock-agentcore-control list-agent-runtime-versions --agent-runtime-id "$RUNTIME_ID" \
         --query 'agentRuntimes[*].agentRuntimeVersion' --output text | tr '\t' ' ')"
    echo "  Runtime DEFAULT -> version $RUNTIME_VERSION  (${TARGET_IMAGE##*/})"
    if [ "$CUR_VERSION" = "$RUNTIME_VERSION" ]; then
        echo "    (already there; will skip)"
    elif [ "$CUR_IMAGE" = "$TARGET_IMAGE" ]; then
        echo "    WARNING: same image as the current version -- switching changes no code."
    fi
fi
if [ -n "$TASKDEF_REVISION" ]; then
    TARGET_TD="${TASK_FAMILY}:${TASKDEF_REVISION}"
    TARGET_TD_IMAGE=$(aws ecs describe-task-definition --task-definition "$TARGET_TD" \
                      --query 'taskDefinition.containerDefinitions[0].image' --output text 2>/dev/null) \
        || die "task definition $TARGET_TD does not exist or is deregistered"
    echo "  ECS service     -> $TARGET_TD  (${TARGET_TD_IMAGE##*/})"
    [ "${CUR_TD##*/}" = "$TARGET_TD" ] && echo "    (already there; will skip)"
fi

echo
echo "not touched: CloudFront, ALB, S3"

if [ "$DRY_RUN" = 1 ]; then
    echo
    echo "--dry-run: nothing changed."
    exit 0
fi

# ── apply ────────────────────────────────────────────────────────────────────
#
# Web first: it is the tier a client actually looks at, it is the fastest to converge (~1 min), and
# it is the one whose failure is visible as a broken page rather than a failed generation.

echo
if [ -n "$TASKDEF_REVISION" ] && [ "${CUR_TD##*/}" != "${TASK_FAMILY}:${TASKDEF_REVISION}" ]; then
    echo "== web -> ${TASK_FAMILY}:${TASKDEF_REVISION} =="
    aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
        --task-definition "${TASK_FAMILY}:${TASKDEF_REVISION}" \
        --query 'service.serviceName' --output text
    # Only wait if something is supposed to be running. `services-stable` on a desiredCount=0
    # service returns immediately, but waiting on a stopped service reads as a hang.
    desired=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
              --query 'services[0].desiredCount' --output text)
    if [ "${desired:-0}" != "0" ]; then
        echo "  waiting for the service to stabilise (usually ~1 min)..."
        aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
        echo "  stable"
    else
        echo "  desiredCount=0, nothing to roll; run deploy/start.sh when needed"
    fi
fi

if [ -n "$RUNTIME_VERSION" ] && [ "$CUR_VERSION" != "$RUNTIME_VERSION" ]; then
    echo "== backend -> Runtime version ${RUNTIME_VERSION} =="
    # Re-pointing the DEFAULT endpoint at an existing version, rather than calling
    # update-agent-runtime with the old image. Both work; this one creates no new version, so the
    # version list stays a truthful history instead of accumulating "v19 = the old v17" entries.
    aws bedrock-agentcore-control update-agent-runtime-endpoint \
        --agent-runtime-id "$RUNTIME_ID" --endpoint-name DEFAULT \
        --agent-runtime-version "$RUNTIME_VERSION" \
        --query '{live:liveVersion,target:targetVersion,status:status}' --output json
    echo "  waiting for READY (usually 1-3 min)..."
    for _ in $(seq 1 60); do
        st=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
             --agent-runtime-id "$RUNTIME_ID" \
             --query 'runtimeEndpoints[?name==`DEFAULT`].status | [0]' --output text)
        [ "$st" = "READY" ] && { echo "  READY"; break; }
        [ "$st" = "UPDATE_FAILED" ] && die "endpoint status UPDATE_FAILED.
       Fall back to re-pointing the image:
         bash deploy/runtime.sh <known-good-tag>
       Check CloudWatch /aws/bedrock-agentcore/runtimes/ for the cause."
        sleep 5
    done
fi

# ── verify, rather than assume ────────────────────────────────────────────────

echo
echo "== after =="
NEW_VERSION=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
              --agent-runtime-id "$RUNTIME_ID" \
              --query 'runtimeEndpoints[?name==`DEFAULT`].liveVersion | [0]' --output text)
NEW_TD=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
         --query 'services[0].taskDefinition' --output text)
echo "  Runtime DEFAULT -> version $NEW_VERSION"
echo "  ECS service     -> ${NEW_TD##*/}"

# The delivered URL must be the same one as before. Resolved the same way status.sh does it, so a
# rollback that somehow moved the edge would show up here.
ALB_DNS=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
          --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || true)
if [ -n "$ALB_DNS" ] && [ "$ALB_DNS" != "None" ]; then
    CF_DOMAIN=$(aws cloudfront list-distributions \
        --query "DistributionList.Items[?Origins.Items[0].DomainName=='${ALB_DNS}'].DomainName | [0]" \
        --output text 2>/dev/null || true)
    if [ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ]; then
        printf '  %-16s https://%s  -> ' "health check:" "$CF_DOMAIN"
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
               "https://${CF_DOMAIN}/healthz" || echo "no answer")
        echo "$code"
        if [ "$code" != "200" ]; then
            echo "    not 200 yet. A rolling task can take another minute; then check"
            echo "    target health and the ${LOG_GROUP} log group."
        fi
    fi
fi

cat <<NOTE

Record this in deploy/RELEASES.md -- an unrecorded rollback leaves the table claiming a version
that is no longer live.
NOTE
