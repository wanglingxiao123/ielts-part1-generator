#!/usr/bin/env bash
# Destroy the demo infrastructure. Requires an explicit confirmation argument.
#
#   bash deploy/teardown.sh --yes            # infra only, S3 objects kept
#   bash deploy/teardown.sh --yes --purge-s3 # also delete every generated material
#
# For "the demo is over for now", use deploy/stop.sh instead -- it costs nothing while stopped
# and keeps everything ready for the next demo. This script is for "we are done with the project".

source "$(dirname "$0")/config.sh"

if [ "${1:-}" != "--yes" ]; then
    echo "This deletes the ECS service, cluster, AgentCore Runtime, ECR repos, IAM roles," >&2
    echo "security group and log group for '$PROJECT'." >&2
    echo >&2
    echo "Re-run with --yes to proceed, or use deploy/stop.sh to just pause the demo." >&2
    exit 1
fi
require_creds

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

echo "== security group =="
# Left until after the service is gone: a SG still attached to an ENI cannot be deleted.
sg=$(aws ec2 describe-security-groups \
     --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
     --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ -n "$sg" ] && [ "$sg" != "None" ]; then
    aws ec2 delete-security-group --group-id "$sg" 2>/dev/null \
        && echo "  deleted $sg" \
        || echo "  $sg still in use; retry in a minute once the ENI is released"
else
    echo "  not present"
fi

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

echo
echo "teardown complete."
