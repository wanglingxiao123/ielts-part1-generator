#!/usr/bin/env bash
# Where is everything, and what is the demo URL right now?
#
# The task's public IP changes on every start, so this is the script to run before telling
# anyone the address. Same problem the previous project solved with a boto3 ENI lookup.
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
rt=$(aws bedrock-agentcore-control list-agent-runtimes \
     --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].[agentRuntimeArn,status]" \
     --output text 2>/dev/null)
echo "${rt:-MISSING}"

printf '%-22s ' "ECS cluster:"
aws ecs describe-clusters --clusters "$ECS_CLUSTER" \
    --query 'clusters[0].status' --output text 2>/dev/null || echo MISSING

printf '%-22s ' "Web service:"
desired=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
          --query 'services[0].desiredCount' --output text 2>/dev/null || echo -)
running=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
          --query 'services[0].runningCount' --output text 2>/dev/null || echo -)
echo "desired=$desired running=$running"

# ── the edge ──────────────────────────────────────────────────────────────────
#
# The delivered address is CloudFront's, and it is stable across deploys. The task's own public IP is
# still printed when there is no ALB, because the direct-IP shape still works — it just hands out a
# new address every rolling update, which is why the edge exists.
ALB_DNS=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
          --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || true)
CF_DOMAIN=""
CF_STATUS=""
if [ -n "$ALB_DNS" ] && [ "$ALB_DNS" != "None" ]; then
    printf '%-22s ' "ALB:"
    tg_health=$(aws elbv2 describe-target-health \
                --target-group-arn "$(aws elbv2 describe-target-groups --names "$TG_NAME" \
                    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null)" \
                --query 'TargetHealthDescriptions[*].TargetHealth.State' --output text 2>/dev/null || true)
    echo "$ALB_DNS  targets=[${tg_health:-none}]"

    CF_ID=$(aws cloudfront list-distributions \
            --query "DistributionList.Items[?Origins.Items[0].DomainName=='${ALB_DNS}'].Id | [0]" \
            --output text 2>/dev/null || true)
    if [ -n "$CF_ID" ] && [ "$CF_ID" != "None" ]; then
        CF_DOMAIN=$(aws cloudfront get-distribution --id "$CF_ID" \
                    --query 'Distribution.DomainName' --output text 2>/dev/null)
        CF_STATUS=$(aws cloudfront get-distribution --id "$CF_ID" \
                    --query 'Distribution.Status' --output text 2>/dev/null)
        printf '%-22s ' "CloudFront:"
        echo "$CF_DOMAIN  ($CF_STATUS)"
    fi
fi

echo
if [ -n "$CF_DOMAIN" ]; then
    echo "  CLIENT URL:  https://${CF_DOMAIN}"
    if [ "$CF_STATUS" != "Deployed" ]; then
        echo "  (distribution is ${CF_STATUS} — a fresh one takes ~5-10 min to propagate)"
    fi
    echo "  (HTTPS end to end for the viewer; CloudFront -> ALB is plain HTTP inside AWS)"
    if [ "${running:-0}" = "0" ] || [ "${running:--}" = "-" ]; then
        echo "  (the task is stopped, so the URL will 503 — run 'bash deploy/start.sh')"
    fi
    # The ALB is reachable directly over plain HTTP by anyone who learns its hostname. Stated rather
    # than implied: it is the one hole this shape leaves, and it is a deliberate trade (locking the
    # origin to CloudFront needs a managed prefix list plus a secret header).
    echo "  (the ALB hostname above also answers directly, over plain HTTP)"
elif [ "${running:-0}" != "0" ] && [ "${running:--}" != "-" ]; then
    task=$(aws ecs list-tasks --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
           --query 'taskArns[0]' --output text 2>/dev/null)
    eni=$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$task" \
          --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" \
          --output text 2>/dev/null)
    ip=$(aws ec2 describe-network-interfaces --network-interface-ids "$eni" \
         --query 'NetworkInterfaces[0].Association.PublicIp' --output text 2>/dev/null)
    echo "  DEMO URL:  http://${ip}"
    echo "  (no ALB yet, so this address changes on every deploy — run deploy/edge.sh)"
    echo "  (login required; plain HTTP, so credentials are unencrypted in transit)"
else
    echo "  service is stopped; run 'bash deploy/start.sh'"
fi
