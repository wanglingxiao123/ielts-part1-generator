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

if [ "${running:-0}" != "0" ] && [ "${running:--}" != "-" ]; then
    task=$(aws ecs list-tasks --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
           --query 'taskArns[0]' --output text 2>/dev/null)
    eni=$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$task" \
          --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" \
          --output text 2>/dev/null)
    ip=$(aws ec2 describe-network-interfaces --network-interface-ids "$eni" \
         --query 'NetworkInterfaces[0].Association.PublicIp' --output text 2>/dev/null)
    echo
    echo "  DEMO URL:  http://${ip}"
    # Was "no login", which was simply untrue and dangerously so: it invited leaving the service
    # up on the assumption that nothing was protected anyway. /api/* answers 401 without a
    # session cookie and / redirects to /login. Credentials do cross the wire in clear text
    # though, which is the real caveat for a plain-HTTP demo.
    echo "  (login required; plain HTTP, so credentials are unencrypted in transit)"
    echo "  (stop the service when the demo ends — 0.0.0.0/0 ingress)"
else
    echo
    echo "  service is stopped; run 'bash deploy/start.sh' before a demo"
fi
