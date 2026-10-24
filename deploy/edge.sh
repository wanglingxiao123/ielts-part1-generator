#!/usr/bin/env bash
# China POC entrypoint: public HTTP ALB:9000 -> ALB-only web task:80.
# Existing extra SG ingress/listeners/routing rules are conflicts, never deleted implicitly.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
require_creds
case "${1:-}" in
    ''|--check-only) ;;
    *) echo 'Usage: edge.sh [--check-only]' >&2; exit 1 ;;
esac
validate() { python3 deploy/validate_edge.py "$@"; }
validate cidr "$INGRESS_CIDR"
[ -n "${VPC_ID:-}" ] && [ "$VPC_ID" != None ] || { echo 'ERROR: VPC_ID is required.' >&2; exit 1; }
read -r -a subnets <<<"${SUBNET_IDS:-}"
[ "${#subnets[@]}" -ge 2 ] || { echo 'ERROR: SUBNET_IDS needs at least two public subnets in different AZs.' >&2; exit 1; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# Missing named resources are normal; authorization/network errors must never mean absent.
lookup_named() {
    local kind="$1" name="$2" key="$3" code="$4"
    if aws elbv2 "describe-$kind" --names "$name" --output json >"$WORK/$kind.json" 2>"$WORK/$kind.err"; then
        cat "$WORK/$kind.json"
    elif grep -q "($code)" "$WORK/$kind.err"; then
        printf '{"%s":[]}\n' "$key"
    else
        cat "$WORK/$kind.err" >&2
        return 1
    fi
}
# Preflight every existing resource before changing any ingress or listener.
ALB_SG_INFO=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$ALB_SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
    --output json | validate sg alb "$ALB_PUBLIC_PORT" "$INGRESS_CIDR" "$VPC_ID")
ALB_SG=${ALB_SG_INFO%% *}
TASK_SG_INFO=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
    --output json | validate sg task "$WEB_PORT" "${ALB_SG:-not-created}" "$VPC_ID")
TASK_SG=${TASK_SG_INFO%% *}
TG_ARN=$(lookup_named target-groups "$TG_NAME" TargetGroups TargetGroupNotFound | validate tg "$VPC_ID" "$WEB_PORT")
ALB_ARN=$(lookup_named load-balancers "$ALB_NAME" LoadBalancers LoadBalancerNotFound | validate alb "$VPC_ID" "${ALB_SG:-not-created}")
LISTENER_ARN=""
if [ -n "$ALB_ARN" ]; then
    LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --output json \
        | validate listeners "$ALB_PUBLIC_PORT" "${TG_ARN:-not-created}")
    if [ -n "$LISTENER_ARN" ]; then
        aws elbv2 describe-rules --listener-arn "$LISTENER_ARN" --output json | validate rules
    fi
fi
if [ "${1:-}" = --check-only ]; then
    if [ -z "$TG_ARN" ] || [ -z "$ALB_ARN" ] || [ -z "$LISTENER_ARN" ] \
        || [ "${ALB_SG_INFO##* }" != present ] || [ "${TASK_SG_INFO##* }" != present ]; then
        echo 'ERROR: edge is incomplete; run deploy/edge.sh before starting or rolling back web.' >&2
        exit 1
    fi
    aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --output json \
        | validate service "$TASK_SG" "$TG_ARN"
    echo 'Existing ALB and security groups verified.'
    exit 0
fi
if [ -z "$ALB_SG" ]; then
    ALB_SG=$(aws ec2 create-security-group --group-name "$ALB_SG_NAME" \
        --description 'IELTS public ALB' --vpc-id "$VPC_ID" --query GroupId --output text)
fi
if [ "${ALB_SG_INFO##* }" != present ]; then
    aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" \
        --protocol tcp --port "$ALB_PUBLIC_PORT" --cidr "$INGRESS_CIDR" >/dev/null
fi
if [ -z "$TASK_SG" ]; then
    TASK_SG=$(aws ec2 create-security-group --group-name "$SG_NAME" \
        --description 'IELTS web task (ALB only)' --vpc-id "$VPC_ID" --query GroupId --output text)
fi
if [ "${TASK_SG_INFO##* }" != present ]; then
    aws ec2 authorize-security-group-ingress --group-id "$TASK_SG" \
        --protocol tcp --port "$WEB_PORT" --source-group "$ALB_SG" >/dev/null
fi
if [ -z "$TG_ARN" ]; then
    TG_ARN=$(aws elbv2 create-target-group --name "$TG_NAME" --protocol HTTP \
        --port "$WEB_PORT" --vpc-id "$VPC_ID" --target-type ip --health-check-path /healthz \
        --health-check-interval-seconds 30 --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
        --query 'TargetGroups[0].TargetGroupArn' --output text)
fi
aws elbv2 modify-target-group --target-group-arn "$TG_ARN" \
    --health-check-protocol HTTP --health-check-port traffic-port --health-check-path /healthz \
    --matcher HttpCode=200 --health-check-interval-seconds 30 \
    --healthy-threshold-count 2 --unhealthy-threshold-count 3 >/dev/null
aws elbv2 modify-target-group-attributes --target-group-arn "$TG_ARN" \
    --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null
if [ -z "$ALB_ARN" ]; then
    ALB_ARN=$(aws elbv2 create-load-balancer --name "$ALB_NAME" --subnets "${subnets[@]}" \
        --security-groups "$ALB_SG" --scheme internet-facing --type application \
        --query 'LoadBalancers[0].LoadBalancerArn' --output text)
    aws elbv2 wait load-balancer-available --load-balancer-arns "$ALB_ARN"
fi
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$ALB_ARN" \
    --attributes "Key=idle_timeout.timeout_seconds,Value=${ALB_IDLE_TIMEOUT}" >/dev/null
if [ -z "$LISTENER_ARN" ]; then
    aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" --protocol HTTP \
        --port "$ALB_PUBLIC_PORT" --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" >/dev/null
fi
ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
    --query 'LoadBalancers[0].DNSName' --output text)
echo "CLIENT URL: $(alb_url "$ALB_DNS")"
echo "ALB tcp/$ALB_PUBLIC_PORT from $INGRESS_CIDR -> web tcp/$WEB_PORT from $ALB_SG only."
