#!/usr/bin/env bash
# Web-only rollback: --taskdef <revision> [--dry-run]. Runtime rollback remains pending.
set -euo pipefail
source "$(dirname "$0")/settings.sh"
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
        -h|--help)          echo 'Usage: rollback.sh --taskdef <revision> [--dry-run]'
                            echo 'Runtime targets (--to, --runtime-version, --runtime-image) remain pending.'; exit 0 ;;
        *)                  die "unknown argument $1 (see --help)" ;;
    esac
done

[ -n "$RUNTIME_VERSION" ] && [ -n "$RUNTIME_IMAGE_TAG" ] \
    && die "--runtime-version and --runtime-image both name the backend target; pass one."

# Historical release tags include a Runtime target; publishing those remains gated.
if [ -n "$GIT_TAG" ]; then
    require_model_ready || exit 1
fi

if [ -z "$RUNTIME_VERSION" ] && [ -z "$RUNTIME_IMAGE_TAG" ] && [ -z "$TASKDEF_REVISION" ]; then
    die "nothing to do. Pass --to <git-tag>, or --runtime-version / --runtime-image / --taskdef."
fi

if [ -n "$RUNTIME_VERSION" ] || [ -n "$RUNTIME_IMAGE_TAG" ]; then
    require_model_ready || exit 1
fi
case "$TASKDEF_REVISION" in
    ''|*[!0-9]*) die '--taskdef must be a revision number' ;;
esac
source "$(dirname "$0")/config.sh"
require_creds
TARGET_TD="${TASK_FAMILY}:${TASKDEF_REVISION}"
CUR_TD=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].taskDefinition' --output text)
[ -n "$CUR_TD" ] && [ "$CUR_TD" != None ] || die "service $ECS_SERVICE not found"
validate_td() {
    aws ecs describe-task-definition --task-definition "$1" --output json \
        | python3 "$(dirname "$0")/validate_edge.py" taskdef \
            "$AWS_REGION" "$ACCOUNT_ID" "$ECR_REGISTRY/$ECR_FRONTEND" "$TASK_FAMILY"
}
TARGET_INFO=$(validate_td "$TARGET_TD")
CURRENT_INFO=$(validate_td "$CUR_TD")
TARGET_TD_IMAGE="${TARGET_INFO%%$'\n'*}"
[ "${TARGET_INFO#*$'\n'}" = "${CURRENT_INFO#*$'\n'}" ] \
    || die 'web-only rollback cannot change the Runtime ARN or qualifier while model deployment is pending'
case "$TARGET_TD_IMAGE" in
    "$ECR_REGISTRY/$ECR_FRONTEND:"*) IMAGE_SELECTOR="imageTag=${TARGET_TD_IMAGE##*:}" ;;
    "$ECR_REGISTRY/$ECR_FRONTEND@sha256:"*) IMAGE_SELECTOR="imageDigest=${TARGET_TD_IMAGE##*@}" ;;
    *) die "task definition image must belong to $ECR_REGISTRY/$ECR_FRONTEND" ;;
esac
aws ecr describe-images --repository-name "$ECR_FRONTEND" --image-ids "$IMAGE_SELECTOR" >/dev/null \
    || die "rollback image does not exist in ECR"
echo "Web rollback: ${CUR_TD##*/} -> $TARGET_TD ($TARGET_TD_IMAGE)"
bash "$(dirname "$0")/edge.sh" --check-only
if [ "$DRY_RUN" = 1 ]; then
    echo '--dry-run: target verified; nothing changed.'
    exit 0
fi
if [ "${CUR_TD##*/}" != "$TARGET_TD" ]; then
    aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
        --task-definition "$TARGET_TD" --query 'service.serviceName' --output text
    desired=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
        --query 'services[0].desiredCount' --output text)
    if [ "$desired" != 0 ]; then
        aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
            || die 'web rollback did not stabilise; inspect ECS events (service may have changed)'
    fi
fi
NEW_TD=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].taskDefinition' --output text)
[ "${NEW_TD##*/}" = "$TARGET_TD" ] || die "MISMATCH: expected $TARGET_TD, found $NEW_TD"
echo "Verified web task definition: $TARGET_TD"
ALB_DNS=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
    --query 'LoadBalancers[0].DNSName' --output text)
if [ -n "$ALB_DNS" ] && [ "$ALB_DNS" != None ]; then
    echo "CLIENT URL: $(alb_url "$ALB_DNS")"
else
    echo 'ALB missing; no public entrypoint.'
fi
