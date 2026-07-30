#!/usr/bin/env bash
# Destroy the demo infrastructure. Requires an explicit confirmation argument.
#
#   bash deploy/teardown.sh --yes            # infra only, S3 objects kept
#   bash deploy/teardown.sh --yes --purge-s3 # also delete every generated material
#
# For "the demo is over for now", use deploy/stop.sh instead -- it costs nothing while stopped
# and keeps everything ready for the next demo. This script is for "we are done with the project".

source "$(dirname "$0")/config.sh"

# Flipping `Enabled` in a distribution config. Held in a variable rather than a heredoc so the
# surrounding `if` blocks stay readable; the CLI has no --enabled flag for this.
DISABLE_CF_PY='
import json, sys
path = sys.argv[1]
with open(path) as fh:
    cfg = json.load(fh)
cfg["Enabled"] = False
with open(path, "w") as fh:
    json.dump(cfg, fh)
'

if [ "${1:-}" != "--yes" ]; then
    echo "This deletes the CloudFront distribution, ALB, target group, ECS service, cluster," >&2
    echo "AgentCore Runtime, ECR repos, IAM roles, security groups and log group for" >&2
    echo "'$PROJECT'. The delivered HTTPS URL stops working and CANNOT be recreated -- a new" >&2
    echo "distribution gets a different *.cloudfront.net hostname." >&2
    echo >&2
    echo "Re-run with --yes to proceed, or use deploy/stop.sh to just pause the demo." >&2
    exit 1
fi
require_creds

# CloudFront first: a distribution must be disabled AND fully propagated before it can be deleted,
# and that wait runs into minutes. Starting the disable here lets the rest of the teardown proceed
# while it propagates; the delete itself is at the very end of this script.
echo "== CloudFront (disable) =="
ALB_DNS=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
          --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || true)
CF_ID=""
if [ -n "$ALB_DNS" ] && [ "$ALB_DNS" != "None" ]; then
    CF_ID=$(aws cloudfront list-distributions \
            --query "DistributionList.Items[?Origins.Items[0].DomainName=='${ALB_DNS}'].Id | [0]" \
            --output text 2>/dev/null || true)
fi
if [ -n "$CF_ID" ] && [ "$CF_ID" != "None" ]; then
    etag=$(aws cloudfront get-distribution-config --id "$CF_ID" --query 'ETag' --output text)
    aws cloudfront get-distribution-config --id "$CF_ID" \
        --query 'DistributionConfig' --output json >"/tmp/${PROJECT}-cf-disable.json"
    python3 -c "$DISABLE_CF_PY" "/tmp/${PROJECT}-cf-disable.json"
    aws cloudfront update-distribution --id "$CF_ID" --if-match "$etag" \
        --distribution-config "file:///tmp/${PROJECT}-cf-disable.json" >/dev/null
    echo "  disabled $CF_ID; it is deleted at the end of this script"
else
    echo "  not present"
fi

echo "== ECS service =="
if aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
   --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
    aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
        --desired-count 0 >/dev/null
    aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" || true
    aws ecs delete-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --force >/dev/null
    echo "  deleted $ECS_SERVICE"
else
    echo "  not present"
fi

# After the service is gone, so no target is still registered with the group.
echo "== load balancer =="
ALB_ARN=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
          --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)
if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
    aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" >/dev/null
    echo "  deleted $ALB_NAME"
    # The listener goes with the ALB. The target group does not, and it cannot be deleted while the
    # ALB still references it — hence the wait.
    aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN" 2>/dev/null || true
else
    echo "  not present"
fi

echo "== target group =="
TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
         --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)
if [ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ]; then
    aws elbv2 delete-target-group --target-group-arn "$TG_ARN" >/dev/null 2>&1 \
        && echo "  deleted $TG_NAME" \
        || echo "  $TG_NAME still referenced; retry in a minute"
else
    echo "  not present"
fi

echo "== ECS cluster =="
aws ecs delete-cluster --cluster "$ECS_CLUSTER" >/dev/null 2>&1 \
    && echo "  deleted $ECS_CLUSTER" || echo "  not present"

echo "== AgentCore runtime =="
arn=$(aws bedrock-agentcore-control list-agent-runtimes \
      --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeArn" \
      --output text 2>/dev/null)
if [ -n "$arn" ] && [ "$arn" != "None" ]; then
    id="${arn##*/}"
    aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$id" >/dev/null \
        && echo "  deleted $RUNTIME_NAME"
else
    echo "  not present"
fi

echo "== ECR repositories =="
for repo in "$ECR_BACKEND" "$ECR_FRONTEND"; do
    aws ecr delete-repository --repository-name "$repo" --force >/dev/null 2>&1 \
        && echo "  deleted $repo" || echo "  $repo not present"
done

echo "== IAM roles =="
for role in "${PROJECT}-ecs-exec" "${PROJECT}-runtime"; do
    if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
        for p in $(aws iam list-attached-role-policies --role-name "$role" \
                   --query 'AttachedPolicies[].PolicyArn' --output text); do
            aws iam detach-role-policy --role-name "$role" --policy-arn "$p"
        done
        for p in $(aws iam list-role-policies --role-name "$role" \
                   --query 'PolicyNames[]' --output text); do
            aws iam delete-role-policy --role-name "$role" --policy-name "$p"
        done
        aws iam delete-role --role-name "$role" && echo "  deleted $role"
    else
        echo "  $role not present"
    fi
done

echo "== security groups =="
# Left until after the service and the ALB are gone: a SG still attached to an ENI cannot be deleted.
# Task SG first — it names the ALB SG as an ingress source, so the ALB SG cannot go while it exists.
for sg_name in "$SG_NAME" "$ALB_SG_NAME"; do
    sg=$(aws ec2 describe-security-groups \
         --filters "Name=group-name,Values=$sg_name" "Name=vpc-id,Values=$VPC_ID" \
         --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
    if [ -n "$sg" ] && [ "$sg" != "None" ]; then
        aws ec2 delete-security-group --group-id "$sg" 2>/dev/null \
            && echo "  deleted $sg_name ($sg)" \
            || echo "  $sg_name still in use; retry in a minute once the ENI is released"
    else
        echo "  $sg_name not present"
    fi
done

echo "== log group =="
aws logs delete-log-group --log-group-name "$LOG_GROUP" 2>/dev/null \
    && echo "  deleted $LOG_GROUP" || echo "  not present"

echo "== S3 =="
if [ "${2:-}" = "--purge-s3" ]; then
    # Versioning is on, so plain `rm --recursive` leaves every version behind and the bucket
    # cannot be deleted. Delete versions and delete-markers explicitly.
    echo "  purging all object versions from $S3_BUCKET..."
    python3 - "$S3_BUCKET" <<'PY'
import subprocess, sys, json
bucket = sys.argv[1]
for kind in ("Versions", "DeleteMarkers"):
    while True:
        out = subprocess.run(
            ["aws", "s3api", "list-object-versions", "--bucket", bucket,
             "--max-keys", "500", "--query", "{0}[].{{Key:Key,VersionId:VersionId}}".format(kind),
             "--output", "json"], capture_output=True, text=True)
        items = json.loads(out.stdout or "null") or []
        if not items:
            break
        payload = json.dumps({"Objects": items, "Quiet": True})
        subprocess.run(["aws", "s3api", "delete-objects", "--bucket", bucket,
                        "--delete", payload], capture_output=True, text=True)
        print("    removed {0} {1}".format(len(items), kind.lower()))
PY
    aws s3api delete-bucket --bucket "$S3_BUCKET" \
        && echo "  deleted bucket $S3_BUCKET" \
        || echo "  bucket not empty or already gone"
else
    n=$(aws s3 ls "s3://$S3_BUCKET/" --recursive 2>/dev/null | wc -l | tr -d ' ')
    echo "  KEPT $S3_BUCKET ($n objects). Pass --purge-s3 to delete generated materials too."
fi

# Last, because the disable had to propagate. A distribution still `InProgress` cannot be deleted,
# so waiting here is the only correct order.
if [ -n "$CF_ID" ] && [ "$CF_ID" != "None" ]; then
    echo
    echo "== CloudFront (delete) =="
    echo "  waiting for the disable to propagate (several minutes)..."
    aws cloudfront wait distribution-deployed --id "$CF_ID" 2>/dev/null || true
    etag=$(aws cloudfront get-distribution-config --id "$CF_ID" \
           --query 'ETag' --output text 2>/dev/null || true)
    if [ -n "$etag" ]; then
        aws cloudfront delete-distribution --id "$CF_ID" --if-match "$etag" >/dev/null 2>&1 \
            && echo "  deleted $CF_ID" \
            || echo "  not deletable yet; re-run this script, or delete $CF_ID in the console"
    fi
fi

echo
echo "teardown complete."
