#!/usr/bin/env bash
# Pure, offline China deployment contract. No AWS discovery or filesystem writes.
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-cn-northwest-1}}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PARTITION=aws-cn
export AWS_DNS_SUFFIX=amazonaws.com.cn
export ALB_PUBLIC_PORT="${ALB_PUBLIC_PORT-9000}"
export WEB_PORT=80

require_region() {
    case "$AWS_REGION" in
        cn-northwest-1|cn-north-1) ;;
        *) echo "ERROR: China-only deployment: unsupported region '$AWS_REGION'; use cn-northwest-1 or cn-north-1." >&2; return 1 ;;
    esac
}
require_ports() {
    case "$ALB_PUBLIC_PORT" in
        ''|*[!0-9]*) echo 'ERROR: ALB_PUBLIC_PORT must be an integer from 1 to 65535.' >&2; return 1 ;;
    esac
    if [ "${#ALB_PUBLIC_PORT}" -gt 5 ]; then
        echo 'ERROR: ALB_PUBLIC_PORT must be an integer from 1 to 65535.' >&2; return 1
    fi
    ALB_PUBLIC_PORT=$((10#$ALB_PUBLIC_PORT))
    if [ "$ALB_PUBLIC_PORT" -lt 1 ] || [ "$ALB_PUBLIC_PORT" -gt 65535 ]; then
        echo 'ERROR: ALB_PUBLIC_PORT must be an integer from 1 to 65535.' >&2; return 1
    fi
    case "$ALB_PUBLIC_PORT" in
        80|443|8080) echo 'ERROR: ALB_PUBLIC_PORT cannot be 80, 443 or 8080 for this POC.' >&2; return 1 ;;
    esac
    export ALB_PUBLIC_PORT
}
require_model_ready() {
    echo "ERROR: China model/runtime release is pending secret-reference publishing and image validation ($AWS_REGION)." >&2
    echo '       Model configuration cannot bypass this gate; local Kimi adapter tests do not enable production deployment.' >&2
    return 1
}
ecr_registry() {
    case "${1:-}" in
        ''|*[!0-9]*) echo 'ERROR: ACCOUNT_ID must contain exactly 12 digits.' >&2; return 1 ;;
    esac
    [ "${#1}" = 12 ] || { echo 'ERROR: ACCOUNT_ID must contain exactly 12 digits.' >&2; return 1; }
    printf '%s.dkr.ecr.%s.%s\n' "$1" "$AWS_REGION" "$AWS_DNS_SUFFIX"
}
alb_url() {
    [ -n "${1:-}" ] && [ "$1" != None ] || { echo 'ERROR: ALB missing; run deploy/edge.sh.' >&2; return 1; }
    printf 'http://%s:%s\n' "$1" "$ALB_PUBLIC_PORT"
}
require_region || return 1
require_ports || return 1
