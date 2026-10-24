#!/usr/bin/env bash
# Destroy the demo infrastructure. Requires an explicit confirmation argument.
#
#   bash deploy/teardown.sh --yes            # infra only, S3 objects kept
#   bash deploy/teardown.sh --yes --purge-s3 # also delete every generated material
#
# For "the demo is over for now", use deploy/stop.sh instead. The ALB remains billable
# and keeps its URL for the next demo. This script is for "we are done with the project".

source "$(dirname "$0")/config.sh"

if [ "${1:-}" != "--yes" ]; then
    echo "This deletes the ALB, target group, ECS service/cluster, ECR repos," >&2
    echo "IAM roles, security groups and log group for '$PROJECT'. The ALB URL is lost." >&2
    echo >&2
    echo "Re-run with --yes to proceed, or use deploy/stop.sh to just pause the demo." >&2
    exit 1
fi
require_creds
INCOMPLETE=0
incomplete() {
    echo "  INCOMPLETE: $*" >&2
    INCOMPLETE=1
}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# Treat only an explicit not-found response as absent; denied/failed deletion is partial.
delete_resource() {
    local label="$1" missing_code="$2"
    shift 2
    if "$@" > /dev/null 2>"$WORK/delete.err"; then
        echo "  deleted $label"
    elif grep -q "($missing_code)" "$WORK/delete.err"; then
        echo "  $label not present"
    else
        cat "$WORK/delete.err" >&2
        incomplete "$label deletion failed"
    fi
}
lookup_resource() {
    local missing_code="$1"
    shift
    if "$@" 2>"$WORK/lookup.err"; then
        return 0
    elif grep -q "($missing_code)" "$WORK/lookup.err"; then
        echo None
    else
        cat "$WORK/lookup.err" >&2
        return 1
    fi
}

echo "== ECS service =="
SERVICE_STATUS=$(lookup_resource ClusterNotFoundException aws ecs describe-services \
    --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].status' --output text)
if [ "$SERVICE_STATUS" = ACTIVE ]; then
    aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
        --desired-count 0 >/dev/null
    aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
    aws ecs delete-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --force >/dev/null
    aws ecs wait services-inactive --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
    echo "  deleted $ECS_SERVICE"
else
    echo "  not present"
fi

# After the service is gone, so no target is still registered with the group.
echo "== load balancer =="
ALB_ARN=$(lookup_resource LoadBalancerNotFound aws elbv2 describe-load-balancers --names "$ALB_NAME" \
          --query 'LoadBalancers[0].LoadBalancerArn' --output text)
if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
    aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" >/dev/null
    echo "  deleted $ALB_NAME"
    # The listener goes with the ALB. The target group does not, and it cannot be deleted while the
    # ALB still references it — hence the wait.
    aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN"
else
    echo "  not present"
fi

echo "== target group =="
TG_ARN=$(lookup_resource TargetGroupNotFound aws elbv2 describe-target-groups --names "$TG_NAME" \
         --query 'TargetGroups[0].TargetGroupArn' --output text)
if [ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ]; then
    aws elbv2 delete-target-group --target-group-arn "$TG_ARN" >/dev/null 2>&1 \
        && echo "  deleted $TG_NAME" \
        || incomplete "$TG_NAME deletion failed; inspect the target group and retry"
else
    echo "  not present"
fi

echo "== ECS cluster =="
delete_resource "$ECS_CLUSTER" ClusterNotFoundException \
    aws ecs delete-cluster --cluster "$ECS_CLUSTER"

# China provisioning does not own a model Runtime or runtime IAM role.
# Their lifecycle remains pending the provider implementation.

echo "== ECR repositories =="
for repo in "$ECR_BACKEND" "$ECR_FRONTEND"; do
    delete_resource "$repo" RepositoryNotFoundException \
        aws ecr delete-repository --repository-name "$repo" --force
done

echo "== IAM roles =="
# `-web-task` used to be missing from this list, so a teardown left one role behind — the one that
# carries InvokeAgentRuntime and S3 write access, which is the least appropriate one to orphan.
for role in "${PROJECT}-ecs-exec" "${PROJECT}-web-task"; do
    if aws iam get-role --role-name "$role" >/dev/null 2>"$WORK/role.err"; then
        for p in $(aws iam list-attached-role-policies --role-name "$role" \
                   --query 'AttachedPolicies[].PolicyArn' --output text); do
            aws iam detach-role-policy --role-name "$role" --policy-arn "$p"
        done
        for p in $(aws iam list-role-policies --role-name "$role" \
                   --query 'PolicyNames[]' --output text); do
            aws iam delete-role-policy --role-name "$role" --policy-name "$p"
        done
        aws iam delete-role --role-name "$role" && echo "  deleted $role"
    elif grep -q '(NoSuchEntity)' "$WORK/role.err"; then
        echo "  $role not present"
    else
        cat "$WORK/role.err" >&2
        incomplete "could not inspect $role"
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
            || incomplete "$sg_name deletion failed; inspect ENI references and retry"
    else
        echo "  $sg_name not present"
    fi
done

echo "== log group =="
delete_resource "$LOG_GROUP" ResourceNotFoundException \
    aws logs delete-log-group --log-group-name "$LOG_GROUP"

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
             "--output", "json"], capture_output=True, text=True, check=True)
        items = json.loads(out.stdout or "null") or []
        if not items:
            break
        payload = json.dumps({"Objects": items, "Quiet": True})
        deleted = subprocess.run(["aws", "s3api", "delete-objects", "--bucket", bucket,
                                  "--delete", payload, "--output", "json"],
                                 capture_output=True, text=True, check=True)
        if json.loads(deleted.stdout or "{}").get("Errors"):
            sys.exit("ERROR: S3 returned object deletion errors; purge stopped.")
        print("    removed {0} {1}".format(len(items), kind.lower()))
PY
    aws s3api delete-bucket --bucket "$S3_BUCKET" \
        && echo "  deleted bucket $S3_BUCKET" \
        || incomplete "bucket deletion failed; inspect $S3_BUCKET"
else
    n=$(aws s3 ls "s3://$S3_BUCKET/" --recursive 2>/dev/null | wc -l | tr -d ' ')
    echo "  KEPT $S3_BUCKET ($n objects). Pass --purge-s3 to delete generated materials too."
fi

echo
if [ "$INCOMPLETE" != 0 ]; then
    echo 'Teardown incomplete; resolve the errors above and retry.' >&2
    exit 1
fi
echo "Teardown commands finished. Review resource results above; this is not a verified inventory of remaining resources."
