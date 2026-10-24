#!/usr/bin/env bash
# Where is everything, and what is the demo URL right now?
#
# Reports the stable public ALB entrypoint.
#
#   bash deploy/status.sh

source "$(dirname "$0")/config.sh"
require_creds

echo "account $ACCOUNT_ID / region $AWS_REGION"
echo

printf '%-22s ' "S3 bucket:"
if aws s3api head-bucket --bucket "$S3_BUCKET" >/dev/null 2>&1; then
    n=$(aws s3 ls "s3://$S3_BUCKET/" --recursive 2>/dev/null | wc -l | tr -d ' ')
    echo "$S3_BUCKET ($n objects)"
else
    echo "MISSING"
fi

for repo in "$ECR_BACKEND" "$ECR_FRONTEND"; do
    printf '%-22s ' "ECR $repo:"
    if aws ecr describe-repositories --repository-names "$repo" >/dev/null 2>&1; then
        tags=$(aws ecr list-images --repository-name "$repo" \
               --query 'imageIds[].imageTag' --output text 2>/dev/null | tr '\t' ' ')
        echo "exists [${tags:-no tags}]"
    else
        echo "MISSING"
    fi
done

printf '%-22s ' "AgentCore runtime:"
echo "PENDING: China model/runtime provider decision"

printf '%-22s ' "ECS cluster:"
aws ecs describe-clusters --clusters "$ECS_CLUSTER" \
    --query 'clusters[0].status' --output text 2>/dev/null || echo MISSING

printf '%-22s ' "Web service:"
desired=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
          --query 'services[0].desiredCount' --output text 2>/dev/null || echo -)
running=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
          --query 'services[0].runningCount' --output text 2>/dev/null || echo -)
echo "desired=$desired running=$running"

ALB_DNS=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
    --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || true)
if [ -n "$ALB_DNS" ] && [ "$ALB_DNS" != None ]; then
    echo "CLIENT URL: $(alb_url "$ALB_DNS")"
    if [ "${running:-0}" = 0 ]; then echo "Web task stopped; run deploy/start.sh."; fi
else
    echo "ALB missing; no public entrypoint. Run deploy/edge.sh."
fi
