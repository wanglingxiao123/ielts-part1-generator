#!/usr/bin/env bash
# Create the demo infrastructure. Idempotent: re-running skips whatever already exists.
#
#   bash deploy/provision.sh
#
# Creates: the S3 bucket, 2 ECR repos, 1 ECS cluster, 1 security group, 1 log group, 3 IAM roles.
# Does NOT create: the AgentCore Runtime (see runtime.sh), the ECS service (see service.sh),
# the ALB / CloudFront edge (see edge.sh).
#
# Everything here is cheap or free while idle. The standing costs in this stack are a RUNNING web
# task (start/stop) and the ALB once edge.sh has run (see stop.sh on what that does and does not
# stop).

source "$(dirname "$0")/config.sh"
require_creds
require_region

# The bucket every other piece writes into: materials, audio clips, batch history, the candidate
# registry, and the user store. It used to be assumed to exist — the account this was built in
# already had it — so a fresh account got through provisioning and then failed at the first
# generate with an S3 404. Created here, versioned, and private.
echo "== S3 bucket =="
if aws s3api head-bucket --bucket "$S3_BUCKET" >/dev/null 2>&1; then
    echo "  $S3_BUCKET already exists"
else
    # us-east-1 is the one region where `create-bucket` must NOT be given a
    # LocationConstraint; passing it there is an InvalidLocationConstraint error.
    if [ "$AWS_REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$S3_BUCKET" >/dev/null
    else
        aws s3api create-bucket --bucket "$S3_BUCKET" \
            --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    fi
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
            --image-scanning-configuration scanOnPush=true \
            --query 'repository.repositoryUri' --output text
    fi
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
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
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

# Outside the branch above, deliberately, and for the same reason the runtime role's policy is: a
# policy written only on creation means an account whose role already exists never receives a
# permission this version added. The symptom is the worst kind -- provision reports success, and the
# failure arrives later as AccessDenied from a deployment that was just "provisioned successfully".
# `put-role-policy` overwrites by name, so re-running is free.
#
# InvokeAgentRuntime authorises against BOTH the runtime and its endpoint resource, so both ARN
# shapes are listed. Wildcarding the id is unavoidable here because the runtime does not exist yet at
# provision time; deploy/runtime.sh prints the real ARN, and narrowing this to it afterwards is a
# one-line change worth making if the stack outlives the demo.
aws iam put-role-policy --role-name "$WEB_ROLE" --policy-name "${PROJECT}-web-inline" \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:InvokeAgentRuntime\",
       \"Resource\":[
         \"arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:runtime/*\",
         \"arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:runtime/*/endpoint/*\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],
       \"Resource\":\"arn:aws:s3:::${S3_BUCKET}/*\"},
      {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::${S3_BUCKET}\"},
      {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"*\"}
    ]}"
echo "  $WEB_ROLE inline policy written (InvokeAgentRuntime + the one bucket)"

echo "== IAM: AgentCore runtime role =="
RT_ROLE="${PROJECT}-runtime"
if aws iam get-role --role-name "$RT_ROLE" >/dev/null 2>&1; then
    echo "  $RT_ROLE already exists"
else
    # SourceAccount/SourceArn conditions per the docs: without them any AgentCore runtime in any
    # account could be pointed at this role (the confused-deputy shape).
    aws iam create-role --role-name "$RT_ROLE" --assume-role-policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[{
        \"Effect\":\"Allow\",
        \"Principal\":{\"Service\":\"bedrock-agentcore.amazonaws.com\"},
        \"Action\":\"sts:AssumeRole\",
        \"Condition\":{
          \"StringEquals\":{\"aws:SourceAccount\":\"${ACCOUNT_ID}\"},
          \"ArnLike\":{\"aws:SourceArn\":\"arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:*\"}}
      }]}" --query 'Role.Arn' --output text
    echo "  created $RT_ROLE"
fi

# The inline policy is written on EVERY run, not only when the role is created. It used to sit inside
# the `else` above, which meant an account whose role already existed never received a newly added
# permission -- the upgrade path silently skipped it, and the symptom was an AccessDenied at run time
# on a deployment that had just been "provisioned" successfully. `put-role-policy` overwrites by
# name, so re-applying it is free and keeps the policy in the file the single source of truth.
#
# bedrock-mantle is a SEPARATE service prefix from bedrock. GPT-5.6 is served only through the
# mantle endpoint, and `bedrock:InvokeModel` does not cover it -- the call fails with
# `access_denied ... not authorized to perform: bedrock-mantle:CreateInference`. Two actions are
# needed, discovered one 401 at a time by deploying: CreateInference for the call itself, and
# CallWithBearerToken because Strands' bedrock_mantle_config mints a bearer token per request
# rather than signing with SigV4 directly. Neither appears in the AWS docs we could find.
#
# The Code Interpreter actions are what let the audit side run its metrics script somewhere the
# generator's blueprint does not exist. `strands_tools.shell` would have been the obvious way to
# let an agent run a script, but it calls `pty.fork()` directly and takes no `agent` parameter,
# so no sandbox can constrain it -- a shell-equipped audit agent can read the blueprint schema
# off the local filesystem. Code Interpreter inverts that: the remote environment starts empty
# and holds only the two files we upload. The identifier is the built-in
# `aws.codeinterpreter.v1`, so there is no resource to provision here.
#
# Scoped to what the loop actually does: pull its image, log, invoke the model, synthesize
# speech, run the audit metrics remotely, and read/write THIS bucket. No wildcard on S3 -- a
# demo role should not reach the other 40-odd buckets in this account. ECR and the metrics/token
# actions are required by the platform itself, not by our code, and the runtime fails to start
# without them.
aws iam put-role-policy --role-name "$RT_ROLE" --policy-name "${PROJECT}-runtime-inline" \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchGetImage\",\"ecr:GetDownloadUrlForLayer\"],
       \"Resource\":\"arn:aws:ecr:${AWS_REGION}:${ACCOUNT_ID}:repository/${ECR_BACKEND}\"},
      {\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":[\"bedrock:InvokeModel\",\"bedrock:InvokeModelWithResponseStream\"],\"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":[\"bedrock-mantle:CreateInference\",\"bedrock-mantle:CreateResponse\",\"bedrock-mantle:CreateChatCompletion\",\"bedrock-mantle:CallWithBearerToken\",\"bedrock:CallWithBearerToken\"],\"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":\"polly:SynthesizeSpeech\",\"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:DeleteObject\"],
       \"Resource\":\"arn:aws:s3:::${S3_BUCKET}/*\"},
      {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::${S3_BUCKET}\"},
      {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",
        \"logs:PutLogEvents\",\"logs:DescribeLogStreams\",\"logs:DescribeLogGroups\"],\"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":\"cloudwatch:PutMetricData\",\"Resource\":\"*\",
       \"Condition\":{\"StringEquals\":{\"cloudwatch:namespace\":\"bedrock-agentcore\"}}},
      {\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:GetWorkloadAccessToken\",\"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":[
         \"bedrock-agentcore:StartCodeInterpreterSession\",
         \"bedrock-agentcore:InvokeCodeInterpreter\",
         \"bedrock-agentcore:StopCodeInterpreterSession\",
         \"bedrock-agentcore:GetCodeInterpreterSession\",
         \"bedrock-agentcore:ListCodeInterpreterSessions\",
         \"bedrock-agentcore:GetCodeInterpreter\",
         \"bedrock-agentcore:ListCodeInterpreters\"],
       \"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":[\"xray:PutTraceSegments\",\"xray:PutTelemetryRecords\"],\"Resource\":\"*\"}
    ]}"
echo "  applied ${PROJECT}-runtime-inline (bucket-scoped, incl. Code Interpreter)"

echo
echo "provisioned. next:"
echo "  1. bash backend/scripts/deploy.sh $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_BACKEND"
echo "  2. bash deploy/runtime.sh      # create the AgentCore Runtime"
echo "  3. bash deploy/service.sh      # push the web image and create the ECS service"
echo "  4. bash deploy/start.sh        # scale to 1 and print the URL"
