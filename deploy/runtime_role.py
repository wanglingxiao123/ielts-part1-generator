"""Review/provision the China Runtime execution role, never model credentials.

Example (read-only unless --apply is supplied):
    python deploy/runtime_role.py --profile china --expected-account 123456789012 \
        --region cn-northwest-1 --secret-arn <full-China-secret-ARN>

stdout is JSON: the proposed trust, inline policy, role ARN and Runtime environment.
Review it before repeating with --apply. Only this role and its inline policy can
be written. A failed policy write may leave a tagged role: rerun after resolving
the failure. No rollback, trust repair, policy detachment or resource deployment.

Trust/platform permissions reference:
https://docs.amazonaws.cn/bedrock-agentcore/latest/devguide/runtime-permissions.md
Code Interpreter operations follow backend/sandboxed_metrics.py and its SDK.
Customer-managed KMS secrets require a separately reviewed decrypt grant; this
tool fails closed for them instead of granting KMS access implicitly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROLE_NAME = "ielts-part1-runtime"
POLICY_NAME = "ielts-part1-runtime-inline"
RUNTIME_NAME = "ielts_part1_runtime"
OWNERSHIP_TAGS = {"Project": "ielts-part1", "ManagedBy": "deploy/runtime_role.py"}
SECRET_OWNER = "ielts-part1-china"
REGIONS = ("cn-north-1", "cn-northwest-1")


class PreflightError(Exception):
    """A fixed diagnostic code, never an AWS response or a supplied value."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's usual message includes rejected arguments (possibly secrets).
        raise PreflightError("InvalidArguments")


@dataclass(frozen=True)
class Settings:
    profile: str
    account: str
    region: str
    secret_arn: str
    bucket: str

    @property
    def role_arn(self) -> str:
        return f"arn:aws-cn:iam::{self.account}:role/{ROLE_NAME}"


def settings(args: argparse.Namespace) -> Settings:
    if not args.profile.strip() or not re.fullmatch(r"[0-9]{12}", args.expected_account):
        raise PreflightError("InvalidIdentityArguments")
    if args.region not in REGIONS:
        raise PreflightError("InvalidChinaRegion")
    prefix = f"arn:aws-cn:secretsmanager:{args.region}:{args.expected_account}:secret:"
    if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}", args.secret_arn):
        raise PreflightError("InvalidSecretArn")
    bucket = args.bucket or f"ielts-part1-materials-{args.expected_account}"
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or any(part in bucket for part in ("..", ".-", "-."))
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", bucket)
    ):
        raise PreflightError("InvalidBucket")
    return Settings(args.profile, args.expected_account, args.region, args.secret_arn, bucket)


def trust_policy(s: Settings) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": s.account},
                "ArnLike": {"aws:SourceArn": f"arn:aws-cn:bedrock-agentcore:{s.region}:{s.account}:*"},
            },
        }],
    }


def permissions(s: Settings) -> dict[str, Any]:
    regional = f"{s.region}:{s.account}"
    agentcore = f"arn:aws-cn:bedrock-agentcore:{regional}"
    logs = f"arn:aws-cn:logs:{regional}:log-group:"
    runtime_logs = f"{logs}/aws/bedrock-agentcore/runtimes/{RUNTIME_NAME}-*"

    def allow(sid: str, actions: list[str], resource: Any, **extra: Any) -> dict[str, Any]:
        return {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": resource, **extra}

    return {"Version": "2012-10-17", "Statement": [
        allow("BackendImage", ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
              f"arn:aws-cn:ecr:{regional}:repository/ielts-part1-backend"),
        allow("RegistryToken", ["ecr:GetAuthorizationToken"], "*"),
        allow("BucketObjects", ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
              f"arn:aws-cn:s3:::{s.bucket}/*"),
        allow("BucketList", ["s3:ListBucket"], f"arn:aws-cn:s3:::{s.bucket}"),
        allow("ModelCredential", ["secretsmanager:GetSecretValue"], s.secret_arn),
        allow("SpeechSynthesis", ["polly:SynthesizeSpeech", "polly:DescribeVoices"], "*",
              Condition={"StringEquals": {"aws:RequestedRegion": s.region}}),
        allow("RuntimeLogGroups", ["logs:CreateLogGroup", "logs:DescribeLogStreams"],
              runtime_logs),
        allow("RuntimeLogStreams", ["logs:CreateLogStream", "logs:PutLogEvents"],
              f"{runtime_logs}:log-stream:*"),
        # DescribeLogGroups has no per-group resource authorization.
        allow("LogDiscovery", ["logs:DescribeLogGroups"], "*"),
        allow("RuntimeMetrics", ["cloudwatch:PutMetricData"], "*",
              Condition={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}),
        allow("RuntimeTraces", ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                               "xray:GetSamplingRules", "xray:GetSamplingTargets"], "*"),
        allow("RuntimeIdentity", ["bedrock-agentcore:GetWorkloadAccessToken"], [
            f"{agentcore}:workload-identity-directory/default",
            f"{agentcore}:workload-identity-directory/default/workload-identity/{RUNTIME_NAME}-*",
        ]),
        # No interpreter creation/discovery; the backend selects this builtin.
        allow("AuditSandbox", [
            "bedrock-agentcore:StartCodeInterpreterSession",
            "bedrock-agentcore:InvokeCodeInterpreter",
            "bedrock-agentcore:StopCodeInterpreterSession",
            "bedrock-agentcore:GetCodeInterpreterSession",
        ], f"arn:aws-cn:bedrock-agentcore:{s.region}:aws:code-interpreter/aws.codeinterpreter.v1"),
    ]}


def runtime_environment(s: Settings) -> dict[str, str]:
    """An allowlist, not a copy of ambient credentials or environment variables."""
    return {
        "AWS_REGION": s.region,
        "AWS_DEFAULT_REGION": s.region,
        "IELTS_AUDIO_BUCKET": s.bucket,
        "IELTS_POLLY_PRESET": "china-standard",
        "IELTS_CODE_INTERPRETER_REGION": s.region,
        "IELTS_CODE_INTERPRETER_ID": "aws.codeinterpreter.v1",
        "IELTS_MODEL_AUTH": "secret",
        "IELTS_MODEL_SECRET_ARN": s.secret_arn,
        "IELTS_MODEL_SECRET_ACCOUNT_ID": s.account,
        "IELTS_MODEL_ID": "moonshotai.kimi-k2.5",
        "IELTS_ALLOWED_MODEL_IDS": (
            "moonshotai.kimi-k2.5,us.moonshotai.kimi-k3"
        ),
        "IELTS_MODEL_REGION": "us-east-1",
    }


def document(value: Any) -> Any:
    # IAM returns decoded dicts in boto3, but also accept URL-encoded IAM JSON.
    return json.loads(unquote(value)) if isinstance(value, str) else value


def inspect_role(iam: Any, s: Settings) -> tuple[bool, Any]:
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
    except iam.exceptions.NoSuchEntityException:
        return False, None
    if role["Arn"] != s.role_arn or role.get("Path") != "/":
        raise PreflightError("RoleIdentityConflict")
    tags = {
        tag["Key"]: tag["Value"]
        for page in iam.get_paginator("list_role_tags").paginate(RoleName=ROLE_NAME)
        for tag in page["Tags"]
    }
    if any(tags.get(key) != value for key, value in OWNERSHIP_TAGS.items()):
        raise PreflightError("RoleOwnershipConflict")
    # Deliberately exact structural comparison: never widen or silently repair trust.
    if document(role["AssumeRolePolicyDocument"]) != trust_policy(s):
        raise PreflightError("RoleTrustConflict")
    if role.get("PermissionsBoundary"):
        raise PreflightError("RoleBoundaryConflict")
    attached = [
        policy for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=ROLE_NAME)
        for policy in page["AttachedPolicies"]
    ]
    names = [
        name for page in iam.get_paginator("list_role_policies").paginate(RoleName=ROLE_NAME)
        for name in page["PolicyNames"]
    ]
    if attached or set(names) - {POLICY_NAME}:
        raise PreflightError("RolePolicyConflict")
    current = (
        document(iam.get_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME)["PolicyDocument"])
        if POLICY_NAME in names else None
    )
    return True, current


def provision(s: Settings, *, apply: bool = False) -> dict[str, Any]:
    session = boto3.Session(profile_name=s.profile, region_name=s.region)
    config = Config(
        connect_timeout=5, read_timeout=20,
        retries={"mode": "standard", "total_max_attempts": 3},
        ignore_configured_endpoint_urls=True,
    )
    identity = session.client("sts", config=config).get_caller_identity()
    if (
        identity["Account"] != s.account
        or not re.fullmatch(
            rf"arn:aws-cn:(iam|sts)::{s.account}:.+", identity["Arn"]
        )
    ):
        raise PreflightError("CallerIdentityMismatch")
    secret = session.client("secretsmanager", config=config).describe_secret(SecretId=s.secret_arn)
    if secret.get("ARN") != s.secret_arn:
        raise PreflightError("SecretIdentityMismatch")
    secret_tags = {tag["Key"]: tag["Value"] for tag in secret.get("Tags", [])}
    if secret_tags.get("ManagedBy") != SECRET_OWNER:
        raise PreflightError("SecretOwnershipConflict")
    if secret.get("DeletedDate"):
        raise PreflightError("SecretPendingDeletion")
    if not any("AWSCURRENT" in stages for stages in secret.get("VersionIdsToStages", {}).values()):
        raise PreflightError("SecretCurrentVersionMissing")
    if secret.get("KmsKeyId") not in (None, "", "alias/aws/secretsmanager"):
        raise PreflightError("SecretKmsReviewRequired")
    iam = session.client("iam", config=config)
    exists, current = inspect_role(iam, s)
    policy = permissions(s)
    operation = "create" if not exists else "noop" if current == policy else "update"
    result = {
        "mode": "apply" if apply else "dry-run",
        "operation": operation,
        "role_arn": s.role_arn,
        "ownership_tags": OWNERSHIP_TAGS,
        "trust_policy": trust_policy(s),
        "policy_name": POLICY_NAME,
        "permissions_policy": policy,
        "runtime_environment": runtime_environment(s),
    }
    if apply and operation != "noop":
        # Detect concurrent changes before mutation. IAM has no conditional policy
        # writes, so operators must also serialize provisioning of this role.
        if inspect_role(iam, s) != (exists, current):
            raise PreflightError("RoleChangedDuringPreflight")
        if not exists:
            iam.create_role(
                RoleName=ROLE_NAME,
                Path="/",
                AssumeRolePolicyDocument=json.dumps(trust_policy(s)),
                Tags=[{"Key": key, "Value": value} for key, value in OWNERSHIP_TAGS.items()],
            )
        iam.put_role_policy(
            RoleName=ROLE_NAME, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(policy)
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--secret-arn", required=True)
    parser.add_argument("--bucket", help="Defaults to ielts-part1-materials-<expected-account>")
    parser.add_argument("--apply", action="store_true", help="Create/update the owned role")
    try:
        args = parser.parse_args(argv)
        result = provision(settings(args), apply=args.apply)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except PreflightError as exc:
        print(json.dumps({"error": type(exc).__name__, "code": exc.args[0]}), file=sys.stderr)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # Never print exception str, AWS messages, request IDs or malformed codes.
        safe_code = code if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", code) else "UnknownAwsError"
        print(json.dumps({"error": "ClientError", "code": safe_code}), file=sys.stderr)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
