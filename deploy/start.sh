#!/usr/bin/env bash
# Bring the demo up: scale the web task to 1, wait for it, print the URL.
#
#   bash deploy/start.sh
#
# Costs nothing while stopped, so the normal cycle is start -> demo -> stop.

source "$(dirname "$0")/config.sh"
require_creds

echo "scaling $ECS_SERVICE to 1..."
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --desired-count 1 --query 'service.desiredCount' --output text

echo "waiting for the task to become running (usually 60-120s)..."
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"

exec bash "$(dirname "$0")/status.sh"
