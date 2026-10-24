"""Offline credential lifecycle tests; no real key or AWS request."""
import json
import logging
import sys
import traceback
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from botocore.endpoint import Endpoint
from botocore.exceptions import ClientError
from botocore.stub import Stubber
import httpx
import pytest

from deploy import model_credentials as module

TEST_BEARER = module.bearer_token({
    "ServiceCredentialAlias": "offline-public-alias",
    "ServiceCredentialSecret": "never-print-this-test-secret",
})


class Missing(Exception):
    pass


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Unexpected live request in offline credential tests")
    # Block transport, while allowing Stubber to validate actual SDK API schemas.
    monkeypatch.setattr(Endpoint, "make_request", unexpected_request)
    monkeypatch.setattr(httpx.Client, "send", unexpected_request)


def owned_user(args):
    return {"User": {
        "Arn": f"arn:aws:iam::{args.overseas_account}:user/{module.USER}",
        "Tags": [{"Key": "ManagedBy", "Value": module.OWNER}],
    }}


@pytest.fixture
def setup():
    args = SimpleNamespace(
        region="cn-northwest-1", model_region="us-east-1", days=90,
        china_account="123456789012", overseas_account="210987654321",
        apply=False, rotate=False,
    )
    iam, secrets = Mock(), Mock()
    iam.exceptions.NoSuchEntityException = Missing
    secrets.exceptions.ResourceNotFoundException = Missing
    iam.get_user.side_effect = Missing
    secrets.describe_secret.side_effect = Missing
    iam.create_service_specific_credential.return_value = {"ServiceSpecificCredential": {
        "ServiceSpecificCredentialId": "ACCAOFFLINETEST",
        "ServiceCredentialSecret": "never-print-this-test-secret",
        "CreateDate": datetime.now(timezone.utc),
        "ServiceName": "bedrock.amazonaws.com",
        "ServiceCredentialAlias": "offline-public-alias",
        "UserName": module.USER,
        "Status": "Active",
    }}
    secrets.create_secret.return_value = {
        "ARN": "arn:aws-cn:secretsmanager:cn-northwest-1:123456789012:secret:test-abcdef",
    }
    overseas, china = Mock(), Mock()
    overseas_sts, china_sts = Mock(), Mock()
    overseas_sts.get_caller_identity.return_value = {
        "Account": args.overseas_account, "Arn": f"arn:aws:sts::{args.overseas_account}:assumed-role/test/user",
    }
    china_sts.get_caller_identity.return_value = {
        "Account": args.china_account, "Arn": f"arn:aws-cn:sts::{args.china_account}:assumed-role/test/user",
    }
    overseas.client.side_effect = lambda service, **kw: {"iam": iam, "sts": overseas_sts}[service]
    china.client.side_effect = lambda service, **kw: {"secretsmanager": secrets, "sts": china_sts}[service]
    return args, overseas, china, iam, secrets


def test_preview_does_not_create_credentials(setup):
    args, overseas, china, iam, secrets = setup
    result = module.provision(args, overseas, china)
    assert result["status"] == "plan"
    iam.create_user.assert_not_called()
    iam.create_service_specific_credential.assert_not_called()
    secrets.create_secret.assert_not_called()


def test_apply_validates_before_publishing_and_never_reports_secret(setup):
    args, overseas, china, iam, secrets = setup
    args.apply = True
    smoke = Mock()
    events = []
    smoke.side_effect = lambda *_: events.append("validated")
    secrets.create_secret.side_effect = lambda **kw: events.append("published") or {"ARN": "secret-arn"}
    result = module.provision(args, overseas, china, smoke)
    assert events == ["validated", "published"]
    smoke.assert_called_once_with(TEST_BEARER, "us-east-1")
    assert json.loads(secrets.create_secret.call_args.kwargs["SecretString"]) == {
        "token": TEST_BEARER,
    }
    assert "never-print" not in json.dumps(result)
    policy = json.loads(iam.put_user_policy.call_args.kwargs["PolicyDocument"])
    assert policy["Statement"][0]["Condition"]["StringEquals"]["aws:RequestedRegion"] == "us-east-1"
    assert set(policy["Statement"][0]["Action"]) == {
        "bedrock-mantle:CreateInference", "bedrock-mantle:CallWithBearerToken",
    }
    secrets.get_secret_value.assert_not_called()


def test_failed_inference_revokes_only_unpublished_key(setup):
    args, overseas, china, iam, secrets = setup
    args.apply = True
    with pytest.raises(RuntimeError, match="revoked"):
        module.provision(args, overseas, china, Mock(side_effect=RuntimeError("private detail")))
    iam.delete_service_specific_credential.assert_called_once_with(
        UserName=module.USER, ServiceSpecificCredentialId="ACCAOFFLINETEST",
    )
    secrets.create_secret.assert_not_called()


def test_unowned_user_cannot_be_modified(setup):
    args, overseas, china, iam, secrets = setup
    args.apply = True
    iam.get_user.side_effect = None
    iam.get_user.return_value = {"User": {"Tags": []}}
    with pytest.raises(ValueError, match="not owned"):
        module.provision(args, overseas, china)
    iam.put_user_policy.assert_not_called()


def test_wrong_account_prevents_mutations(setup):
    args, overseas, china, iam, secrets = setup
    args.china_account = "111111111111"
    with pytest.raises(ValueError, match="identity"):
        module.provision(args, overseas, china)
    iam.create_user.assert_not_called()


def test_unpublished_key_requires_reconciliation(setup):
    args, overseas, china, iam, secrets = setup
    args.apply = True
    iam.get_user.side_effect = None
    iam.get_user.return_value = owned_user(args)
    iam.list_service_specific_credentials.return_value = {"ServiceSpecificCredentials": [
        {"ServiceSpecificCredentialId": "existing"},
    ]}
    with pytest.raises(ValueError, match="Unpublished"):
        module.provision(args, overseas, china)
    iam.create_service_specific_credential.assert_not_called()


def test_ambiguous_secret_write_does_not_revoke_potentially_live_key(setup):
    args, overseas, china, iam, secrets = setup
    args.apply = True
    secrets.create_secret.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        module.provision(args, overseas, china, Mock())
    iam.delete_service_specific_credential.assert_not_called()


@pytest.fixture
def existing(setup):
    args, overseas, china, iam, secrets = setup
    iam.get_user.side_effect = None
    iam.get_user.return_value = owned_user(args)
    metadata = {
        "ManagedBy": module.OWNER, "CredentialId": "ACCAOLDTEST",
        "OverseasAccount": args.overseas_account, "ModelRegion": args.model_region,
        "ExpiresAt": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat(),
    }
    secret = {
        "ARN": f"arn:aws-cn:secretsmanager:{args.region}:{args.china_account}:secret:{module.SECRET}-abcdef",
        "Tags": [{"Key": k, "Value": v} for k, v in metadata.items()],
        "VersionIdsToStages": {module.secret_version("ACCAOLDTEST"): ["AWSCURRENT"]},
    }
    secrets.describe_secret.side_effect = None
    secrets.describe_secret.return_value = secret
    iam.list_service_specific_credentials.return_value = {"ServiceSpecificCredentials": [
        {"ServiceSpecificCredentialId": "ACCAOLDTEST", "Status": "Active"},
    ]}
    return setup


def change_tag(secrets, key, value):
    tags = secrets.describe_secret.return_value["Tags"]
    tags[:] = [tag for tag in tags if tag["Key"] != key]
    tags.append({"Key": key, "Value": value})


def assert_no_writes(iam, secrets):
    for method in ("create_user", "put_user_policy", "create_service_specific_credential",
                   "delete_service_specific_credential"):
        getattr(iam, method).assert_not_called()
    for method in ("create_secret", "put_secret_value", "tag_resource"):
        getattr(secrets, method).assert_not_called()
    secrets.get_secret_value.assert_not_called()


@pytest.mark.parametrize("apply", [False, True])
def test_existing_configuration_is_idempotent(existing, apply):
    args, overseas, china, iam, secrets = existing
    args.apply = apply
    smoke = Mock()
    first = module.provision(args, overseas, china, smoke)
    assert module.provision(args, overseas, china, smoke) == first
    assert first["status"] == "existing"
    assert_no_writes(iam, secrets)
    smoke.assert_not_called()


def test_rotation_dry_run_is_read_only(existing):
    args, overseas, china, iam, secrets = existing
    args.rotate = True
    assert module.provision(args, overseas, china, Mock())["rotation"] is True
    assert_no_writes(iam, secrets)


def test_rotation_order_retains_old_key_and_rerun_is_idempotent(existing):
    args, overseas, china, iam, secrets = existing
    args.apply = args.rotate = True
    events = []
    secret = secrets.describe_secret.return_value
    credentials = iam.list_service_specific_credentials.return_value["ServiceSpecificCredentials"]
    created = iam.create_service_specific_credential.return_value

    def create(**kwargs):
        events.append("create")
        iam.list_service_specific_credentials.return_value = {"ServiceSpecificCredentials": credentials + [{
            "ServiceSpecificCredentialId": created["ServiceSpecificCredential"]["ServiceSpecificCredentialId"],
            "Status": "Active",
        }]}
        return created

    def put(**kwargs):
        events.append("publish")
        assert kwargs["SecretId"] == secret["ARN"]
        assert json.loads(kwargs["SecretString"]) == {"token": TEST_BEARER}
        secret["VersionIdsToStages"] = {
            module.secret_version("ACCAOLDTEST"): ["AWSPREVIOUS"],
            kwargs["ClientRequestToken"]: ["AWSCURRENT"],
        }

    def tag(**kwargs):
        events.append("tag")
        secret["Tags"] = kwargs["Tags"]

    iam.create_service_specific_credential.side_effect = create
    secrets.put_secret_value.side_effect = put
    secrets.tag_resource.side_effect = tag
    result = module.provision(args, overseas, china, Mock(side_effect=lambda *_: events.append("smoke")))
    assert events == ["create", "smoke", "publish", "tag"]
    assert result["previous_credentials_retained"] == 1
    assert "never-print" not in json.dumps(result)
    assert {t["Key"]: t["Value"] for t in secret["Tags"]}["PreviousCredentialId"] == "ACCAOLDTEST"
    iam.delete_service_specific_credential.assert_not_called()
    secrets.create_secret.assert_not_called()
    # A normal apply after rotation recognizes the current and retained prior keys.
    args.rotate = False
    assert module.provision(args, overseas, china, Mock())["status"] == "existing"
    assert events == ["create", "smoke", "publish", "tag"]
    # Another rotation needs explicit retirement before creating a third key.
    args.rotate = True
    with pytest.raises(ValueError, match="Two credentials"):
        module.provision(args, overseas, china, Mock())
    assert events == ["create", "smoke", "publish", "tag"]


def test_first_apply_then_rerun_is_idempotent(setup):
    args, overseas, china, iam, secrets = setup
    args.apply = True

    def create_user(**kwargs):
        iam.get_user.side_effect = None
        iam.get_user.return_value = owned_user(args)

    def create_secret(**kwargs):
        secret = {
            "ARN": f"arn:aws-cn:secretsmanager:{args.region}:{args.china_account}:secret:{module.SECRET}-abcdef",
            "Tags": kwargs["Tags"], "VersionIdsToStages": {kwargs["ClientRequestToken"]: ["AWSCURRENT"]},
        }
        secrets.describe_secret.side_effect = None
        secrets.describe_secret.return_value = secret
        iam.list_service_specific_credentials.return_value = {"ServiceSpecificCredentials": [
            {"ServiceSpecificCredentialId": "ACCAOFFLINETEST", "Status": "Active"},
        ]}
        return secret

    iam.create_user.side_effect = create_user
    secrets.create_secret.side_effect = create_secret
    smoke = Mock()
    assert module.provision(args, overseas, china, smoke)["status"] == "configured"
    assert module.provision(args, overseas, china, smoke)["status"] == "existing"
    iam.create_user.assert_called_once()
    iam.create_service_specific_credential.assert_called_once()
    secrets.create_secret.assert_called_once()
    smoke.assert_called_once()
    secrets.get_secret_value.assert_not_called()


@pytest.mark.parametrize("rotate", [False, True])
@pytest.mark.parametrize("tag,value", [
    ("ManagedBy", "someone-else"),
    ("OverseasAccount", "999999999999"),
    ("ModelRegion", "us-west-2"),
    ("ExpiresAt", "invalid"),
    ("ExpiresAt", "2099-01-01T00:00:00"),
    ("CredentialId", "ACCAUNKNOWN"),
])
def test_bad_metadata_rejected_before_mutations(existing, rotate, tag, value):
    args, overseas, china, iam, secrets = existing
    args.apply, args.rotate = True, rotate
    change_tag(secrets, tag, value)
    with pytest.raises(ValueError):
        module.provision(args, overseas, china, Mock())
    assert_no_writes(iam, secrets)


@pytest.mark.parametrize("state", ["deleted", "wrong_arn", "no_current", "wrong_current",
                                  "orphan_key", "missing_user", "wrong_user_arn"])
def test_inconsistent_resources_rejected_before_mutations(existing, state):
    args, overseas, china, iam, secrets = existing
    args.apply = args.rotate = True
    secret = secrets.describe_secret.return_value
    if state == "deleted":
        secret["DeletedDate"] = datetime.now(timezone.utc)
    elif state == "wrong_arn":
        secret["ARN"] = secret["ARN"].replace(args.china_account, "999999999999")
    elif state == "no_current":
        secret["VersionIdsToStages"] = {}
    elif state == "wrong_current":
        secret["VersionIdsToStages"] = {"unknown-version": ["AWSCURRENT"]}
    elif state == "orphan_key":
        iam.list_service_specific_credentials.return_value["ServiceSpecificCredentials"].append(
            {"ServiceSpecificCredentialId": "orphan", "Status": "Active"})
    elif state == "missing_user":
        iam.get_user.side_effect = Missing
    else:
        iam.get_user.return_value["User"]["Arn"] += "-different"
    with pytest.raises(ValueError):
        module.provision(args, overseas, china, Mock())
    assert_no_writes(iam, secrets)


def test_rotation_failure_revokes_new_key_and_preserves_old_secret(existing):
    args, overseas, china, iam, secrets = existing
    args.apply = args.rotate = True
    with pytest.raises(RuntimeError, match="revoked"):
        module.provision(args, overseas, china, Mock(side_effect=ValueError("never-print-this-test-secret")))
    iam.delete_service_specific_credential.assert_called_once_with(
        UserName=module.USER, ServiceSpecificCredentialId="ACCAOFFLINETEST")
    secrets.put_secret_value.assert_not_called()
    secrets.tag_resource.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["publish", "tag"])
def test_partial_rotation_never_revokes_and_rerun_requires_reconciliation(existing, failure_stage):
    args, overseas, china, iam, secrets = existing
    args.apply = args.rotate = True
    created = iam.create_service_specific_credential.return_value

    def create(**kwargs):
        iam.list_service_specific_credentials.return_value["ServiceSpecificCredentials"].append(
            {"ServiceSpecificCredentialId": "ACCAOFFLINETEST", "Status": "Active"})
        return created

    def publish(**kwargs):
        secrets.describe_secret.return_value["VersionIdsToStages"] = {
            kwargs["ClientRequestToken"]: ["AWSCURRENT"]}
        if failure_stage == "publish":
            raise TimeoutError("never-print-this-test-secret")

    iam.create_service_specific_credential.side_effect = create
    secrets.put_secret_value.side_effect = publish
    secrets.tag_resource.side_effect = TimeoutError("never-print-this-test-secret")
    with pytest.raises(TimeoutError):
        module.provision(args, overseas, china, Mock())
    iam.delete_service_specific_credential.assert_not_called()
    args.rotate = False
    with pytest.raises(ValueError, match="reconcile"):
        module.provision(args, overseas, china, Mock())
    iam.create_service_specific_credential.assert_called_once()


def test_failed_revocation_does_not_claim_success_or_publish(existing):
    args, overseas, china, iam, secrets = existing
    args.apply = args.rotate = True
    iam.delete_service_specific_credential.side_effect = RuntimeError("never-print-this-test-secret")
    with pytest.raises(RuntimeError, match="revocation failed") as error:
        module.provision(args, overseas, china, Mock(side_effect=ValueError("private smoke error")))
    assert "never-print" not in str(error.value)
    assert error.value.__suppress_context__
    secrets.put_secret_value.assert_not_called()


@pytest.mark.parametrize("token", [None, "", 123, "bad\nheader"])
def test_invalid_returned_key_is_revoked_without_smoke(setup, token):
    args, overseas, china, iam, secrets = setup
    args.apply = True
    iam.create_service_specific_credential.return_value["ServiceSpecificCredential"]["ServiceCredentialSecret"] = token
    smoke = Mock()
    with pytest.raises(RuntimeError, match="revoked"):
        module.provision(args, overseas, china, smoke)
    smoke.assert_not_called()
    iam.delete_service_specific_credential.assert_called_once()
    secrets.create_secret.assert_not_called()


@pytest.mark.parametrize("operation", ["get_user", "describe_secret", "list_service_specific_credentials"])
def test_discovery_access_denied_is_not_absence(existing, operation):
    args, overseas, china, iam, secrets = existing
    args.apply = True
    client = secrets if operation == "describe_secret" else iam
    getattr(client, operation).side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "private"}}, operation)
    with pytest.raises(ClientError):
        module.provision(args, overseas, china, Mock())
    assert_no_writes(iam, secrets)


def test_client_configuration_bounds_retries_and_pins_china_region(setup):
    args, overseas, china, _, _ = setup
    module.provision(args, overseas, china)
    iam_args = next(call.kwargs for call in overseas.client.call_args_list if call.args == ("iam",))
    assert iam_args["config"].retries == {"mode": "standard", "total_max_attempts": 1}
    secret_args = next(call.kwargs for call in china.client.call_args_list if call.args == ("secretsmanager",))
    assert secret_args["region_name"] == args.region
    assert secret_args["config"].ignore_configured_endpoint_urls
    assert secret_args["config"].connect_timeout == 8
    assert secret_args["config"].read_timeout == 20


@pytest.mark.parametrize("state", ["expired", "inactive"])
def test_expired_or_inactive_credential_requires_rotation(existing, state):
    args, overseas, china, iam, secrets = existing
    if state == "expired":
        change_tag(secrets, "ExpiresAt", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    else:
        iam.list_service_specific_credentials.return_value["ServiceSpecificCredentials"][0]["Status"] = "Inactive"
    args.apply = True
    with pytest.raises(ValueError):
        module.provision(args, overseas, china, Mock())
    assert_no_writes(iam, secrets)
    args.rotate = True
    assert module.provision(args, overseas, china, Mock())["status"] == "configured"
    iam.delete_service_specific_credential.assert_not_called()


def test_partial_first_creation_with_unpublished_key_blocks_rerun(setup):
    args, overseas, china, iam, secrets = setup
    args.apply = True

    def create_user(**kwargs):
        iam.get_user.side_effect = None
        iam.get_user.return_value = owned_user(args)

    created = iam.create_service_specific_credential.return_value

    def create_key(**kwargs):
        iam.list_service_specific_credentials.return_value = {"ServiceSpecificCredentials": [
            {"ServiceSpecificCredentialId": "ACCAOFFLINETEST", "Status": "Active"}]}
        return created

    iam.create_user.side_effect = create_user
    iam.create_service_specific_credential.side_effect = create_key
    secrets.create_secret.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        module.provision(args, overseas, china, Mock())
    with pytest.raises(ValueError, match="Unpublished"):
        module.provision(args, overseas, china, Mock())
    iam.create_service_specific_credential.assert_called_once()
    iam.delete_service_specific_credential.assert_not_called()


@pytest.mark.parametrize("error,expected_code", [
    (RuntimeError("never-print-this-test-secret"), None),
    (ClientError({"Error": {"Code": "AccessDenied", "Message": "never-print-this-test-secret"}},
                 "CreateServiceSpecificCredential"), "AccessDenied"),
    (ClientError({"Error": {"Code": "never-print-this-test-secret", "Message": "private"}},
                 "CreateServiceSpecificCredential"), None),
])
def test_cli_failure_output_never_contains_secret(monkeypatch, capsys, error, expected_code):
    monkeypatch.setattr(sys, "argv", [
        "model_credentials.py", "--china-profile", "offline", "--overseas-profile", "offline",
        "--china-account", "123456789012", "--overseas-account", "210987654321",
    ])
    monkeypatch.setattr(module.boto3, "Session", Mock())
    monkeypatch.setattr(module, "provision", Mock(side_effect=error))
    previous_logging = logging.root.manager.disable
    try:
        assert module.main() == 1
    finally:
        logging.disable(previous_logging)
    captured = capsys.readouterr()
    assert "never-print" not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["status"] == "failed"
    assert result.get("aws_error_code") == expected_code


@pytest.mark.parametrize("revoked", [True, False])
@pytest.mark.parametrize("failure", ["http", "sdk_http", "json", "token", "metadata"])
def test_validation_diagnostics_reach_cli_without_secret(
    setup, monkeypatch, capsys, revoked, failure,
):
    args, overseas, china, iam, secrets = setup
    args.apply = True
    private = "never-print-this-test-secret"
    stage = "synthetic_inference_validation"
    http_status = None
    if failure == "http":
        from openai import PermissionDeniedError
        origin = PermissionDeniedError(
            private, response=httpx.Response(403, request=httpx.Request("POST", "https://offline.invalid")),
            body={"token": private},
        )
        http_status, error_type = 403, "PermissionDeniedError"
    elif failure == "sdk_http":
        origin = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": private},
             "ResponseMetadata": {"HTTPStatusCode": 403}}, "OfflineTest",
        )
        http_status, error_type = 403, "ClientError"
    elif failure == "json":
        origin = json.JSONDecodeError(private, private, 0)
        error_type = "JSONDecodeError"
    else:
        created = iam.create_service_specific_credential.return_value["ServiceSpecificCredential"]
        del created["ServiceCredentialSecret" if failure == "token" else "CreateDate"]
        stage = f"credential_{failure}_validation"
        origin, error_type = None, "KeyError"
    if not revoked:
        iam.delete_service_specific_credential.side_effect = RuntimeError(private)
    with pytest.raises(module.CredentialValidationError) as raised:
        module.provision(args, overseas, china, Mock(side_effect=origin))
    expected = {
        "stage": stage, "originating_error_type": error_type,
        "revocation_succeeded": revoked,
    }
    if http_status is not None:
        expected["http_status"] = http_status
    assert raised.value.diagnostics == expected
    assert private not in "".join(traceback.format_exception(raised.value))
    secrets.create_secret.assert_not_called()
    secrets.put_secret_value.assert_not_called()
    monkeypatch.setattr(sys, "argv", [
        "model_credentials.py", "--china-profile", "offline", "--overseas-profile", "offline",
        "--china-account", args.china_account, "--overseas-account", args.overseas_account,
    ])
    monkeypatch.setattr(module.boto3, "Session", Mock())
    monkeypatch.setattr(module, "provision", Mock(side_effect=raised.value))
    previous_logging = logging.root.manager.disable
    try:
        assert module.main() == 1
    finally:
        logging.disable(previous_logging)
    captured = capsys.readouterr()
    assert private not in captured.out + captured.err
    assert json.loads(captured.out) == {
        "status": "failed", "error_type": "CredentialValidationError", **expected,
    }


@pytest.mark.parametrize("status", ["never-print-this-test-secret", "403", True, None, {}, 0, 600])
def test_validation_diagnostics_do_not_coerce_http_status(status):
    origin = RuntimeError("never-print-this-test-secret")
    origin.status_code = status
    error = module.CredentialValidationError("synthetic_inference_validation", origin, True)
    assert "http_status" not in error.diagnostics
    assert "never-print" not in json.dumps(error.diagnostics)


@pytest.mark.parametrize("returned_expiration", [True, False])
@pytest.mark.parametrize("has_bedrock_secret", [True, False])
def test_real_iam_response_schema_and_expiration(
    setup, monkeypatch, tmp_path, returned_expiration, has_bedrock_secret,
):
    args, overseas, china, iam, secrets = setup
    args.apply = True
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    session = module.boto3.Session(
        aws_access_key_id="offline-key", aws_secret_access_key="offline-secret",
        region_name=args.model_region,
    )
    client = session.client("iam", config=module.IAM_CONFIG)
    created_at = datetime.now(timezone.utc)
    actual_expiration = created_at + timedelta(days=89, hours=2)
    credential = {
        "CreateDate": created_at,
        "ServiceName": "bedrock.amazonaws.com",
        "ServiceCredentialAlias": "offline-public-alias",
        "ServiceSpecificCredentialId": "ACCA0000000000000000",
        "UserName": module.USER,
        "Status": "Active",
    }
    if returned_expiration:
        credential["ExpirationDate"] = actual_expiration
    if has_bedrock_secret:
        credential["ServiceCredentialSecret"] = "never-print-this-test-secret"
    else:
        # A generic service password cannot substitute for the Bedrock secret.
        credential["ServicePassword"] = "wrong-service-password"
    smoke = Mock()
    with closing(client), Stubber(client) as stubber:
        stubber.add_response(
            "create_service_specific_credential", {"ServiceSpecificCredential": credential},
            {"UserName": module.USER, "ServiceName": "bedrock.amazonaws.com", "CredentialAgeDays": 90},
        )
        iam.create_service_specific_credential.side_effect = client.create_service_specific_credential
        if has_bedrock_secret:
            result = module.provision(args, overseas, china, smoke)
            expected_expiration = actual_expiration if returned_expiration else created_at + timedelta(days=90)
            assert result["expires_at"] == expected_expiration.isoformat()
            tags = {t["Key"]: t["Value"] for t in secrets.create_secret.call_args.kwargs["Tags"]}
            assert tags["ExpiresAt"] == expected_expiration.isoformat()
            smoke.assert_called_once_with(TEST_BEARER, args.model_region)
            assert json.loads(secrets.create_secret.call_args.kwargs["SecretString"]) == {
                "token": TEST_BEARER}
            assert "never-print" not in json.dumps(result)
        else:
            with pytest.raises(module.CredentialValidationError) as error:
                module.provision(args, overseas, china, smoke)
            assert error.value.diagnostics == {
                "stage": "credential_token_validation", "originating_error_type": "KeyError",
                "revocation_succeeded": True,
            }
            smoke.assert_not_called()
            secrets.create_secret.assert_not_called()
            iam.delete_service_specific_credential.assert_called_once()
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("scenario,request_count,sleep_count", [
    ("401_then_success", 2, 1),
    ("double_401", 2, 1),
    ("403", 1, 0),
])
def test_smoke_bounded_auth_retry_reuses_raw_bearer(
    monkeypatch, capsys, caplog, scenario, request_count, sleep_count,
):
    import openai

    raw_token = "ABSK" + "x" * 136
    assert module.bearer_token({
        "ServiceCredentialAlias": "offline-public-alias",
        "ServiceCredentialSecret": raw_token,
    }) == raw_token
    client = MagicMock()
    client.__enter__.return_value = client
    factory = Mock(return_value=client)
    monkeypatch.setattr(openai, "OpenAI", factory)
    # A retry must not issue a replacement credential through a new AWS session.
    aws_session = Mock(side_effect=AssertionError("Unexpected AWS session"))
    monkeypatch.setattr(module.boto3, "Session", aws_session)
    create = client.chat.completions.create

    def sleep(seconds):
        assert seconds == 8
        assert create.call_count == 1

    pause = Mock(side_effect=sleep)
    monkeypatch.setattr(module.time, "sleep", pause)

    def api_error(status):
        error_class = openai.AuthenticationError if status == 401 else openai.PermissionDeniedError
        return error_class(
            "offline synthetic failure",
            response=httpx.Response(status, request=httpx.Request("POST", "https://offline.invalid")),
            body={"error": "offline synthetic failure"},
        )

    if scenario == "401_then_success":
        create.side_effect = [
            api_error(401),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"status":"ok"}'))]),
        ]
        module.smoke_token(raw_token, "us-east-1")
    else:
        final_error = api_error(401 if scenario == "double_401" else 403)
        create.side_effect = [api_error(401), final_error] if scenario == "double_401" else [final_error]
        with pytest.raises(type(final_error)) as raised:
            module.smoke_token(raw_token, "us-east-1")
        assert raised.value is final_error
    factory.assert_called_once_with(
        api_key=raw_token, base_url="https://bedrock-mantle.us-east-1.api.aws/v1",
        timeout=45, max_retries=0,
    )
    assert create.call_count == request_count
    assert pause.call_count == sleep_count
    if sleep_count:
        pause.assert_called_once_with(8)
        assert create.call_args_list[0] == create.call_args_list[1]
    client.__exit__.assert_called_once()
    aws_session.assert_not_called()
    captured = capsys.readouterr()
    assert raw_token not in captured.out + captured.err + caplog.text
