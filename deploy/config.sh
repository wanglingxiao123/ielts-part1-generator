#!/usr/bin/env bash
# Shared settings for the demo deployment. Sourced by the other scripts.
#
# Shape: a single Fargate task with a public ENI IP. No ALB, no EIP, no Route53, no CloudFront --
# for a demo that is started before a session and stopped afterwards, they add cost without
# adding anything the task's own public IP does not already provide.

set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"

# Discovered from the caller's own credentials rather than hardcoded, so this repo carries no
# account identity. Override by exporting ACCOUNT_ID if you need a different target.
if [ -z "${ACCOUNT_ID:-}" ]; then
    ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
fi
export ACCOUNT_ID

# Optional. Leave unset to use the default credential chain (env vars, instance role, or the
# default profile). Set it only if your credentials live in a named profile.
if [ -n "${AWS_PROFILE:-}" ]; then export AWS_PROFILE; fi

export PROJECT="ielts-part1"
export S3_BUCKET="${S3_BUCKET:-ielts-part1-materials-${ACCOUNT_ID}}"

export ECR_BACKEND="${PROJECT}-backend"
export ECR_FRONTEND="${PROJECT}-frontend"
export ECS_CLUSTER="${PROJECT}"
export ECS_SERVICE="${PROJECT}-web"
export TASK_FAMILY="${PROJECT}-web"
export LOG_GROUP="/ecs/${PROJECT}-web"
export SG_NAME="${PROJECT}-web-sg"
export RUNTIME_NAME="ielts_part1_runtime"

# Defaults to a public subnet in the account's default VPC, resolved at run time. Override both
# to deploy into a specific network.
if [ -z "${VPC_ID:-}" ]; then
    VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
              --query 'Vpcs[0].VpcId' --output text 2>/dev/null || true)"
fi
export VPC_ID
if [ -z "${SUBNET_ID:-}" ] && [ -n "${VPC_ID:-}" ] && [ "$VPC_ID" != "None" ]; then
    SUBNET_ID="$(aws ec2 describe-subnets \
        --filters "Name=vpc-id,Values=${VPC_ID}" Name=map-public-ip-on-launch,Values=true \
        --query 'Subnets[0].SubnetId' --output text 2>/dev/null || true)"
fi
export SUBNET_ID

# Port 80, not 8080: many corporate networks block outbound 8080, which makes a demo URL on that
# port unreachable for exactly the audience a demo is for.
export WEB_PORT=80

# Demo exposure. 0.0.0.0/0 is deliberate for a short-lived demo: the service is stopped between
# sessions, so the exposure window is minutes rather than months. Access control is the
# application's own login plus ALLOWED_EMAIL_DOMAINS, not the network.
# It is NOT appropriate for a long-lived deployment: anyone with the IP can spend model and Polly
# budget. Narrow this to an office egress CIDR if the service is ever left running.
export INGRESS_CIDR="${INGRESS_CIDR:-0.0.0.0/0}"

# 0.5 vCPU / 1 GB: the web tier only proxies and serves static files.
export TASK_CPU=512
export TASK_MEMORY=1024

require_creds() {
    if ! aws sts get-caller-identity --query Account --output text >/dev/null 2>&1; then
        echo "ERROR: AWS credentials are expired or absent." >&2
        echo "       Configure them (env vars, a named profile, or an instance role) and re-run." >&2
        exit 1
    fi
    local actual
    actual="$(aws sts get-caller-identity --query Account --output text)"
    if [ -n "${ACCOUNT_ID:-}" ] && [ "$actual" != "$ACCOUNT_ID" ]; then
        echo "ERROR: authenticated as account $actual, expected $ACCOUNT_ID." >&2
        echo "       Refusing to create resources in an unexpected account." >&2
        exit 1
    fi
}

require_region() {
    # GPT-5.6 has no cross-region inference; a Runtime elsewhere cannot reach the model at all.
    case "$AWS_REGION" in
        us-east-1|us-east-2) ;;
        *) echo "ERROR: $AWS_REGION cannot serve openai.gpt-5.6-*; use us-east-1 or us-east-2" >&2
           exit 1 ;;
    esac
}
