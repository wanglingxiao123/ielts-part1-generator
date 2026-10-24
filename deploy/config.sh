#!/usr/bin/env bash
# Discovered deployment configuration: public ALB -> Fargate, China regions only.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/settings.sh"
require_region

# Discovered from the caller's own credentials rather than hardcoded, so this repo carries no
# account identity. Override by exporting ACCOUNT_ID if you need a different target.
if [ -z "${ACCOUNT_ID:-}" ]; then
    ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
fi
export ACCOUNT_ID
ECR_REGISTRY="$(ecr_registry "$ACCOUNT_ID")"
export ECR_REGISTRY

# Optional. Leave unset to use the default credential chain (env vars, instance role, or the
# default profile). Set it only if your credentials live in a named profile.
if [ -n "${AWS_PROFILE:-}" ]; then export AWS_PROFILE; fi

# Who may register through the web login. Default to the client domain rather than to open
# registration; override before running any deploy script if a different allow-list is needed.
export ALLOWED_EMAIL_DOMAINS="${ALLOWED_EMAIL_DOMAINS:-example.com}"

export PROJECT="ielts-part1"
export S3_BUCKET="${S3_BUCKET:-ielts-part1-materials-${ACCOUNT_ID}}"

export ECR_BACKEND="${PROJECT}-backend"
export ECR_FRONTEND="${PROJECT}-frontend"
export ECS_CLUSTER="${PROJECT}"
export ECS_SERVICE="${PROJECT}-web"
export TASK_FAMILY="${PROJECT}-web"
export LOG_GROUP="/ecs/${PROJECT}-web"
export SG_NAME="${PROJECT}-web-sg"
export ALB_SG_NAME="${PROJECT}-alb-sg"
export ALB_NAME="${PROJECT}-alb"
export TG_NAME="${PROJECT}-tg"
export RUNTIME_NAME="ielts_part1_runtime"

# Defaults to a public subnet in the account's default VPC, resolved at run time. Override both
# to deploy into a specific network.
if [ -z "${VPC_ID:-}" ]; then
    VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
              --query 'Vpcs[0].VpcId' --output text 2>/dev/null || true)"
fi
export VPC_ID
# Two subnets in different AZs, because an ALB requires at least two. `SUBNET_IDS` is the
# space-separated list for the ALB; `SUBNET_ID` stays the single one the task runs in, so nothing
# that already used it has to change.
if [ -z "${SUBNET_IDS:-}" ] && [ -n "${VPC_ID:-}" ] && [ "$VPC_ID" != "None" ]; then
    SUBNET_IDS="$(aws ec2 describe-subnets \
        --filters "Name=vpc-id,Values=${VPC_ID}" Name=map-public-ip-on-launch,Values=true \
        --query 'Subnets[*].SubnetId' --output text 2>/dev/null | tr '\t' ' ' || true)"
fi
export SUBNET_IDS
if [ -z "${SUBNET_ID:-}" ]; then
    SUBNET_ID="$(echo "${SUBNET_IDS:-}" | awk '{print $1}')"
fi
export SUBNET_ID

# Only the ALB may admit clients. Backend ingress is granted by edge.sh.
export INGRESS_CIDR="${INGRESS_CIDR:-0.0.0.0/0}"

# Must exceed the web SSE heartbeat interval.
export ALB_IDLE_TIMEOUT="${ALB_IDLE_TIMEOUT:-120}"
export TASK_CPU=512
export TASK_MEMORY=1024
export WEB_FANOUT_CONCURRENCY="${WEB_FANOUT_CONCURRENCY:-20}"

require_creds() {
    local actual
    actual="$(aws sts get-caller-identity --query Account --output text)" || {
        echo 'ERROR: AWS credentials are expired or absent.' >&2; return 1;
    }
    if [ "$actual" != "$ACCOUNT_ID" ]; then
        echo "ERROR: authenticated as account $actual, expected $ACCOUNT_ID; refusing mutations." >&2
        return 1
    fi
}
