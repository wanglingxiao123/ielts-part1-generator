"""Offline K3 authorization policy and ownership contracts."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from botocore.exceptions import ClientError
from botocore.session import Session
from botocore.validate import validate_parameters

from deploy import model_access as access

ACCOUNT = "123456789012"
USER = access.model_credentials.USER
ARGS = ["--overseas-profile", "overseas-test", "--overseas-account", ACCOUNT]


@pytest.fixture
def aws(monkeypatch):
    sts = Mock(spec_set=["get_caller_identity", "close"])
    sts.get_caller_identity.return_value = {
        "Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/operator/session",
    }
    iam = Mock(spec_set=["get_user", "get_paginator", "get_user_policy", "put_user_policy", "close"])
    iam.get_user.return_value = {"User": {
        "Arn": f"arn:aws:iam::{ACCOUNT}:user/{USER}", "Path": "/", "UserId": "synthetic-user-id",
    }}
    pages = {
        "list_user_tags": [{"Tags": [{"Key": "ManagedBy", "Value": "ielts-part1-china"}]}],
        "list_attached_user_policies": [{"AttachedPolicies": []}],
        "list_groups_for_user": [{"Groups": []}],
        "list_user_policies": [{"PolicyNames": [access.BASE_POLICY]}],
    }
    policies = {access.BASE_POLICY: access.model_credentials.inference_policy("us-east-1")}
    models = Session()
    # Loading SDK schemas must not consult the operator's ambient profile.
    monkeypatch.setattr(models, "get_scoped_config", lambda: {})

    def paginate(method):
        model = models.get_paginator_model("iam")
        operation = "".join(word.title() for word in method.split("_"))
        assert operation in model._paginator_config

        def pages_for(**kwargs):
            assert kwargs == {"UserName": USER}
            return pages[method]

        return SimpleNamespace(paginate=pages_for)

    iam.get_paginator.side_effect = paginate
    iam.get_user_policy.side_effect = lambda **kw: {"PolicyDocument": policies[kw["PolicyName"]]}
    session = Mock(spec_set=["client"])
    session.client.side_effect = lambda service, **kw: {"sts": sts, "iam": iam}[service]
    factory = Mock(return_value=session)
    monkeypatch.setattr(access.boto3, "Session", factory)
    return SimpleNamespace(
        sts=sts, iam=iam, factory=factory, session=session, pages=pages, policies=policies,
    )


def test_default_dry_run_preserves_mantle_and_uses_explicit_session(aws, capsys, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "ambient-profile")
    assert access.main(ARGS) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry-run"
    assert result["operation"] == "create"
    assert result["preserved_policy"] == access.BASE_POLICY
    assert result["policy"] == access.policy(ACCOUNT)
    aws.factory.assert_called_once_with(profile_name="overseas-test", region_name="us-east-1")
    assert [call.args[0] for call in aws.session.client.call_args_list] == ["sts", "iam"]
    assert all(call.kwargs["config"].ignore_configured_endpoint_urls
               for call in aws.session.client.call_args_list)
    aws.iam.put_user_policy.assert_not_called()
    aws.iam.close.assert_called_once_with()
    aws.sts.close.assert_called_once_with()
    import os
    assert os.environ["AWS_PROFILE"] == "ambient-profile"


def test_policy_has_only_k3_resources_and_regional_bearer_permission():
    statements = access.policy(ACCOUNT)["Statement"]
    assert len(statements) == 2
    assert statements[0]["Action"] == ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    assert set(statements[0]["Resource"]) == {
        f"arn:aws:bedrock:us-east-1:{ACCOUNT}:inference-profile/us.moonshotai.kimi-k3",
        "arn:aws:bedrock:us-east-1::foundation-model/moonshotai.kimi-k3",
        "arn:aws:bedrock:us-east-2::foundation-model/moonshotai.kimi-k3",
        "arn:aws:bedrock:us-west-2::foundation-model/moonshotai.kimi-k3",
    }
    assert all("*" not in arn for arn in statements[0]["Resource"])
    assert statements[1] == {
        "Sid": "K3BearerEntryRegion", "Effect": "Allow",
        "Action": ["bedrock:CallWithBearerToken"], "Resource": "*",
        "Condition": {"StringEquals": {"aws:RequestedRegion": "us-east-1"}},
    }


def test_apply_only_creates_separate_inline_policy_with_real_sdk_parameters(aws, capsys):
    original = copy.deepcopy(aws.policies)
    assert access.main([*ARGS, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "create"
    aws.iam.put_user_policy.assert_called_once()
    kwargs = aws.iam.put_user_policy.call_args.kwargs
    validate_parameters(kwargs, Session().get_service_model("iam").operation_model("PutUserPolicy").input_shape)
    assert kwargs["UserName"] == USER
    assert kwargs["PolicyName"] == access.K3_POLICY
    assert json.loads(kwargs["PolicyDocument"]) == access.policy(ACCOUNT)
    assert aws.policies == original
    assert aws.iam.get_user.call_count == 2


def test_exact_existing_k3_policy_is_noop_and_url_encoded_base_is_supported(aws, capsys):
    aws.pages["list_user_policies"].append({"PolicyNames": [access.K3_POLICY]})
    aws.policies[access.K3_POLICY] = access.policy(ACCOUNT)
    aws.policies[access.BASE_POLICY] = quote(json.dumps(aws.policies[access.BASE_POLICY]))
    assert access.main([*ARGS, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "noop"
    aws.iam.put_user_policy.assert_not_called()


def test_repeated_apply_creates_once_then_returns_noop(aws, capsys):
    original_mantle = copy.deepcopy(aws.policies[access.BASE_POLICY])

    def save_policy(**kwargs):
        assert kwargs["UserName"] == USER
        assert kwargs["PolicyName"] == access.K3_POLICY
        aws.policies[access.K3_POLICY] = json.loads(kwargs["PolicyDocument"])
        aws.pages["list_user_policies"].append({"PolicyNames": [access.K3_POLICY]})
        return {}

    aws.iam.put_user_policy.side_effect = save_policy
    assert access.main([*ARGS, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "create"
    assert access.main([*ARGS, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "noop"
    aws.iam.put_user_policy.assert_called_once()
    assert aws.policies[access.BASE_POLICY] == original_mantle


@pytest.mark.parametrize("problem", [
    "user_arn", "user_path", "unowned", "wrong_owner", "boundary", "managed", "group",
    "missing_base", "extra_inline", "mantle_action", "mantle_region", "k3_drift",
])
@pytest.mark.parametrize("apply", [False, True])
def test_user_and_policy_conflicts_fail_closed(aws, capsys, problem, apply):
    user = aws.iam.get_user.return_value["User"]
    if problem == "user_arn":
        user["Arn"] += "-foreign"
    elif problem == "user_path":
        user["Path"] = "/foreign/"
    elif problem == "unowned":
        aws.pages["list_user_tags"] = [{"Tags": []}]
    elif problem == "wrong_owner":
        aws.pages["list_user_tags"] = [{"Tags": [{"Key": "ManagedBy", "Value": "another-project"}]}]
    elif problem == "boundary":
        user["PermissionsBoundary"] = {"PermissionsBoundaryArn": "synthetic"}
    elif problem == "managed":
        aws.pages["list_attached_user_policies"].append({"AttachedPolicies": [{"PolicyName": "foreign"}]})
    elif problem == "group":
        aws.pages["list_groups_for_user"].append({"Groups": [{"GroupName": "foreign"}]})
    elif problem == "missing_base":
        aws.pages["list_user_policies"] = [{"PolicyNames": []}]
    elif problem == "extra_inline":
        aws.pages["list_user_policies"].append({"PolicyNames": ["foreign-policy"]})
    elif problem == "mantle_action":
        aws.policies[access.BASE_POLICY]["Statement"][0]["Action"].append("bedrock:*")
    elif problem == "mantle_region":
        aws.policies[access.BASE_POLICY]["Statement"][0]["Condition"]["StringEquals"]["aws:RequestedRegion"] = "*"
    else:
        aws.pages["list_user_policies"].append({"PolicyNames": [access.K3_POLICY]})
        aws.policies[access.K3_POLICY] = access.policy(ACCOUNT)
        aws.policies[access.K3_POLICY]["Statement"][0]["Resource"] = "*"
    assert access.main(ARGS + (["--apply"] if apply else [])) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "AccessConflict"
    aws.iam.put_user_policy.assert_not_called()


def test_user_replacement_before_apply_is_rejected(aws, capsys):
    first = aws.iam.get_user.return_value
    second = copy.deepcopy(first)
    second["User"]["UserId"] = "different-user-id"
    aws.iam.get_user.side_effect = [first, second]
    assert access.main([*ARGS, "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "UserChangedDuringPreflight"
    aws.iam.put_user_policy.assert_not_called()


@pytest.mark.parametrize("identity", [
    {"Account": "999999999999", "Arn": f"arn:aws:iam::{ACCOUNT}:user/operator"},
    {"Account": ACCOUNT, "Arn": f"arn:aws-cn:iam::{ACCOUNT}:user/operator"},
    {"Account": ACCOUNT, "Arn": "arn:aws:sts::999999999999:assumed-role/operator/session"},
])
def test_account_partition_isolation_precedes_iam(aws, capsys, identity):
    aws.sts.get_caller_identity.return_value = identity
    assert access.main([*ARGS, "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "CallerIdentityMismatch"
    aws.iam.get_user.assert_not_called()
    aws.iam.put_user_policy.assert_not_called()


@pytest.mark.parametrize("method,code", [
    ("get_user", "NoSuchEntity"), ("get_user", "AccessDenied"), ("get_user_policy", "AccessDenied"),
    ("put_user_policy", "AccessDenied"),
])
def test_aws_errors_never_mean_adoption_or_leak_messages(aws, capsys, method, code):
    getattr(aws.iam, method).side_effect = ClientError(
        {"Error": {"Code": code, "Message": "synthetic-private-credential"}}, "Operation",
    )
    assert access.main([*ARGS, "--apply"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "ClientError", "code": code}
    if method != "put_user_policy":
        aws.iam.put_user_policy.assert_not_called()
    aws.iam.close.assert_called_once()


@pytest.mark.parametrize("argv", [
    [], ["--unknown-private-argument"],
    ["--overseas-profile", " ", "--overseas-account", ACCOUNT],
    ["--overseas-profile", "overseas-test", "--overseas-account", "not-account"],
])
def test_invalid_arguments_fail_before_session(aws, capsys, argv):
    assert access.main(argv) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "AccessConflict"
    aws.factory.assert_not_called()


def test_credential_helper_exception_is_type_only(aws, capsys):
    aws.factory.side_effect = RuntimeError("synthetic-private-credential")
    assert access.main(ARGS) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "RuntimeError"}
