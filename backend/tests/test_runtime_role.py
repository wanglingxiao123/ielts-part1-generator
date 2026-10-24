"""Offline provisioning contract: all AWS clients are strict local mocks."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from deploy import runtime_role as role


ACCOUNT = "123456789012"
REGION = "cn-northwest-1"
SECRET = f"arn:aws-cn:secretsmanager:{REGION}:{ACCOUNT}:secret:ielts/model-Ab12Cd"
ARGS = [
    "--profile", "china-review", "--expected-account", ACCOUNT,
    "--region", REGION, "--secret-arn", SECRET,
]
SETTINGS = role.Settings("china-review", ACCOUNT, REGION, SECRET, f"ielts-part1-materials-{ACCOUNT}")


class MissingRole(ClientError):
    def __init__(self):
        super().__init__({"Error": {"Code": "NoSuchEntity", "Message": "private detail"}}, "GetRole")


@pytest.fixture
def aws(monkeypatch):
    sts = MagicMock(spec=["get_caller_identity"])
    sts.get_caller_identity.return_value = {
        "Account": ACCOUNT, "Arn": f"arn:aws-cn:sts::{ACCOUNT}:assumed-role/operator/session",
    }
    secret = MagicMock(spec=["describe_secret"])
    secret.describe_secret.return_value = {
        "ARN": SECRET, "VersionIdsToStages": {"fake-version": ["AWSCURRENT"]},
        "Tags": [{"Key": "ManagedBy", "Value": "ielts-part1-china"}],
    }
    iam = MagicMock(spec=[
        "get_role", "get_role_policy", "get_paginator", "create_role", "put_role_policy", "exceptions",
    ])
    iam.exceptions = SimpleNamespace(NoSuchEntityException=MissingRole)
    iam.get_role.side_effect = MissingRole()
    pages = {
        "list_role_tags": [{"Tags": [
            {"Key": key, "Value": value} for key, value in role.OWNERSHIP_TAGS.items()
        ]}],
        "list_attached_role_policies": [{"AttachedPolicies": []}],
        "list_role_policies": [{"PolicyNames": []}],
    }

    def paginator(name):
        result = MagicMock(spec=["paginate"])
        result.paginate.return_value = pages[name]
        return result

    iam.get_paginator.side_effect = paginator
    session = MagicMock(spec=["client"])
    clients = {"sts": sts, "secretsmanager": secret, "iam": iam}
    session.client.side_effect = lambda name, **kwargs: clients[name]
    factory = MagicMock(return_value=session)
    monkeypatch.setattr(role.boto3, "Session", factory)
    return SimpleNamespace(
        sts=sts, secret=secret, iam=iam, pages=pages, session=session, factory=factory,
    )


def owned(aws, policy=None):
    aws.iam.get_role.side_effect = None
    aws.iam.get_role.return_value = {"Role": {
        "Arn": SETTINGS.role_arn, "Path": "/", "AssumeRolePolicyDocument": role.trust_policy(SETTINGS),
    }}
    if policy is not None:
        aws.pages["list_role_policies"] = [{"PolicyNames": [role.POLICY_NAME]}]
        aws.iam.get_role_policy.return_value = {"PolicyDocument": policy}


def no_writes(aws):
    aws.iam.create_role.assert_not_called()
    aws.iam.put_role_policy.assert_not_called()


def test_default_dry_run_metadata_only_and_explicit_session(aws, capsys, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "must-not-copy-this")
    monkeypatch.setenv("IELTS_MODEL_PROFILE", "must-not-copy-profile")
    assert role.main(ARGS) == 0
    output = capsys.readouterr()
    plan = json.loads(output.out)
    assert plan["mode"] == "dry-run"
    assert plan["operation"] == "create"
    assert plan["role_arn"] == SETTINGS.role_arn
    assert "must-not-copy" not in output.out
    aws.factory.assert_called_once_with(profile_name="china-review", region_name=REGION)
    aws.secret.describe_secret.assert_called_once_with(SecretId=SECRET)
    assert [call.args[0] for call in aws.session.client.call_args_list] == [
        "sts", "secretsmanager", "iam",
    ]
    assert all(call.kwargs["config"].ignore_configured_endpoint_urls
               for call in aws.session.client.call_args_list)
    no_writes(aws)


@pytest.mark.parametrize("region", role.REGIONS)
def test_policy_resource_boundaries(region):
    s = role.Settings("china", ACCOUNT, region, SECRET.replace(REGION, region), SETTINGS.bucket)
    statements = {entry["Sid"]: entry for entry in role.permissions(s)["Statement"]}
    assert statements["BackendImage"]["Resource"] == (
        f"arn:aws-cn:ecr:{region}:{ACCOUNT}:repository/ielts-part1-backend"
    )
    assert statements["ModelCredential"]["Resource"] == s.secret_arn
    assert statements["ModelCredential"]["Action"] == ["secretsmanager:GetSecretValue"]
    assert statements["BucketObjects"]["Resource"] == f"arn:aws-cn:s3:::{s.bucket}/*"
    assert statements["BucketList"]["Resource"] == f"arn:aws-cn:s3:::{s.bucket}"
    assert statements["RuntimeLogStreams"]["Resource"] == (
        f"arn:aws-cn:logs:{region}:{ACCOUNT}:log-group:"
        "/aws/bedrock-agentcore/runtimes/ielts_part1_runtime-*:log-stream:*"
    )
    assert statements["AuditSandbox"]["Resource"] == (
        f"arn:aws-cn:bedrock-agentcore:{region}:aws:code-interpreter/aws.codeinterpreter.v1"
    )
    assert set(statements["AuditSandbox"]["Action"]) == {
        "bedrock-agentcore:StartCodeInterpreterSession", "bedrock-agentcore:InvokeCodeInterpreter",
        "bedrock-agentcore:StopCodeInterpreterSession", "bedrock-agentcore:GetCodeInterpreterSession",
    }
    assert statements["RuntimeMetrics"]["Condition"] == {
        "StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"},
    }
    assert statements["SpeechSynthesis"]["Action"] == [
        "polly:SynthesizeSpeech", "polly:DescribeVoices",
    ]
    assert statements["SpeechSynthesis"]["Condition"] == {
        "StringEquals": {"aws:RequestedRegion": region},
    }
    wildcard_sids = {sid for sid, entry in statements.items() if entry["Resource"] == "*"}
    assert wildcard_sids == {
        "RegistryToken", "LogDiscovery", "RuntimeMetrics", "RuntimeTraces", "SpeechSynthesis",
    }
    for entry in statements.values():
        assert not any(action.startswith(("bedrock:", "bedrock-mantle:", "iam:", "kms:"))
                       for action in entry["Action"])
        assert not any("*" in action for action in entry["Action"])
    trust = role.trust_policy(s)["Statement"]
    assert len(trust) == 1
    assert trust[0]["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert trust[0]["Condition"] == {
        "StringEquals": {"aws:SourceAccount": ACCOUNT},
        "ArnLike": {"aws:SourceArn": f"arn:aws-cn:bedrock-agentcore:{region}:{ACCOUNT}:*"},
    }
    env = role.runtime_environment(s)
    assert env["AWS_REGION"] == env["IELTS_CODE_INTERPRETER_REGION"] == region
    assert env["IELTS_POLLY_PRESET"] == "china-standard"
    assert env["IELTS_MODEL_REGION"] == "us-east-1"
    assert env["IELTS_MODEL_AUTH"] == "secret"
    assert env["IELTS_MODEL_SECRET_ARN"] == s.secret_arn
    assert env["IELTS_MODEL_ID"] == "moonshotai.kimi-k2.5"
    assert env["IELTS_ALLOWED_MODEL_IDS"].split(",") == [
        "moonshotai.kimi-k2.5", "us.moonshotai.kimi-k3",
    ]


@pytest.mark.parametrize(("flag", "value"), [
    ("--profile", " "), ("--expected-account", "123"),
    ("--region", "us-east-1"), ("--secret-arn", SECRET.replace("aws-cn", "aws")),
    ("--secret-arn", SECRET.replace(REGION, "cn-north-1")),
    ("--secret-arn", SECRET.replace(ACCOUNT, "999999999999")),
    ("--secret-arn", SECRET[:-7]), ("--secret-arn", SECRET + "*"),
    ("--secret-arn", "plaintext-secret"), ("--bucket", "bucket/*"),
    ("--bucket", "invalid..bucket"), ("--bucket", "127.0.0.1"),
])
def test_bad_arguments_fail_before_session(aws, capsys, flag, value):
    argv = list(ARGS)
    if flag in argv:
        argv[argv.index(flag) + 1] = value
    else:
        argv += [flag, value]
    assert role.main(argv + ["--apply"]) == 1
    aws.factory.assert_not_called()
    assert capsys.readouterr().out == ""


def test_parser_never_echoes_unknown_or_missing_argument(aws, capsys):
    assert role.main(["--plaintext-secret"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {"error": "PreflightError", "code": "InvalidArguments"}
    assert output.out == ""
    aws.factory.assert_not_called()


@pytest.mark.parametrize("identity", [
    {"Account": "999999999999", "Arn": f"arn:aws-cn:iam::{ACCOUNT}:user/operator"},
    {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/operator"},
    {"Account": ACCOUNT, "Arn": "arn:aws-cn:sts::999999999999:assumed-role/operator/session"},
])
def test_identity_mismatch_before_secret_or_iam(aws, identity, capsys):
    aws.sts.get_caller_identity.return_value = identity
    assert role.main(ARGS + ["--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "CallerIdentityMismatch"
    aws.secret.describe_secret.assert_not_called()
    aws.iam.get_role.assert_not_called()
    no_writes(aws)


@pytest.mark.parametrize(("patch", "code"), [
    ({"ARN": SECRET + "wrong"}, "SecretIdentityMismatch"),
    ({"Tags": []}, "SecretOwnershipConflict"),
    ({"Tags": [{"Key": "ManagedBy", "Value": "foreign-tool"}]}, "SecretOwnershipConflict"),
    ({"DeletedDate": "scheduled"}, "SecretPendingDeletion"),
    ({"VersionIdsToStages": {}}, "SecretCurrentVersionMissing"),
    ({"VersionIdsToStages": {"v": ["AWSPREVIOUS"]}}, "SecretCurrentVersionMissing"),
    ({"KmsKeyId": "customer-key"}, "SecretKmsReviewRequired"),
])
def test_secret_metadata_conflicts_fail_before_iam(aws, capsys, patch, code):
    aws.secret.describe_secret.return_value.update(patch)
    assert role.main(ARGS + ["--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == code
    aws.iam.get_role.assert_not_called()
    no_writes(aws)


@pytest.mark.parametrize("conflict", [
    "untagged", "wrong_owner", "extra_trust", "cn_principal", "wrong_source",
    "wrong_arn", "path", "boundary", "attached", "inline",
])
@pytest.mark.parametrize("apply", [False, True])
def test_existing_role_conflicts_are_never_adopted(aws, capsys, conflict, apply):
    owned(aws)
    existing = aws.iam.get_role.return_value["Role"]
    statement = existing["AssumeRolePolicyDocument"]["Statement"][0]
    if conflict == "untagged":
        aws.pages["list_role_tags"] = [{"Tags": []}]
    elif conflict == "wrong_owner":
        aws.pages["list_role_tags"][0]["Tags"][1]["Value"] = "other-tool"
    elif conflict == "extra_trust":
        existing["AssumeRolePolicyDocument"]["Statement"].append(copy.deepcopy(statement))
    elif conflict == "cn_principal":
        statement["Principal"]["Service"] += ".cn"
    elif conflict == "wrong_source":
        statement["Condition"]["ArnLike"]["aws:SourceArn"] = "*"
    elif conflict == "wrong_arn":
        existing["Arn"] += "-other"
    elif conflict == "path":
        existing["Path"] = "/service-role/"
    elif conflict == "boundary":
        existing["PermissionsBoundary"] = {"PermissionsBoundaryArn": "boundary"}
    elif conflict == "attached":
        aws.pages["list_attached_role_policies"].append(
            {"AttachedPolicies": [{"PolicyName": "AdministratorAccess"}]}
        )
    elif conflict == "inline":
        aws.pages["list_role_policies"].append({"PolicyNames": ["foreign-policy"]})
    assert role.main(ARGS + (["--apply"] if apply else [])) == 1
    assert json.loads(capsys.readouterr().err)["code"].startswith("Role")
    no_writes(aws)


def test_apply_create_tags_and_exact_policy_only(aws, capsys):
    assert role.main(ARGS + ["--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "create"
    aws.iam.create_role.assert_called_once()
    call = aws.iam.create_role.call_args.kwargs
    assert call["RoleName"] == role.ROLE_NAME
    assert {tag["Key"]: tag["Value"] for tag in call["Tags"]} == role.OWNERSHIP_TAGS
    assert json.loads(call["AssumeRolePolicyDocument"]) == role.trust_policy(SETTINGS)
    aws.iam.put_role_policy.assert_called_once()
    call = aws.iam.put_role_policy.call_args.kwargs
    assert call["RoleName"] == role.ROLE_NAME
    assert call["PolicyName"] == role.POLICY_NAME
    assert json.loads(call["PolicyDocument"]) == role.permissions(SETTINGS)


@pytest.mark.parametrize("apply", [False, True])
def test_owned_update_and_dry_run(aws, capsys, apply):
    owned(aws, {"Version": "2012-10-17", "Statement": []})
    assert role.main(ARGS + (["--apply"] if apply else [])) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "update"
    aws.iam.create_role.assert_not_called()
    assert aws.iam.put_role_policy.call_count == int(apply)


def test_encoded_trust_and_paginated_ownership_noop(aws, capsys):
    owned(aws, role.permissions(SETTINGS))
    existing = aws.iam.get_role.return_value["Role"]
    existing["AssumeRolePolicyDocument"] = quote(json.dumps(role.trust_policy(SETTINGS)))
    tags = aws.pages["list_role_tags"][0]["Tags"]
    aws.pages["list_role_tags"] = [{"Tags": tags[:1]}, {"Tags": tags[1:]}]
    assert role.main(ARGS + ["--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "noop"
    no_writes(aws)


def test_role_appearing_after_plan_fails_closed(aws, capsys):
    owned(aws)
    existing = aws.iam.get_role.return_value
    aws.iam.get_role.side_effect = [MissingRole(), existing]
    assert role.main(ARGS + ["--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "RoleChangedDuringPreflight"
    no_writes(aws)


@pytest.mark.parametrize("stage", ["sts", "secret", "get_role", "create", "put"])
def test_access_denied_is_not_absence_and_messages_are_redacted(aws, capsys, stage):
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "plaintext-model-token"},
         "ResponseMetadata": {"RequestId": "private-request-id"}},
        "Operation",
    )
    methods = {
        "sts": aws.sts.get_caller_identity, "secret": aws.secret.describe_secret,
        "get_role": aws.iam.get_role, "create": aws.iam.create_role, "put": aws.iam.put_role_policy,
    }
    methods[stage].side_effect = error
    assert role.main(ARGS + ["--apply"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "ClientError", "code": "AccessDenied"}
    if stage in ("sts", "secret", "get_role"):
        no_writes(aws)
    if stage == "create":
        aws.iam.put_role_policy.assert_not_called()


@pytest.mark.parametrize("error", [
    RuntimeError("plaintext-model-token"), NoCredentialsError(),
    ClientError({"Error": {"Code": "token\nprivate-value", "Message": "secret"}}, "operation"),
])
def test_other_errors_are_type_or_safe_code_only(aws, capsys, error):
    aws.factory.side_effect = error
    assert role.main(ARGS) == 1
    output = capsys.readouterr()
    assert output.out == ""
    diagnostic = json.loads(output.err)
    assert diagnostic["error"] == type(error).__name__
    assert set(diagnostic) <= {"error", "code"}
    assert "token" not in output.err
    assert "private-value" not in output.err
    assert "secret" not in output.err
