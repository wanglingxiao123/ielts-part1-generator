"""Add narrowly scoped K3 access to the existing dedicated overseas model user.

Dry-run by default; --apply writes only ielts-part1-model-k3-inference.
Requires --overseas-profile and --overseas-account (alias --expected-account).
No user/key creation, credential rotation, secret reads or managed-policy attach.

The CallWithBearerToken action/Resource "*" contract was verified against the
AWS-managed AmazonBedrockLimitedAccess policy. This tool writes its own scoped
inline policy; it never attaches that managed policy.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import model_credentials

REGION = "us-east-1"
DESTINATIONS = ("us-east-1", "us-east-2", "us-west-2")
BASE_POLICY = "ielts-part1-model-inference"
K3_POLICY = "ielts-part1-model-k3-inference"


class AccessConflict(Exception):
    """Constant diagnostic code only; never an AWS response or supplied value."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AccessConflict("InvalidArguments")


def require(condition: Any, code: str) -> None:
    if not condition:
        raise AccessConflict(code)


def policy(account: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "K3Inference",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    f"arn:aws:bedrock:{REGION}:{account}:inference-profile/us.moonshotai.kimi-k3",
                    *(f"arn:aws:bedrock:{region}::foundation-model/moonshotai.kimi-k3"
                      for region in DESTINATIONS),
                ],
            },
            {
                "Sid": "K3BearerEntryRegion",
                "Effect": "Allow",
                "Action": ["bedrock:CallWithBearerToken"],
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
            },
        ],
    }


def document(value: Any) -> Any:
    return json.loads(unquote(value)) if isinstance(value, str) else value


def collect(iam: Any, method: str, key: str) -> list:
    return [
        value for page in iam.get_paginator(method).paginate(UserName=model_credentials.USER)
        for value in page[key]
    ]


def inspect(iam: Any, account: str) -> tuple[str, bool]:
    """Fail closed on absence, unowned users, drift or additional permission paths."""
    user = iam.get_user(UserName=model_credentials.USER)["User"]
    require(user["Arn"] == f"arn:aws:iam::{account}:user/{model_credentials.USER}"
            and user["Path"] == "/" and not user.get("PermissionsBoundary"),
            "UserIdentityConflict")
    tags = {tag["Key"]: tag["Value"] for tag in collect(iam, "list_user_tags", "Tags")}
    require(tags.get("ManagedBy") == model_credentials.OWNER, "UserOwnershipConflict")
    require(not collect(iam, "list_attached_user_policies", "AttachedPolicies"),
            "UnexpectedManagedPolicy")
    require(not collect(iam, "list_groups_for_user", "Groups"), "UnexpectedUserGroup")
    names = set(collect(iam, "list_user_policies", "PolicyNames"))
    require(BASE_POLICY in names and names <= {BASE_POLICY, K3_POLICY}, "UnexpectedInlinePolicies")
    base = document(iam.get_user_policy(
        UserName=model_credentials.USER, PolicyName=BASE_POLICY,
    )["PolicyDocument"])
    require(base == model_credentials.inference_policy(REGION), "MantlePolicyConflict")
    if K3_POLICY in names:
        existing = document(iam.get_user_policy(
            UserName=model_credentials.USER, PolicyName=K3_POLICY,
        )["PolicyDocument"])
        # Inline policies cannot be tagged. Ownership requires the tagged user,
        # this exact policy name, and the exact policy document from this tool.
        require(existing == policy(account), "K3PolicyConflict")
    return user["UserId"], K3_POLICY in names


def grant(profile: str, account: str, *, apply: bool = False) -> dict:
    require(bool(profile.strip()) and re.fullmatch(r"[0-9]{12}", account), "InvalidIdentityArguments")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    with ExitStack() as stack:
        sts = stack.enter_context(closing(session.client("sts", config=model_credentials.SDK_CONFIG)))
        identity = sts.get_caller_identity()
        require(identity["Account"] == account and re.fullmatch(
            rf"arn:aws:(iam|sts)::{account}:.+", identity["Arn"],
        ), "CallerIdentityMismatch")
        iam = stack.enter_context(closing(session.client("iam", config=model_credentials.SDK_CONFIG)))
        snapshot = inspect(iam, account)
        exists = snapshot[1]
        if apply and not exists:
            # IAM has no conditional inline-policy write. Detect preflight changes,
            # and serialize this user's policy administration operationally.
            require(inspect(iam, account) == snapshot, "UserChangedDuringPreflight")
            iam.put_user_policy(
                UserName=model_credentials.USER, PolicyName=K3_POLICY,
                PolicyDocument=json.dumps(policy(account)),
            )
        return {
            "mode": "apply" if apply else "dry-run",
            "operation": "noop" if exists else "create",
            "user_arn": f"arn:aws:iam::{account}:user/{model_credentials.USER}",
            "policy_name": K3_POLICY,
            "policy": policy(account),
            "preserved_policy": BASE_POLICY,
        }


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--overseas-profile", required=True)
    parser.add_argument("--overseas-account", "--expected-account", dest="account", required=True)
    parser.add_argument("--apply", action="store_true")
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        args = parser.parse_args(argv)
        print(json.dumps(grant(args.overseas_profile, args.account, apply=args.apply), indent=2))
        return 0
    except AccessConflict as exc:
        print(json.dumps({"error": "AccessConflict", "code": exc.args[0]}), file=sys.stderr)
    except Exception as exc:
        report = {"error": type(exc).__name__}
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", code):
            report["code"] = code
        print(json.dumps(report), file=sys.stderr)
    finally:
        logging.disable(previous_logging)
    return 1


if __name__ == "__main__":
    sys.exit(main())
