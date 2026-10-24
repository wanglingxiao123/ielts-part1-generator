#!/usr/bin/env bash
# Create the demo infrastructure. Idempotent: re-running skips whatever already exists.
#
#   bash deploy/provision.sh
#
# Creates: the S3 bucket, 2 ECR repos, 1 ECS cluster, 1 security group, 1 log group, 2 IAM roles.
# Does NOT create: the AgentCore Runtime (see runtime.sh), the ECS service (see service.sh),
# the public ALB edge (see edge.sh).
#
# Everything here is cheap or free while idle. The standing costs in this stack are a RUNNING web
# task (start/stop) and the ALB once edge.sh has run (see stop.sh on what that does and does not
# stop).

source "$(dirname "$0")/config.sh"
require_creds
require_region

echo "== login allow-list =="
echo "  ALLOWED_EMAIL_DOMAINS=${ALLOWED_EMAIL_DOMAINS}"
echo "  (default: example.com; export ALLOWED_EMAIL_DOMAINS before any deploy script to"
echo "   override, and keep the same value for later scripts that publish the web task)"

# The bucket every other piece writes into: materials, audio clips, batch history, the candidate
# registry, and the user store. It used to be assumed to exist — the account this was built in
# already had it — so a fresh account got through provisioning and then failed at the first
# generate with an S3 404. Created here, versioned, and private.
echo "== S3 bucket =="
if aws s3api head-bucket --bucket "$S3_BUCKET" >/dev/null 2>&1; then
    echo "  $S3_BUCKET already exists"
else
    aws s3api create-bucket --bucket "$S3_BUCKET" \
        --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    # Versioning is what `teardown.sh --purge-s3` has to walk to empty the bucket, and it is on
    # because a material overwritten by a bad revision should be recoverable.
    aws s3api put-bucket-versioning --bucket "$S3_BUCKET" \
        --versioning-configuration Status=Enabled >/dev/null
    # Nothing in this system serves objects to the browser directly — audio reaches the player as a
    # presigned URL (`action: presign_audio`), so no object ever needs to be public.
    aws s3api put-public-access-block --bucket "$S3_BUCKET" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
        >/dev/null
    aws s3api put-bucket-encryption --bucket "$S3_BUCKET" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
    echo "  created $S3_BUCKET (versioned, private, SSE-S3)"
fi

echo "== ECR repositories =="
for repo in "$ECR_BACKEND" "$ECR_FRONTEND"; do
    if aws ecr describe-repositories --repository-names "$repo" >/dev/null 2>&1; then
        echo "  $repo already exists"
    else
        aws ecr create-repository --repository-name "$repo" \
            --image-tag-mutability IMMUTABLE \
            --image-scanning-configuration scanOnPush=true \
            --query 'repository.repositoryUri' --output text
    fi
    aws ecr put-image-tag-mutability --repository-name "$repo" \
        --image-tag-mutability IMMUTABLE >/dev/null
done

echo "== CloudWatch log group =="
if aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
   --query 'logGroups[0].logGroupName' --output text 2>/dev/null | grep -q "$LOG_GROUP"; then
    echo "  $LOG_GROUP already exists"
else
    aws logs create-log-group --log-group-name "$LOG_GROUP"
    # 7 days: demo logs have no long-term value and retention is the only cost here.
    aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 7
    echo "  created $LOG_GROUP (7-day retention)"
fi

echo "== security group =="
SG_ID=$(aws ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    SG_ID=$(aws ec2 create-security-group --group-name "$SG_NAME" \
            --description "IELTS Part 1 web task (ingress granted by deploy/edge.sh)" \
            --vpc-id "$VPC_ID" --query 'GroupId' --output text)
    # No ingress rule here, on purpose. This group used to be opened to `INGRESS_CIDR`
    # (0.0.0.0/0), which made the task directly reachable — and `deploy/edge.sh` then had to revoke
    # it. Granting and revoking the same rule in two scripts is how one of them ends up forgotten.
    # `edge.sh` grants the only rule the task needs: tcp/80 from the ALB's security group.
    echo "  created $SG_ID with NO ingress (deploy/edge.sh grants ALB-only access)"
else
    echo "  $SG_NAME already exists ($SG_ID)"
fi

echo "== ECS cluster =="
status=$(aws ecs describe-clusters --clusters "$ECS_CLUSTER" \
         --query 'clusters[0].status' --output text 2>/dev/null)
if [ "$status" = "ACTIVE" ]; then
    echo "  $ECS_CLUSTER already active"
else
    aws ecs create-cluster --cluster-name "$ECS_CLUSTER" \
        --query 'cluster.clusterName' --output text
fi

echo "== IAM: ECS task execution role =="
EXEC_ROLE="${PROJECT}-ecs-exec"
if aws iam get-role --role-name "$EXEC_ROLE" >/dev/null 2>&1; then
    echo "  $EXEC_ROLE already exists"
else
    # China ECS documentation retains ecs-tasks.amazonaws.com (not the endpoint suffix).
    # https://docs.amazonaws.cn/en_us/AmazonECS/latest/developerguide/task-iam-roles.html
    aws iam create-role --role-name "$EXEC_ROLE" --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},
                    "Action":"sts:AssumeRole"}]}' --query 'Role.Arn' --output text
    echo "  created $EXEC_ROLE"
fi

# Outside the branch: attaching is idempotent, and a role that lost this policy (teardown.sh detaches
# them) would otherwise never get it back on a re-provision. The symptom is a task that cannot pull
# its own image.
aws iam attach-role-policy --role-name "$EXEC_ROLE" \
    --policy-arn arn:${AWS_PARTITION}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
echo "  $EXEC_ROLE execution policy attached"

echo "== IAM: web task role (the SigV4 caller) =="
# Distinct from the execution role above: the execution role lets ECS pull the image and write
# logs, while THIS role is what the application's boto3 client assumes to sign calls. Keeping
# them separate is what stops "can start the container" from implying "can invoke the model".
WEB_ROLE="${PROJECT}-web-task"
if aws iam get-role --role-name "$WEB_ROLE" >/dev/null 2>&1; then
    echo "  $WEB_ROLE already exists"
else
    aws iam create-role --role-name "$WEB_ROLE" --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},
                    "Action":"sts:AssumeRole"}]}' --query 'Role.Arn' --output text
    echo "  created $WEB_ROLE"
fi

# Invoking a Runtime is distinct from provider/model access. Scope both resource
# shapes to this China account/region; the runtime execution role remains deferred.
aws iam put-role-policy --role-name "$WEB_ROLE" --policy-name "${PROJECT}-web-inline" \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:InvokeAgentRuntime\",
       \"Resource\":[
         \"arn:${AWS_PARTITION}:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:runtime/*\",
         \"arn:${AWS_PARTITION}:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:runtime/*/endpoint/*\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],
       \"Resource\":\"arn:${AWS_PARTITION}:s3:::${S3_BUCKET}/*\"},
      {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:${AWS_PARTITION}:s3:::${S3_BUCKET}\"},
      {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"*\"}
    ]}"
echo "  $WEB_ROLE inline policy written (China-scoped Runtime invocation, bucket and logs)"
echo "Core infrastructure provisioned. Model/runtime IAM and deployment remain pending."
echo "next: bash deploy/edge.sh"
echo "build: bash backend/scripts/deploy.sh ${ECR_REGISTRY}/${ECR_BACKEND} <tag>"
