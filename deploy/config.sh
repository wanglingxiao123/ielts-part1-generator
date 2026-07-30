#!/usr/bin/env bash
# Shared settings for the demo deployment. Sourced by the other scripts.
#
# Shape: CloudFront (HTTPS, stable hostname) -> ALB (HTTP, private) -> Fargate task.
#
# It used to be one Fargate task with a public ENI IP and nothing in front of it. That was right for
# a demo started and stopped around a session, and wrong for the thing this became: a URL handed to
# a client. Two reasons, both structural rather than stylistic.
#
# **The address changed on every deploy.** A task's public IP belongs to the task, so each rolling
# update handed out a new one -- five different addresses in one afternoon. A delivered URL cannot
# work that way.
#
# **Login was in plaintext.** Port 80 with no TLS anywhere means the password and the session cookie
# are readable by anything on the path. For a reviewer typing a password daily that is not a
# theoretical exposure.
#
# CloudFront terminates TLS on its own `*.cloudfront.net` certificate, so no domain and no Route53
# are needed. The ALB gives it a stable origin, and the task's security group now admits only the
# ALB -- the task has no public ingress at all.

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

# Port 80, not 8080: many corporate networks block outbound 8080, which makes a demo URL on that
# port unreachable for exactly the audience a demo is for.
export WEB_PORT=80

# Who may reach the **ALB**. The task itself no longer has public ingress: its security group admits
# only the ALB's security group (see `deploy/alb.sh`), so this is the only network-level knob.
#
# 0.0.0.0/0 here is what makes a delivered CloudFront URL work -- CloudFront fetches from the origin
# over the public internet from a large, changing set of IPs. Narrowing it to an office CIDR would
# break CloudFront, not secure it. (Restricting the origin to CloudFront specifically is possible
# with the `com.amazonaws.global.cloudfront.origin-facing` managed prefix list plus a custom header
# check; it is not done here, and the consequence is stated plainly: anyone who discovers the ALB
# hostname can bypass CloudFront and reach the app over plain HTTP. Access control remains the
# application's login plus ALLOWED_EMAIL_DOMAINS.)
export INGRESS_CIDR="${INGRESS_CIDR:-0.0.0.0/0}"

# ALB idle timeout. Must exceed the SSE heartbeat interval (`WEB_SSE_HEARTBEAT`, 15s) by a wide
# margin, or a generating batch is severed mid-stream. 120s leaves eight heartbeats of slack.
export ALB_IDLE_TIMEOUT="${ALB_IDLE_TIMEOUT:-120}"

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
