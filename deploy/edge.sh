#!/usr/bin/env bash
# The delivered entry point: CloudFront (HTTPS) -> ALB (HTTP, private) -> Fargate task.
#
#   bash deploy/edge.sh
#
# Idempotent: every resource is created only if absent, so re-running it is how you check the shape
# is still right. Prints the CloudFront URL at the end — that is the address to hand to the client.
#
# ## Why this exists
#
# The task used to carry its own public IP with nothing in front of it. Two consequences made that
# untenable once the URL was delivered rather than demoed:
#
#   * **The address changed on every deploy.** A public IP belongs to the task, so each rolling
#     update minted a new one. Five addresses in one afternoon.
#   * **Login was plaintext.** Port 80, no TLS, so the password and session cookie were readable by
#     anything on the path.
#
# CloudFront terminates TLS on its own `*.cloudfront.net` certificate — no domain, no ACM request,
# no Route53. The ALB gives CloudFront a stable origin and gives the task a private one.
#
# ## The three settings that are load-bearing, and why
#
# **SSE must not be buffered or cached.** The whole product is a stream of materials arriving one by
# one. The cache policy disables caching outright and the origin request policy forwards everything
# (`AllViewer`), because the app is entirely dynamic and cookie-authenticated — a cached
# `/api/batch-history` would show one reviewer another's batches.
#
# **Idle timeouts must exceed the heartbeat.** A generating batch can be silent for 96s between
# events (measured). `web/fanout.py` now emits an SSE comment every 15s; the ALB idle timeout is set
# to 120s and CloudFront's origin read timeout to 60s, both far above 15s. Without the heartbeat
# neither of these would be survivable — see the commit that added it.
#
# **HTTP/2 must be off for CloudFront's viewer protocol… no.** It is on by default and that is fine:
# the streaming problem is at the origin side, not the viewer side. Stated because it is the kind of
# thing one is tempted to change while debugging a stream, and changing it would not help.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
require_creds

echo "account ${ACCOUNT_ID} / region ${AWS_REGION}"
echo

# ── 1. security groups ────────────────────────────────────────────────────────
#
# Two groups, and the split is the point: the ALB is public, the task is not. The task's group
# admits only the ALB's group, so a task IP that leaks (they are public ENIs — `assignPublicIp` is
# still ENABLED because a Fargate task in a public subnet needs it to reach ECR and Bedrock) is
# still unreachable.
echo "== security groups =="
ALB_SG=$(aws ec2 describe-security-groups \
         --filters "Name=group-name,Values=$ALB_SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
         --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ "$ALB_SG" = "None" ] || [ -z "$ALB_SG" ]; then
    ALB_SG=$(aws ec2 create-security-group --group-name "$ALB_SG_NAME" \
             --description "IELTS Part 1 ALB (public)" --vpc-id "$VPC_ID" \
             --query 'GroupId' --output text)
    aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" \
        --protocol tcp --port 80 --cidr "$INGRESS_CIDR" >/dev/null
    echo "  created ALB sg $ALB_SG allowing tcp/80 from $INGRESS_CIDR"
else
    echo "  ALB sg exists ($ALB_SG)"
fi

TASK_SG=$(aws ec2 describe-security-groups \
          --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
          --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ "$TASK_SG" = "None" ] || [ -z "$TASK_SG" ]; then
    TASK_SG=$(aws ec2 create-security-group --group-name "$SG_NAME" \
              --description "IELTS Part 1 web task (ALB only)" --vpc-id "$VPC_ID" \
              --query 'GroupId' --output text)
    echo "  created task sg $TASK_SG"
fi

# The task accepts traffic from the ALB's group and from nothing else. Added if absent; the old
# 0.0.0.0/0 rule (if this is an upgrade from the direct-IP shape) is revoked below.
if ! aws ec2 describe-security-groups --group-ids "$TASK_SG" \
     --query "SecurityGroups[0].IpPermissions[?FromPort==\`${WEB_PORT}\`].UserIdGroupPairs[].GroupId" \
     --output text 2>/dev/null | grep -q "$ALB_SG"; then
    aws ec2 authorize-security-group-ingress --group-id "$TASK_SG" \
        --protocol tcp --port "$WEB_PORT" --source-group "$ALB_SG" >/dev/null
    echo "  task sg now admits the ALB only"
fi

# Revoke the world-open rule left by the previous shape. Not silent: removing public ingress is
# exactly the sort of change that should say so.
if aws ec2 describe-security-groups --group-ids "$TASK_SG" \
   --query "SecurityGroups[0].IpPermissions[?FromPort==\`${WEB_PORT}\`].IpRanges[].CidrIp" \
   --output text 2>/dev/null | grep -q '0.0.0.0/0'; then
    aws ec2 revoke-security-group-ingress --group-id "$TASK_SG" \
        --protocol tcp --port "$WEB_PORT" --cidr 0.0.0.0/0 >/dev/null
    echo "  REVOKED 0.0.0.0/0 from the task sg — the task is no longer directly reachable"
fi

# ── 2. target group ───────────────────────────────────────────────────────────
#
# `/healthz` is the health check because it is the one route with no auth (the `/api/*` gate would
# 401 an ALB probe, and a 401 is an unhealthy target as far as the ALB is concerned).
echo
echo "== target group =="
TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
         --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)
if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
    TG_ARN=$(aws elbv2 create-target-group --name "$TG_NAME" \
             --protocol HTTP --port "$WEB_PORT" --vpc-id "$VPC_ID" \
             --target-type ip \
             --health-check-path /healthz \
             --health-check-interval-seconds 30 \
             --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
             --query 'TargetGroups[0].TargetGroupArn' --output text)
    echo "  created ${TG_NAME}"
else
    echo "  exists"
fi
# Deregistration delay: the default 300s makes every deploy wait five minutes with two tasks
# running. 30s is enough for in-flight requests that are not streams; a stream is bounded by the
# client's own reconnect (`sseClient.ts` resumes from `since_seq`).
aws elbv2 modify-target-group-attributes --target-group-arn "$TG_ARN" \
    --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null

# ── 3. load balancer ──────────────────────────────────────────────────────────
echo
echo "== load balancer =="
ALB_ARN=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
          --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)
if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
    # shellcheck disable=SC2086 — SUBNET_IDS is a deliberate word list
    ALB_ARN=$(aws elbv2 create-load-balancer --name "$ALB_NAME" \
              --subnets $SUBNET_IDS --security-groups "$ALB_SG" \
              --scheme internet-facing --type application \
              --query 'LoadBalancers[0].LoadBalancerArn' --output text)
    echo "  created ${ALB_NAME}"
    echo "  waiting for it to come up (this takes a couple of minutes)"
    aws elbv2 wait load-balancer-available --load-balancer-arns "$ALB_ARN"
else
    echo "  exists"
fi
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$ALB_ARN" \
    --attributes "Key=idle_timeout.timeout_seconds,Value=${ALB_IDLE_TIMEOUT}" >/dev/null
echo "  idle timeout ${ALB_IDLE_TIMEOUT}s (SSE heartbeat is 15s)"

ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
          --query 'LoadBalancers[0].DNSName' --output text)

if ! aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
     --query 'Listeners[?Port==`80`].ListenerArn' --output text 2>/dev/null | grep -q .; then
    aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" \
        --protocol HTTP --port 80 \
        --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" \
        --query 'Listeners[0].ListenerArn' --output text >/dev/null
    echo "  listener :80 -> ${TG_NAME}"
fi

# ── 4. CloudFront ─────────────────────────────────────────────────────────────
#
# Managed policies rather than hand-rolled ones, by id because the CLI takes ids here:
#   CachingDisabled       4135ea2d-6df8-44a3-9df3-4b5a84be39ad
#   AllViewerExceptHostHeader  b689b0a8-53d0-40ab-baf2-68738e2966ac
#
# `AllViewerExceptHostHeader` and not `AllViewer`: forwarding the viewer's Host would send the
# CloudFront hostname to the ALB, and the ALB routes on it. Everything else — every header, every
# cookie, every query string — is forwarded, which is what a cookie-authenticated dynamic app needs.
echo
echo "== CloudFront =="
CF_ID=$(aws cloudfront list-distributions \
        --query "DistributionList.Items[?Origins.Items[0].DomainName=='${ALB_DNS}'].Id | [0]" \
        --output text 2>/dev/null || true)
if [ -z "$CF_ID" ] || [ "$CF_ID" = "None" ]; then
    cat >/tmp/${PROJECT}-cf.json <<JSON
{
  "CallerReference": "${PROJECT}-$(date +%s)",
  "Comment": "IELTS Part 1 material generator",
  "Enabled": true,
  "HttpVersion": "http2",
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "alb",
      "DomainName": "${ALB_DNS}",
      "CustomOriginConfig": {
        "HTTPPort": 80,
        "HTTPSPort": 443,
        "OriginProtocolPolicy": "http-only",
        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
        "OriginReadTimeout": 60,
        "OriginKeepaliveTimeout": 60
      }
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "alb",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {
      "Quantity": 7,
      "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
      "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}
    },
    "Compress": false,
    "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
    "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac"
  },
  "PriceClass": "PriceClass_100"
}
JSON
    CF_ID=$(aws cloudfront create-distribution \
            --distribution-config "file:///tmp/${PROJECT}-cf.json" \
            --query 'Distribution.Id' --output text)
    echo "  created ${CF_ID}"
    echo "  NOTE: a new distribution takes ~5-10 minutes to deploy worldwide."
else
    echo "  exists (${CF_ID})"
fi
CF_DOMAIN=$(aws cloudfront get-distribution --id "$CF_ID" \
            --query 'Distribution.DomainName' --output text)
CF_STATUS=$(aws cloudfront get-distribution --id "$CF_ID" \
            --query 'Distribution.Status' --output text)

# `Compress: false` is deliberate and worth stating: gzip on a streaming response is how a
# progressive stream becomes a single blob delivered at the end. The frontend bundle is a few
# hundred KB and the users are internal question writers, so the bandwidth saved is not worth
# risking the one behaviour the product depends on.

echo
echo "─────────────────────────────────────────────────────────────"
echo " DELIVER THIS URL:  https://${CF_DOMAIN}"
echo "   distribution ${CF_ID} (${CF_STATUS})"
echo "   origin       http://${ALB_DNS}  (ALB, plain HTTP, reachable directly)"
echo "─────────────────────────────────────────────────────────────"
echo
echo "next: bash deploy/service.sh   (re-register the service behind the ALB)"
echo "      bash deploy/start.sh     (desiredCount=1)"
