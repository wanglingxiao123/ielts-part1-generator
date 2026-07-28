#!/usr/bin/env bash
# Take the demo down: scale the web task to 0. Standing cost goes to zero.
#
#   bash deploy/stop.sh
#
# This is the one to run the moment a demo ends. While the task runs, the public IP is reachable
# by anyone (ingress is 0.0.0.0/0 by design for the demo), and every request can spend model and
# Polly budget. Nothing is destroyed -- start.sh brings it back, with a NEW IP.

source "$(dirname "$0")/config.sh"
require_creds

aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --desired-count 0 --query 'service.desiredCount' --output text

echo "stopped. standing cost is now zero; S3 objects and images are untouched."
