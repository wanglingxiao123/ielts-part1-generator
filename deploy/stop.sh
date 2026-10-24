#!/usr/bin/env bash
# Stop web tasks; the ALB remains provisioned and billable.

source "$(dirname "$0")/config.sh"
require_creds

aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --desired-count 0 --query 'service.desiredCount' --output text

echo "task stopped. S3 objects and images are untouched."
echo "The ALB stays up. Restart with deploy/start.sh; remove infrastructure with deploy/teardown.sh."
