#!/usr/bin/env python3
"""Provision a dedicated overseas Bedrock key into a China secret, without printing it.

Dry-run by default. --apply creates the dedicated resources. --rotate --apply
publishes a replacement; old credentials remain active for explicit retirement.
No secret is fetched, and no general-purpose IAM access key is created.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

OWNER = "ielts-part1-china"
USER = "ielts-part1-china-model"
SECRET = "ielts-part1/overseas-model"
MODEL = "moonshotai.kimi-k2.5"
SDK_CONFIG = Config(
    connect_timeout=8, read_timeout=20,
    retries={"mode": "standard", "total_max_attempts": 2},
    ignore_configured_endpoint_urls=True,
)
# IAM credential creation has no idempotency token. Retrying an ambiguous response
# can mint another key whose plaintext is lost; inspect/reconcile before retrying.
IAM_CONFIG = SDK_CONFIG.merge(Config(retries={"mode": "standard", "total_max_attempts": 1}))


class CredentialValidationError(RuntimeError):
    """Public validation diagnostics contain no originating message or response body."""

    def __init__(self, stage: str, origin: Exception, revocation_succeeded: bool):
        super().__init__(
            "New credential failed validation and was revoked" if revocation_succeeded else
            "New credential validation failed; revocation failed, reconcile before retrying"
        )
        self.diagnostics = {
            "stage": stage,
            "originating_error_type": type(origin).__name__,
            "revocation_succeeded": revocation_succeeded,
        }
        status = getattr(origin, "status_code", None)
        if status is None:
            response = getattr(origin, "response", None)
            if isinstance(response, dict):
                metadata = response.get("ResponseMetadata")
                if isinstance(metadata, dict):
                    status = metadata.get("HTTPStatusCode")
        # Do not coerce strings or objects, or accidentally serialize bool as a status.
        if type(status) is int and 100 <= status <= 599:
            self.diagnostics["http_status"] = status


def secret_version(credential_id: str) -> str:
    return hashlib.sha256(credential_id.encode("utf-8")).hexdigest()


def bearer_token(credential: dict) -> str:
    """IAM returns the full ABSK bearer key here; do not encode it again."""
    secret = credential["ServiceCredentialSecret"]
    if not isinstance(secret, str) or not secret or not all(
        33 <= ord(char) <= 126 for char in secret
    ):
        raise ValueError("Invalid service credential fields")
    return secret


def validate_existing_secret(secret: dict, tags: dict, args, keys: list[dict]) -> datetime:
    """Use metadata only; never read the current or previous secret plaintext."""
    if not re.fullmatch(
        rf"arn:aws-cn:secretsmanager:{re.escape(args.region)}:{args.china_account}:"
        rf"secret:{re.escape(SECRET)}-[A-Za-z0-9]{{6}}", secret["ARN"],
    ):
        raise ValueError("Existing secret ARN does not match the deployment")
    if tags.get("OverseasAccount") != args.overseas_account or tags.get("ModelRegion") != args.model_region:
        raise ValueError("Existing secret account or model region differs; reconcile before changing it")
    credential_id = tags.get("CredentialId", "")
    matching = [key for key in keys if key["ServiceSpecificCredentialId"] == credential_id]
    if not matching:
        raise ValueError("Secret metadata does not match a model credential")
    if not args.rotate and matching[0]["Status"] != "Active":
        raise ValueError("Secret metadata does not match an active model credential")
    current = [
        version for version, stages in secret.get("VersionIdsToStages", {}).items()
        if "AWSCURRENT" in stages
    ]
    if current != [secret_version(credential_id)]:
        raise ValueError("Secret current version and credential metadata disagree; reconcile before proceeding")
    previous = tags.get("PreviousCredentialId")
    if any(key["ServiceSpecificCredentialId"] not in (credential_id, previous) for key in keys):
        raise ValueError("Unpublished credential exists; reconcile it before proceeding")
    try:
        expiry = datetime.fromisoformat(tags["ExpiresAt"])
        if expiry.utcoffset() is None:
            raise ValueError
    except (KeyError, ValueError):
        raise ValueError("Model credential expiry metadata is missing or invalid") from None
    return expiry


def inference_policy(region: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["bedrock-mantle:CreateInference", "bedrock-mantle:CallWithBearerToken"],
            "Resource": "*",
            "Condition": {"StringEquals": {"aws:RequestedRegion": region}},
        }],
    }


def require_owner(tags: list[dict]) -> dict:
    values = {tag["Key"]: tag["Value"] for tag in tags}
    if values.get("ManagedBy") != OWNER:
        raise ValueError("Existing resource is not owned by this deployment")
    return values


def verify_identity(session, expected: str, partition: str) -> None:
    identity = session.client("sts", config=SDK_CONFIG).get_caller_identity()
    if identity["Account"] != expected or not identity["Arn"].startswith(f"arn:{partition}:"):
        raise ValueError("AWS identity does not match the requested account and partition")


def smoke_token(token: str, region: str) -> None:
    """Validate the new credential before publishing; never print model output."""
    from openai import AuthenticationError, OpenAI

    with OpenAI(
        api_key=token, base_url=f"https://bedrock-mantle.{region}.api.aws/v1",
        timeout=45, max_retries=0,
    ) as client:
        for attempt in range(2):
            try:
                result = client.chat.completions.create(
                    model=MODEL, max_tokens=128,
                    messages=[{"role": "user", "content": 'Return exactly {"status":"ok"} and nothing else.'}],
                )
                break
            except AuthenticationError:
                # A newly issued key can briefly return 401 before propagation.
                # One bounded retry reuses the same key; never create a second key.
                if attempt:
                    raise
                time.sleep(8)
    if not result.choices or json.loads(result.choices[0].message.content or "") != {"status": "ok"}:
        raise ValueError("New model credential did not pass the synthetic inference check")


def provision(args, overseas, china, smoke=smoke_token) -> dict:
    if args.region not in ("cn-north-1", "cn-northwest-1"):
        raise ValueError("Deployment must use a China region")
    if args.model_region not in ("us-east-1", "us-east-2", "us-west-2"):
        raise ValueError("Unsupported overseas model region")
    if not 1 <= args.days <= 90:
        raise ValueError("Credential lifetime must be from 1 to 90 days")
    if not all(re.fullmatch(r"[0-9]{12}", value) for value in (args.china_account, args.overseas_account)):
        raise ValueError("Expected account IDs must contain 12 digits")
    verify_identity(overseas, args.overseas_account, "aws")
    verify_identity(china, args.china_account, "aws-cn")
    iam = overseas.client("iam", region_name=args.model_region, config=IAM_CONFIG)
    secrets = china.client("secretsmanager", region_name=args.region, config=SDK_CONFIG)
    try:
        user = iam.get_user(UserName=USER)["User"]
        require_owner(user.get("Tags", []))
        if user.get("Arn") != f"arn:aws:iam::{args.overseas_account}:user/{USER}":
            raise ValueError("Existing user ARN does not match the dedicated model user")
    except iam.exceptions.NoSuchEntityException:
        user = None
    try:
        secret = secrets.describe_secret(SecretId=SECRET)
        tags = require_owner(secret.get("Tags", []))
        if secret.get("DeletedDate"):
            raise ValueError("Model secret is scheduled for deletion")
    except secrets.exceptions.ResourceNotFoundException:
        secret, tags = None, {}
    if secret and not user:
        raise ValueError("Model secret exists without its dedicated overseas user")
    keys = iam.list_service_specific_credentials(UserName=USER, ServiceName="bedrock.amazonaws.com")[
        "ServiceSpecificCredentials"
    ] if user else []
    if secret:
        expiry = validate_existing_secret(secret, tags, args, keys)
    if secret and not args.rotate:
        if expiry <= datetime.now(timezone.utc):
            raise ValueError("Model credential expired; rotate before deployment")
        return {"status": "existing", "secret_arn": secret["ARN"], "expires_at": tags["ExpiresAt"]}
    if keys and not secret:
        raise ValueError("Unpublished credential exists; reconcile it before issuing another")
    if len(keys) >= 2:
        raise ValueError("Two credentials exist; retire the previous credential after verification before rotating")
    report = {
        "status": "plan", "china_account": args.china_account,
        "overseas_account": args.overseas_account, "user": USER,
        "secret_name": SECRET, "region": args.region, "model_region": args.model_region,
        "validity_days": args.days, "rotation": args.rotate,
        "policy": inference_policy(args.model_region),
    }
    if not args.apply:
        return report
    if not user:
        iam.create_user(UserName=USER, Tags=[{"Key": "ManagedBy", "Value": OWNER}])
    iam.put_user_policy(UserName=USER, PolicyName="ielts-part1-model-inference",
                        PolicyDocument=json.dumps(inference_policy(args.model_region)))
    created = iam.create_service_specific_credential(
        UserName=USER, ServiceName="bedrock.amazonaws.com", CredentialAgeDays=args.days,
    )["ServiceSpecificCredential"]
    credential_id = created["ServiceSpecificCredentialId"]
    stage = "credential_token_validation"
    try:
        token = bearer_token(created)
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Invalid model credential")
        stage = "credential_metadata_validation"
        expiration = (
            created["ExpirationDate"] if "ExpirationDate" in created
            else created["CreateDate"] + timedelta(days=args.days)
        )
        if not isinstance(expiration, datetime) or expiration.utcoffset() is None:
            raise ValueError("Invalid model credential expiration")
        expires = expiration.astimezone(timezone.utc).isoformat()
        stage = "synthetic_inference_validation"
        smoke(token, args.model_region)
    except Exception as origin:
        # Only the newly created, unpublished key is removed. Existing live keys stay intact.
        try:
            iam.delete_service_specific_credential(UserName=USER, ServiceSpecificCredentialId=credential_id)
        except Exception:
            raise CredentialValidationError(stage, origin, revocation_succeeded=False) from None
        raise CredentialValidationError(stage, origin, revocation_succeeded=True) from None
    metadata = {
        "ManagedBy": OWNER, "CredentialId": credential_id, "ExpiresAt": expires,
        "OverseasAccount": args.overseas_account, "ModelRegion": args.model_region,
    }
    if secret:
        metadata["PreviousCredentialId"] = tags["CredentialId"]
    secret_tags = [{"Key": key, "Value": value} for key, value in metadata.items()]
    payload = json.dumps({"token": token})
    # On an ambiguous storage failure preserve the newly tested key: revocation could
    # break a successfully committed AWSCURRENT version. The next run fails for reconciliation.
    if secret:
        secrets.put_secret_value(SecretId=secret["ARN"], SecretString=payload,
                                 ClientRequestToken=secret_version(credential_id))
        secrets.tag_resource(SecretId=secret["ARN"], Tags=secret_tags)
        arn = secret["ARN"]
    else:
        arn = secrets.create_secret(Name=SECRET, SecretString=payload, Tags=secret_tags,
                                    ClientRequestToken=secret_version(credential_id),
                                    Description="IELTS POC overseas Bedrock model credential")["ARN"]
    return {"status": "configured", "secret_arn": arn, "credential_id": credential_id,
            "expires_at": expires, "previous_credentials_retained": len(keys)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--china-profile", required=True)
    parser.add_argument("--overseas-profile", required=True)
    parser.add_argument("--china-account", required=True)
    parser.add_argument("--overseas-account", required=True)
    parser.add_argument("--region", default="cn-northwest-1")
    parser.add_argument("--model-region", default="us-east-1")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        overseas = boto3.Session(profile_name=args.overseas_profile, region_name=args.model_region)
        china = boto3.Session(profile_name=args.china_profile, region_name=args.region)
        report = provision(args, overseas, china)
    except Exception as exc:
        report = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, CredentialValidationError):
            report.update(exc.diagnostics)
        # Do not expose SDK messages: request bodies or credential helper output may be included.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in {
            "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
            "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
            "NoSuchEntity", "ResourceNotFoundException", "EntityAlreadyExists",
            "ResourceExistsException", "LimitExceeded", "LimitExceededException",
            "Throttling", "ThrottlingException", "TooManyRequestsException",
            "ServiceFailure", "InternalServiceError", "InvalidParameterException",
            "InvalidRequestException", "ServiceNotSupported", "NotSupportedService",
        }:
            report["aws_error_code"] = code
        print(json.dumps(report))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
