#!/usr/bin/env bash
# Build and push the web image, register a task definition, create/update the ECS service.
#
#   bash deploy/service.sh [image-tag]
#
# The service starts at desiredCount=0 so provisioning costs nothing. Use deploy/start.sh
# before a demo and deploy/stop.sh after it.

source "$(dirname "$0")/config.sh"
require_creds
require_region

TAG="${1:-dev}"
REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_FRONTEND}"
IMAGE="${REPO}:${TAG}"

RUNTIME_ARN=$(aws bedrock-agentcore-control list-agent-runtimes \
              --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeArn" \
              --output text)
if [ -z "$RUNTIME_ARN" ] || [ "$RUNTIME_ARN" = "None" ]; then
    echo "ERROR: runtime $RUNTIME_NAME not found; run deploy/runtime.sh first" >&2
    exit 1
fi
echo "runtime: $RUNTIME_ARN"

# The signing key must be stable across restarts: a per-process random key would invalidate every
# existing cookie on each deploy. Generated once and kept in SSM.
SECRET_PARAM="/${PROJECT}/session-secret"
if ! aws ssm get-parameter --name "$SECRET_PARAM" --with-decryption >/dev/null 2>&1; then
    echo "generating $SECRET_PARAM"
    aws ssm put-parameter --name "$SECRET_PARAM" --type SecureString \
        --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
        --query 'Version' --output text
    # The execution role fetches it at task start, so it needs read access on this one parameter.
    aws iam put-role-policy --role-name "${PROJECT}-ecs-exec" \
        --policy-name "${PROJECT}-read-secret" --policy-document "{
          \"Version\":\"2012-10-17\",
          \"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameters\",
            \"Resource\":\"arn:aws:ssm:${AWS_REGION}:${ACCOUNT_ID}:parameter${SECRET_PARAM}\"}]}"
fi

if [ -z "${ALLOWED_EMAIL_DOMAINS:-}" ] || [ "${ALLOWED_EMAIL_DOMAINS}" = "*" ]; then
    echo "NOTE: ALLOWED_EMAIL_DOMAINS is unset, so ANY email address may register."
    echo "      Restrict it before exposing the service, e.g.:"
    echo "        ALLOWED_EMAIL_DOMAINS=example.com bash deploy/service.sh"
fi

echo "== build + push web image =="
aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker build --platform linux/arm64 -f web/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"

echo "== register task definition =="
cat > /tmp/${PROJECT}-taskdef.json <<JSON
{
  "family": "${TASK_FAMILY}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
  "cpu": "${TASK_CPU}",
  "memory": "${TASK_MEMORY}",
  "executionRoleArn": "arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT}-ecs-exec",
  "taskRoleArn": "arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT}-web-task",
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
      {"name": "USER_STORE_S3_BUCKET", "value": "${S3_BUCKET}"},
      {"name": "USER_STORE_S3_KEY", "value": "web/users.json"},
      {"name": "PORT", "value": "${WEB_PORT}"}
    ],
    "secrets": [
      {"name": "SESSION_SECRET",
       "valueFrom": "arn:aws:ssm:${AWS_REGION}:${ACCOUNT_ID}:parameter${SECRET_PARAM}"}
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
REVISION=$(aws ecs register-task-definition --cli-input-json "file:///tmp/${PROJECT}-taskdef.json" \
           --query 'taskDefinition.revision' --output text)
echo "  registered ${TASK_FAMILY}:${REVISION}"

SG_ID=$(aws ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
        --query 'SecurityGroups[0].GroupId' --output text)
NET="awsvpcConfiguration={subnets=[${SUBNET_ID}],securityGroups=[${SG_ID}],assignPublicIp=ENABLED}"

echo "== ECS service =="
if aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
   --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
    aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
        --task-definition "${TASK_FAMILY}:${REVISION}" --network-configuration "$NET" \
        --query 'service.serviceName' --output text
    echo "  updated (desiredCount unchanged)"
else
    aws ecs create-service --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
        --task-definition "${TASK_FAMILY}:${REVISION}" --desired-count 0 \
        --launch-type FARGATE --network-configuration "$NET" \
        --query 'service.serviceName' --output text
    echo "  created at desiredCount=0 (costs nothing until started)"
fi

echo
echo "next: bash deploy/start.sh"
