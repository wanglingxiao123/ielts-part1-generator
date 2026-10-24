"""Publish the China Runtime with immutable image and Secret references only.

Dry-run unless --apply is supplied. Returns immediately after create/update;
poll GetAgentRuntime separately and require READY before publishing the Web.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import runtime_role

OWNER = "deploy/runtime_release.py"
NAME = runtime_role.RUNTIME_NAME
CONFIG = Config(connect_timeout=5, read_timeout=20,
                retries={"mode": "standard", "total_max_attempts": 2},
                ignore_configured_endpoint_urls=True)


def release(args) -> dict:
    settings = runtime_role.settings(args)
    registry = f"{settings.account}.dkr.ecr.{settings.region}.amazonaws.com.cn"
    match = re.fullmatch(re.escape(registry) + r"/ielts-part1-backend@(sha256:[0-9a-f]{64})", args.image)
    if not match:
        raise runtime_role.PreflightError("ChinaBackendImageDigestRequired")
    # This read-only check validates caller, secret metadata and exact existing role.
    role_plan = runtime_role.provision(settings)
    if role_plan["operation"] != "noop":
        raise runtime_role.PreflightError("RuntimeRoleMustBeProvisionedFirst")
    session = boto3.Session(profile_name=settings.profile, region_name=settings.region)
    ecr = session.client("ecr", config=CONFIG)
    repo = ecr.describe_repositories(repositoryNames=["ielts-part1-backend"])["repositories"][0]
    if repo["imageTagMutability"] != "IMMUTABLE" or repo["repositoryUri"] != f"{registry}/ielts-part1-backend":
        raise runtime_role.PreflightError("ImmutableChinaRepositoryRequired")
    images = ecr.describe_images(repositoryName="ielts-part1-backend", imageIds=[{
        "imageDigest": match[1],
    }])["imageDetails"]
    if len(images) != 1 or images[0]["imageDigest"] != match[1] or images[0]["imageSizeInBytes"] > 2_000_000_000:
        raise runtime_role.PreflightError("InvalidRuntimeImage")
    secret = session.client("secretsmanager", config=CONFIG).describe_secret(SecretId=settings.secret_arn)
    tags = {tag["Key"]: tag["Value"] for tag in secret.get("Tags", [])}
    try:
        expiry = datetime.fromisoformat(tags["ExpiresAt"])
        valid = expiry > datetime.now(timezone.utc) + timedelta(hours=1)
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise runtime_role.PreflightError("ModelCredentialExpiryInvalid")
    control = session.client("bedrock-agentcore-control", config=CONFIG)
    rows = [
        row
        for page in control.get_paginator("list_agent_runtimes").paginate()
        for row in page["agentRuntimes"] if row["agentRuntimeName"] == NAME
    ]
    if len(rows) > 1:
        raise runtime_role.PreflightError("RuntimeNameConflict")
    desired = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": args.image}},
        "roleArn": settings.role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "environmentVariables": runtime_role.runtime_environment(settings),
    }
    current = None
    if rows:
        row = rows[0]
        if not re.fullmatch(
            rf"arn:aws-cn:bedrock-agentcore:{settings.region}:{settings.account}:runtime/[A-Za-z0-9_-]+",
            row["agentRuntimeArn"],
        ):
            raise runtime_role.PreflightError("RuntimeIdentityConflict")
        owner = control.list_tags_for_resource(resourceArn=row["agentRuntimeArn"])["tags"]
        if owner.get("ManagedBy") != OWNER:
            raise runtime_role.PreflightError("RuntimeOwnershipConflict")
        current = control.get_agent_runtime(agentRuntimeId=row["agentRuntimeId"])
        if current["status"] != "READY":
            raise runtime_role.PreflightError("ExistingRuntimeNotReady")
        if any(current.get(key) for key in ("authorizerConfiguration", "requestHeaderConfiguration",
                                            "filesystemConfigurations")):
            raise runtime_role.PreflightError("RuntimeConfigurationConflict")
    operation = "create" if current is None else (
        "noop" if all(current.get(key) == value for key, value in desired.items()) else "update"
    )
    report = {"mode": "apply" if args.apply else "dry-run", "operation": operation,
              "runtime_name": NAME, "configuration": desired}
    if current:
        report.update(runtime_arn=current["agentRuntimeArn"],
                      previous_version=current["agentRuntimeVersion"],
                      previous_image=current["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"])
    if not args.apply or operation == "noop":
        return report
    # Retries of one transition share a token; a later rollback to the same
    # configuration must be a new update, not replay an earlier transition.
    transition = {"desired": desired}
    if current:
        transition.update(runtime_id=current["agentRuntimeId"],
                          previous_version=current["agentRuntimeVersion"])
    token = hashlib.sha256(json.dumps(transition, sort_keys=True).encode()).hexdigest()
    if current:
        response = control.update_agent_runtime(agentRuntimeId=current["agentRuntimeId"],
                                                clientToken=token, **desired)
    else:
        response = control.create_agent_runtime(
            agentRuntimeName=NAME, clientToken=token,
            tags={"ManagedBy": OWNER, "Project": "ielts-part1"}, **desired,
        )
    report.update(runtime_arn=response["agentRuntimeArn"], runtime_id=response["agentRuntimeId"],
                  runtime_version=response["agentRuntimeVersion"], status=response["status"])
    return report


def main() -> int:
    parser = runtime_role.SafeParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--region", default="cn-northwest-1")
    parser.add_argument("--secret-arn", required=True)
    parser.add_argument("--image", required=True, help="China backend ECR URI pinned with @sha256")
    parser.add_argument("--bucket")
    parser.add_argument("--apply", action="store_true")
    logging.disable(logging.CRITICAL)
    try:
        args = parser.parse_args()
        print(json.dumps(release(args), indent=2))
        return 0
    except runtime_role.PreflightError as exc:
        print(json.dumps({"error": type(exc).__name__, "code": exc.args[0]}))
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        report = {"error": type(exc).__name__}
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9]{1,80}", code):
            report["code"] = code
        print(json.dumps(report))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
