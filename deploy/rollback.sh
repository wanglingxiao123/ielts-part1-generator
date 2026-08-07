#!/usr/bin/env bash
# Put production back on a known-good version, fast.
#
#   bash deploy/rollback.sh --to prod-20260801          # both tiers, from deploy/RELEASES.md
#   bash deploy/rollback.sh --runtime-version 17        # backend only
#   bash deploy/rollback.sh --runtime-image two-states-20260801   # backend, by image tag
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
# THE TARGET IS AN IMAGE, NOT A VERSION NUMBER. This script used to re-point the DEFAULT endpoint
# with `update-agent-runtime-endpoint --agent-runtime-version`, on the reasoning that it creates no
# new version and so keeps the version list a truthful history. Measured on 2026-08-07, mid-rollback:
#
#   ConflictException: Default endpoints are managed through agent updates.
#                      Please use the update agent operation.
#
# AWS does not permit that call on a DEFAULT endpoint at all -- the parameter exists, but not for
# this endpoint. The only route is `update-agent-runtime`, which necessarily mints a NEW version
# carrying the old image, so v19 can be "the old v17". Two consequences, both load-bearing below:
#   * a version number no longer implies a code version, so every check here compares `containerUri`;
#   * `--runtime-version N` means "the image and config that version N pins", never "make N live".
#
# ORDER: Runtime first, then web. The web tier is what a client looks at and is the faster of the
# two, which argued for doing it first -- but the Runtime step is the one that failed, and failing
# second left production on the old web bundle talking to the new backend. That half-rolled-back
# state is worse than either end. Runtime first means its failure aborts before anything has moved;
# if the web step then fails, the Runtime is put back where it was (see `compensate`).
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
RUNTIME_IMAGE_TAG=""
TASKDEF_REVISION=""
GIT_TAG=""
DRY_RUN=0

die() { echo "ERROR: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --to)               GIT_TAG="${2:?--to needs a tag, e.g. prod-20260801}"; shift 2 ;;
        --runtime-version)  RUNTIME_VERSION="${2:?--runtime-version needs a number}"; shift 2 ;;
        --runtime-image)    RUNTIME_IMAGE_TAG="${2:?--runtime-image needs an ECR image tag}"; shift 2 ;;
        --taskdef)          TASKDEF_REVISION="${2:?--taskdef needs a revision number}"; shift 2 ;;
        --dry-run)          DRY_RUN=1; shift ;;
        -h|--help)          sed -n '2,49p' "$0"; exit 0 ;;
        *)                  die "unknown argument $1 (see --help)" ;;
    esac
done

[ -n "$RUNTIME_VERSION" ] && [ -n "$RUNTIME_IMAGE_TAG" ] \
    && die "--runtime-version and --runtime-image both name the backend target; pass one."

# `--to <tag>` is a convenience over the explicit numbers. The mapping lives in deploy/RELEASES.md,
# which is the human record; the numbers are resolved from the git tag's annotation so the two cannot
# drift apart silently.
if [ -n "$GIT_TAG" ]; then
    annotation="$(git -C "$(dirname "$0")/.." tag -n99 "$GIT_TAG" 2>/dev/null)" \
        || die "git tag $GIT_TAG not found; see deploy/RELEASES.md for known anchors"
    [ -n "$annotation" ] || die "git tag $GIT_TAG not found"
    if [ -z "$RUNTIME_VERSION" ] && [ -z "$RUNTIME_IMAGE_TAG" ]; then
        RUNTIME_VERSION="$(printf '%s' "$annotation" | sed -n 's/.*liveVersion=\([0-9]\{1,\}\).*/\1/p' | head -1)"
    fi
    if [ -z "$TASKDEF_REVISION" ]; then
        TASKDEF_REVISION="$(printf '%s' "$annotation" | sed -n "s|.*${TASK_FAMILY}:\([0-9]\{1,\}\).*|\1|p" | head -1)"
    fi
    [ -n "$RUNTIME_VERSION" ] || [ -n "$RUNTIME_IMAGE_TAG" ] || die "could not read a Runtime version from tag $GIT_TAG.
       The annotation must contain 'liveVersion=<n>'. Pass --runtime-version or --runtime-image."
    [ -n "$TASKDEF_REVISION" ] || die "could not read a taskdef revision from tag $GIT_TAG.
       The annotation must contain '${TASK_FAMILY}:<n>'. Pass --taskdef explicitly."
fi

if [ -z "$RUNTIME_VERSION" ] && [ -z "$RUNTIME_IMAGE_TAG" ] && [ -z "$TASKDEF_REVISION" ]; then
    die "nothing to do. Pass --to <git-tag>, or --runtime-version / --runtime-image / --taskdef."
fi

RUNTIME_ID=$(aws bedrock-agentcore-control list-agent-runtimes \
             --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId | [0]" \
             --output text 2>/dev/null)
[ -n "$RUNTIME_ID" ] && [ "$RUNTIME_ID" != "None" ] || die "runtime $RUNTIME_NAME not found"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Build the `update-agent-runtime` request for a given existing version by REPLAYING that version's
# whole configuration, rather than re-stating the flags here. Restoring a version means restoring
# what it was, and the field list is not ours to curate: the drill on 2026-08-07 was run by hand with
# five flags copied off `get-agent-runtime`, and any field neither the operator nor this script
# thought to copy would have been silently reset to its default. Passing the recorded config through
# means a field added by a future API version rolls back correctly without an edit here.
#
# `--runtime-image` overrides only the containerUri, keeping the live version's config, since an
# image tag says nothing about configuration.
runtime_input() {   # <version> [override-image] -> path to a --cli-input-json file
    local version="$1" override="${2:-}" out="$WORK/runtime-input-$1${2:+-override}.json"
    aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "$RUNTIME_ID" --agent-runtime-version "$version" --output json \
        > "$WORK/version-$version.json" 2>"$WORK/get-$version.err" \
        || return 1
    RUNTIME_ID="$RUNTIME_ID" OVERRIDE="$override" python3 - "$WORK/version-$version.json" "$out" <<'PY'
import json, os, sys

recorded = json.load(open(sys.argv[1], encoding="utf-8"))
# Exactly the keys update-agent-runtime accepts, minus the ones it derives itself. Read-only fields
# (status, timestamps, arn, workloadIdentityDetails, agentRuntimeVersion) are rejected as unknown
# parameters, so they are dropped by omission rather than by a blocklist -- a new read-only field
# added upstream must not start failing the call.
PASSTHROUGH = ("agentRuntimeArtifact", "roleArn", "networkConfiguration", "protocolConfiguration",
               "lifecycleConfiguration", "environmentVariables", "metadataConfiguration",
               "authorizerConfiguration", "requestHeaderConfiguration", "filesystemConfigurations",
               "description")
payload = {"agentRuntimeId": os.environ["RUNTIME_ID"]}
for key in PASSTHROUGH:
    if key in recorded:
        payload[key] = recorded[key]

override = os.environ.get("OVERRIDE") or ""
if override:
    payload.setdefault("agentRuntimeArtifact", {}).setdefault("containerConfiguration", {})
    payload["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] = override

uri = payload.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri")
if not uri:
    sys.exit("recorded version carries no containerUri; cannot roll back to it")
json.dump(payload, open(sys.argv[2], "w", encoding="utf-8"))
print(uri)
PY
}

image_of_version() {
    aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" \
        --agent-runtime-version "$1" \
        --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text 2>/dev/null
}

ecr_has() {   # <repo> <full image uri>
    aws ecr describe-images --repository-name "$1" --image-ids imageTag="${2##*:}" >/dev/null 2>&1
}

# ── show the current state and the target, then pre-flight every artefact ─────
#
# Printed before anything changes so the operator can abort, and every existence check happens here
# rather than at the point of use. A rollback runs under time pressure, which is exactly when an
# unverified assumption costs the most -- and a half-applied rollback costs more than a refused one.

echo "== current =="
CUR_VERSION=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
              --agent-runtime-id "$RUNTIME_ID" \
              --query 'runtimeEndpoints[?name==`DEFAULT`].liveVersion | [0]' --output text)
CUR_TD=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
         --query 'services[0].taskDefinition' --output text)
CUR_IMAGE=$(image_of_version "$CUR_VERSION")
echo "  Runtime DEFAULT -> v$CUR_VERSION  (${CUR_IMAGE##*/})"
echo "  ECS service     -> ${CUR_TD##*/}"

echo "== target =="
TARGET_IMAGE=""
if [ -n "$RUNTIME_VERSION" ] || [ -n "$RUNTIME_IMAGE_TAG" ]; then
    if [ -n "$RUNTIME_IMAGE_TAG" ]; then
        # Config comes from the live version, image from the argument.
        TARGET_IMAGE="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_BACKEND}:${RUNTIME_IMAGE_TAG}"
        runtime_input "$CUR_VERSION" "$TARGET_IMAGE" >/dev/null \
            || die "could not read the live Runtime config (v$CUR_VERSION) to build the request:
       $(cat "$WORK/get-$CUR_VERSION.err" 2>/dev/null)"
        RUNTIME_INPUT="$WORK/runtime-input-${CUR_VERSION}-override.json"
        echo "  Runtime image   -> ${TARGET_IMAGE##*/}  (config kept from v$CUR_VERSION)"
    else
        TARGET_IMAGE="$(runtime_input "$RUNTIME_VERSION")" \
            || die "Runtime version $RUNTIME_VERSION does not exist, or its config could not be read. Known versions:
       $(aws bedrock-agentcore-control list-agent-runtime-versions --agent-runtime-id "$RUNTIME_ID" \
         --query 'agentRuntimes[*].agentRuntimeVersion' --output text | tr '\t' ' ')"
        RUNTIME_INPUT="$WORK/runtime-input-${RUNTIME_VERSION}.json"
        echo "  Runtime image   -> ${TARGET_IMAGE##*/}  (recorded by v$RUNTIME_VERSION)"
    fi

    # The image the old version names must still be IN ECR. A version whose image was expired by a
    # lifecycle policy still lists, still reads back, and still accepts an update -- the runtime then
    # fails to pull and there is nothing to roll back to. Refuse now, while production is intact.
    ecr_has "$ECR_BACKEND" "$TARGET_IMAGE" \
        || die "${TARGET_IMAGE##*/} is not in ECR repository $ECR_BACKEND.
       The version records it, but the image is gone (expired or deleted), so this rollback would
       leave the Runtime unable to pull. Pick another anchor from deploy/RELEASES.md."

    if [ "$CUR_IMAGE" = "$TARGET_IMAGE" ]; then
        echo "    (already on this image; will skip -- a new version number would change no code)"
    fi
fi
if [ -n "$TASKDEF_REVISION" ]; then
    TARGET_TD="${TASK_FAMILY}:${TASKDEF_REVISION}"
    read -r TD_STATUS TARGET_TD_IMAGE <<<"$(aws ecs describe-task-definition --task-definition "$TARGET_TD" \
        --query 'taskDefinition.[status,containerDefinitions[0].image]' --output text 2>/dev/null)" \
        || die "task definition $TARGET_TD does not exist"
    [ -n "${TD_STATUS:-}" ] || die "task definition $TARGET_TD does not exist"
    # A deregistered revision reads back fine and cannot start a task. `update-service` accepts it,
    # then the service never stabilises -- a failure that shows up minutes later, downstream.
    [ "$TD_STATUS" = "ACTIVE" ] \
        || die "task definition $TARGET_TD is $TD_STATUS, not ACTIVE; ECS cannot launch tasks from it."
    ecr_has "$ECR_FRONTEND" "$TARGET_TD_IMAGE" \
        || die "${TARGET_TD_IMAGE##*/} is not in ECR repository $ECR_FRONTEND.
       $TARGET_TD points at an image that no longer exists, so its tasks would fail to pull."
    echo "  ECS service     -> $TARGET_TD  (${TARGET_TD_IMAGE##*/})"
    [ "${CUR_TD##*/}" = "$TARGET_TD" ] && echo "    (already there; will skip)"
fi

echo
echo "not touched: CloudFront, ALB, S3"

if [ "$DRY_RUN" = 1 ]; then
    echo
    echo "--dry-run: nothing changed. Every artefact above was verified to exist and be usable."
    exit 0
fi

# The escape route out of a half-applied rollback, prepared while nothing has moved. Building it
# afterwards would mean reading the live config during a failure, which is the least reliable moment
# to depend on another API call succeeding.
PRE_IMAGE="$CUR_IMAGE"
PRE_INPUT=""
if [ -n "$TARGET_IMAGE" ] && [ -n "$TASKDEF_REVISION" ] && [ "$CUR_IMAGE" != "$TARGET_IMAGE" ]; then
    if runtime_input "$CUR_VERSION" >/dev/null 2>&1; then
        PRE_INPUT="$WORK/runtime-input-${CUR_VERSION}.json"
    else
        echo
        echo "WARNING: could not record the current Runtime config (v$CUR_VERSION), so a failed web"
        echo "         step cannot be compensated automatically. Continuing; if the web step fails,"
        echo "         restore the backend by hand with:"
        echo "           bash deploy/rollback.sh --runtime-image ${PRE_IMAGE##*:}"
    fi
fi

apply_runtime_input() {   # <cli-input-json path> -> applies it and waits for READY
    aws bedrock-agentcore-control update-agent-runtime \
        --cli-input-json "file://$1" --query 'agentRuntimeVersion' --output text \
        || return 1
    echo "  waiting for READY (usually ~1 min)..."
    local st
    for _ in $(seq 1 60); do
        st=$(aws bedrock-agentcore-control list-agent-runtimes \
             --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].status" --output text)
        case "$st" in
            READY) echo "  READY"; return 0 ;;
            UPDATE_FAILED|CREATE_FAILED) echo "  status $st" >&2; return 1 ;;
        esac
        sleep 5
    done
    echo "  timed out waiting for READY (last status ${st:-unknown})" >&2
    return 1
}

compensate() {
    echo
    echo "== compensating: putting the backend back on ${PRE_IMAGE##*/} =="
    if [ -z "$PRE_INPUT" ]; then
        echo "  no recorded config to replay. Restore by hand:" >&2
        echo "    bash deploy/rollback.sh --runtime-image ${PRE_IMAGE##*:}" >&2
        return 1
    fi
    if apply_runtime_input "$PRE_INPUT"; then
        echo "  backend is back on ${PRE_IMAGE##*/}; both tiers are as they were before this run."
        return 0
    fi
    echo "  COMPENSATION FAILED. Production is split: the backend is rolled back and the web tier" >&2
    echo "  is not. Restore the backend with:" >&2
    echo "    bash deploy/rollback.sh --runtime-image ${PRE_IMAGE##*:}" >&2
    return 1
}

# ── apply: Runtime first, so its failure costs nothing ───────────────────────

echo
RUNTIME_APPLIED=0
if [ -n "$TARGET_IMAGE" ] && [ "$CUR_IMAGE" != "$TARGET_IMAGE" ]; then
    echo "== backend -> ${TARGET_IMAGE##*/} =="
    # `update-agent-runtime`, not `update-agent-runtime-endpoint`: see the header. This mints a new
    # version number carrying the old image, and that is the only route AWS offers for DEFAULT.
    apply_runtime_input "$RUNTIME_INPUT" || die "the Runtime update failed.
       NOTHING HAS CHANGED: this is the first step, so production is still on ${CUR_IMAGE##*/} with
       ${CUR_TD##*/}. Check CloudWatch /aws/bedrock-agentcore/runtimes/ for the cause."
    RUNTIME_APPLIED=1
fi

if [ -n "$TASKDEF_REVISION" ] && [ "${CUR_TD##*/}" != "${TASK_FAMILY}:${TASKDEF_REVISION}" ]; then
    echo "== web -> ${TASK_FAMILY}:${TASKDEF_REVISION} =="
    web_ok=1
    aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
        --task-definition "${TASK_FAMILY}:${TASKDEF_REVISION}" \
        --query 'service.serviceName' --output text || web_ok=0
    if [ "$web_ok" = 1 ]; then
        # Only wait if something is supposed to be running. `services-stable` on a desiredCount=0
        # service returns immediately, but waiting on a stopped service reads as a hang.
        desired=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
                  --query 'services[0].desiredCount' --output text)
        if [ "${desired:-0}" != "0" ]; then
            echo "  waiting for the service to stabilise (usually ~1 min)..."
            aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" || web_ok=0
            [ "$web_ok" = 1 ] && echo "  stable"
        else
            echo "  desiredCount=0, nothing to roll; run deploy/start.sh when needed"
        fi
    fi
    if [ "$web_ok" != 1 ]; then
        echo "  the web rollback failed." >&2
        if [ "$RUNTIME_APPLIED" = 1 ]; then
            compensate || exit 1
            die "web rollback failed; the backend was put back, so production is unchanged.
       Check the ${LOG_GROUP} log group and ECS service events, then re-run."
        fi
        die "web rollback failed and the backend was not touched; production is unchanged.
       Check the ${LOG_GROUP} log group and ECS service events."
    fi
fi

# ── verify by artefact, not by version number ────────────────────────────────
#
# The check is `containerUri` and the taskdef revision, never the Runtime version number: the number
# necessarily changed, and after 2026-08-07 it no longer implies which code is live. `/healthz` alone
# cannot distinguish "rolled back" from "nothing happened", so it is reported but decides nothing.

echo
echo "== after =="
NEW_VERSION=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
              --agent-runtime-id "$RUNTIME_ID" \
              --query 'runtimeEndpoints[?name==`DEFAULT`].liveVersion | [0]' --output text)
NEW_IMAGE=$(image_of_version "$NEW_VERSION")
NEW_TD=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
         --query 'services[0].taskDefinition' --output text)
echo "  Runtime DEFAULT -> v$NEW_VERSION  (${NEW_IMAGE##*/})"
echo "  ECS service     -> ${NEW_TD##*/}"

VERIFY_FAILED=0
if [ -n "$TARGET_IMAGE" ]; then
    if [ "$NEW_IMAGE" = "$TARGET_IMAGE" ]; then
        echo "  verified: the DEFAULT endpoint serves ${TARGET_IMAGE##*/}"
    else
        echo "  MISMATCH: expected ${TARGET_IMAGE##*/}, DEFAULT serves ${NEW_IMAGE##*/}" >&2
        VERIFY_FAILED=1
    fi
fi
if [ -n "$TASKDEF_REVISION" ]; then
    if [ "${NEW_TD##*/}" = "${TASK_FAMILY}:${TASKDEF_REVISION}" ]; then
        echo "  verified: the service runs ${TASK_FAMILY}:${TASKDEF_REVISION}"
    else
        echo "  MISMATCH: expected ${TASK_FAMILY}:${TASKDEF_REVISION}, service runs ${NEW_TD##*/}" >&2
        VERIFY_FAILED=1
    fi
fi

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
        echo "    (a 200 only says something is answering. What proves the rollback are the two"
        echo "     artefact lines above, plus a marker string from the withdrawn build being absent"
        echo "     from the served bundle.)"
    fi
fi

if [ "$VERIFY_FAILED" = 1 ]; then
    die "the rollback did not land as intended -- see the MISMATCH lines above.
       Production may be serving a mix. Re-check with deploy/status.sh before doing anything else."
fi

cat <<NOTE

Record this in deploy/RELEASES.md -- an unrecorded rollback leaves the table claiming a version
that is no longer live. Record the IMAGE TAG, not just the version number: the Runtime version
number is now allocated by the rollback itself and says nothing about which code is live.
NOTE
