#!/usr/bin/env bash
# Scale the web task to 0. The delivered URL stays valid and works again after start.sh.
#
#   bash deploy/stop.sh
#
# What this stops and what it deliberately does not:
#
#   * **Stops**: the Fargate task. That is what serves requests and what can spend model and Polly
#     budget, so this is still the thing to run when nobody needs the service.
#   * **Keeps**: the ALB and the CloudFront distribution. They are what make the delivered URL
#     stable; tearing them down would hand the client a dead link and mint a different hostname
#     next time.
#
# So standing cost is NOT zero any more, and the earlier version of this file claimed it was. The ALB
# bills roughly $16-18/month whether or not a task sits behind it. That was accepted as the price of
# a deliverable URL. `deploy/teardown.sh` is what actually takes cost to zero — at the cost of the
# URL.
#
# While the task is down the CloudFront URL answers 503 (measured — the ALB has no healthy target,
# which is a 503, not the 502 an earlier version of this comment claimed). Honest (the service is
# off) but not a
# friendly page, which is worth knowing before pointing a client at it.

source "$(dirname "$0")/config.sh"
require_creds

aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --desired-count 0 --query 'service.desiredCount' --output text

echo "task stopped. S3 objects and images are untouched."
echo "the ALB and CloudFront distribution stay up, so the delivered URL keeps working after"
echo "'bash deploy/start.sh'. They cost ~\$16-18/month while they exist; run"
echo "'bash deploy/teardown.sh' to take cost to zero (that destroys the URL)."
