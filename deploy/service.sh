#!/usr/bin/env bash
# Build and push the web image, register a task definition, create/update the ECS service.
#
#   bash deploy/service.sh <image-tag>
#
# The service starts at desiredCount=0 so provisioning costs nothing. Use deploy/start.sh
# before a demo and deploy/stop.sh after it.
#
# The tag is required, and both ECR repositories are IMMUTABLE. The old `dev` default overwrote the
# only copy of the previous image, which is the one thing that makes a bad deploy unrecoverable.
# `known-good-20260730` names the last image from before the agent-autonomy rewrite.

source "$(dirname "$0")/config.sh"
require_creds
require_region
echo "China model/provider implementation remains pending; this script publishes only the web service."

TAG="${1:?a tag is required; name this build. Tags are immutable, so do not reuse one.}"
REPO="${ECR_REGISTRY}/${ECR_FRONTEND}"
IMAGE="${REPO}:${TAG}"

RUNTIME_ARN=$(aws bedrock-agentcore-control list-agent-runtimes --output json \
    | python3 "$(dirname "$0")/validate_edge.py" runtime "$RUNTIME_NAME" "$AWS_REGION" "$ACCOUNT_ID")
echo "runtime: $RUNTIME_ARN"

# Validate release and registration settings before edge, IAM, SSM or image mutations.
if [ -z "${ALLOWED_EMAIL_DOMAINS:-}" ] || [ "${ALLOWED_EMAIL_DOMAINS}" = "*" ]; then
    echo 'ERROR: ALLOWED_EMAIL_DOMAINS must explicitly restrict registration.' >&2
    exit 1
fi
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
MUTABILITY=$(aws ecr describe-repositories --repository-names "$ECR_FRONTEND" \
    --query 'repositories[0].imageTagMutability' --output text)
[ "$MUTABILITY" = IMMUTABLE ] || {
    echo 'ERROR: web repository must use IMMUTABLE tags; run deploy/provision.sh.' >&2
    exit 1
}
if aws ecr describe-images --repository-name "$ECR_FRONTEND" --image-ids "imageTag=$TAG" \
    >/dev/null 2>"$WORKDIR/image.err"; then
    echo "ERROR: web tag '$TAG' already exists; use a new immutable tag." >&2
    exit 1
elif ! grep -q '(ImageNotFoundException)' "$WORKDIR/image.err"; then
    cat "$WORKDIR/image.err" >&2
    echo 'ERROR: could not verify that the web image tag is unused.' >&2
    exit 1
fi

# Reconcile and validate the ALB and both SGs before publishing a service.
# edge.sh fails on extra ingress/listeners instead of accepting public task access.
bash "$(dirname "$0")/edge.sh"
TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
    --query 'TargetGroups[0].TargetGroupArn' --output text)
[ -n "$TG_ARN" ] && [ "$TG_ARN" != None ] || {
    echo 'ERROR: ALB target group missing; run deploy/edge.sh.' >&2; exit 1;
}
SERVICE_STATUS=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --output json | python3 "$(dirname "$0")/validate_edge.py" service-target "$TG_ARN")

# The signing key must be stable across restarts: a per-process random key would invalidate every
# existing cookie on each deploy. Generated once and kept in SSM.
SECRET_PARAM="/${PROJECT}/session-secret"
if ! aws ssm get-parameter --name "$SECRET_PARAM" --with-decryption >/dev/null 2>&1; then
    echo "generating $SECRET_PARAM"
    aws ssm put-parameter --name "$SECRET_PARAM" --type SecureString \
        --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
        --query 'Version' --output text
fi

# Written every run, not only when the parameter is created. `teardown.sh` deletes the role's inline
# policies but leaves the SSM parameter, so after a teardown the guard above is satisfied while the
# read permission is gone -- and the task then fails to start with ResourceInitializationError
# fetching the secret, which names the secret rather than the missing permission.
aws iam put-role-policy --role-name "${PROJECT}-ecs-exec" \
    --policy-name "${PROJECT}-read-secret" --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameters\",
        \"Resource\":\"arn:${AWS_PARTITION}:ssm:${AWS_REGION}:${ACCOUNT_ID}:parameter${SECRET_PARAM}\"}]}"

echo "== build + push web image =="
aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
docker build --platform linux/arm64 -f web/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"

echo "== register task definition =="
cat > "$WORKDIR/taskdef.json" <<JSON
{
  "family": "${TASK_FAMILY}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
  "cpu": "${TASK_CPU}",
  "memory": "${TASK_MEMORY}",
  "executionRoleArn": "arn:${AWS_PARTITION}:iam::${ACCOUNT_ID}:role/${PROJECT}-ecs-exec",
  "taskRoleArn": "arn:${AWS_PARTITION}:iam::${ACCOUNT_ID}:role/${PROJECT}-web-task",
  "containerDefinitions": [{
    "name": "web",
    "image": "${IMAGE}",
    "essential": true,
    "portMappings": [{"containerPort": ${WEB_PORT}, "protocol": "tcp"}],
    "user": "10001",
    "systemControls": [
      {"namespace": "net.ipv4.ip_unprivileged_port_start", "value": "0"}
    ],
    "environment": [
      {"name": "AWS_REGION", "value": "${AWS_REGION}"},
      {"name": "AGENT_RUNTIME_ARN", "value": "${RUNTIME_ARN}"},
      {"name": "IELTS_AUDIO_BUCKET", "value": "${S3_BUCKET}"},
      {"name": "ALLOWED_EMAIL_DOMAINS", "value": "${ALLOWED_EMAIL_DOMAINS:-*}"},
      {"name": "WEB_FANOUT_CONCURRENCY", "value": "${WEB_FANOUT_CONCURRENCY}"},
      {"name": "USER_STORE_S3_BUCKET", "value": "${S3_BUCKET}"},
      {"name": "USER_STORE_S3_KEY", "value": "web/users.json"},
      {"name": "PORT", "value": "${WEB_PORT}"}
    ],
    "secrets": [
      {"name": "SESSION_SECRET",
       "valueFrom": "arn:${AWS_PARTITION}:ssm:${AWS_REGION}:${ACCOUNT_ID}:parameter${SECRET_PARAM}"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "${LOG_GROUP}",
        "awslogs-region": "${AWS_REGION}",
        "awslogs-stream-prefix": "web"
      }
    }
  }]
}
JSON
REVISION=$(aws ecs register-task-definition --cli-input-json "file://$WORKDIR/taskdef.json" \
           --query 'taskDefinition.revision' --output text)
echo "  registered ${TASK_FAMILY}:${REVISION}"

SG_ID=$(aws ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
        --query 'SecurityGroups[0].GroupId' --output text)
# `assignPublicIp=ENABLED` stays even though the task is now private behind an ALB: a Fargate task in
# a public subnet needs a public IP to reach ECR (pull the image) and Bedrock. What makes it private
# is the security group — it admits only the ALB's group (`deploy/edge.sh`), so the address exists but
# nothing on the internet can open a connection to it.
NET="awsvpcConfiguration={subnets=[${SUBNET_ID}],securityGroups=[${SG_ID}],assignPublicIp=ENABLED}"

echo "== ECS service =="
if [ "$SERVICE_STATUS" = "ACTIVE" ]; then
    aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
        --task-definition "${TASK_FAMILY}:${REVISION}" --network-configuration "$NET" \
        --query 'service.serviceName' --output text
    echo "  updated (desiredCount unchanged)"
else
    LB_ARG=()
    if [ -n "$TG_ARN" ]; then
        LB_ARG=(--load-balancers "targetGroupArn=${TG_ARN},containerName=web,containerPort=${WEB_PORT}")
        # The ALB needs a moment to call /healthz before ECS decides the task is unhealthy. Fargate
        # tasks take ~40s to start serving; 90s of grace avoids a restart loop on a healthy task.
        LB_ARG+=(--health-check-grace-period-seconds 90)
    fi
    aws ecs create-service --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
        --task-definition "${TASK_FAMILY}:${REVISION}" \
        --desired-count "${RESTORE_COUNT:-0}" \
        --launch-type FARGATE --network-configuration "$NET" \
        "${LB_ARG[@]}" \
        --query 'service.serviceName' --output text
    if [ -n "${RESTORE_COUNT:-}" ]; then
        echo "  recreated behind the ALB at desiredCount=${RESTORE_COUNT}"
    else
        echo "  created at desiredCount=0 (costs nothing until started)"
    fi
fi

echo
if [ -n "$TG_ARN" ]; then
    echo "next: bash deploy/start.sh   then  bash deploy/status.sh  for the public ALB URL"
else
    echo "next: bash deploy/start.sh"
fi
