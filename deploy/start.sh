#!/usr/bin/env bash
# Bring the demo up: scale the web task to 1, wait for it, print the URL.
#
#   bash deploy/start.sh
#
# The ALB remains provisioned and billable while web tasks are stopped.

source "$(dirname "$0")/config.sh"
require_creds
bash "$(dirname "$0")/edge.sh" --check-only
CURRENT_TD=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --task-definition "$CURRENT_TD" --output json \
    | python3 "$(dirname "$0")/validate_edge.py" taskdef \
        "$AWS_REGION" "$ACCOUNT_ID" "$ECR_REGISTRY/$ECR_FRONTEND" "$TASK_FAMILY" >/dev/null

echo "scaling $ECS_SERVICE to 1..."
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --desired-count 1 --query 'service.desiredCount' --output text

echo "waiting for the task to become running (usually 60-120s)..."
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"

exec bash "$(dirname "$0")/status.sh"
