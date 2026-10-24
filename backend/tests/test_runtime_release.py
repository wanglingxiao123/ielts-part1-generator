"""Offline release safety and the actual AgentCore SDK request schema."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from deploy import runtime_release as m

ACCOUNT = "123456789012"
ARN = f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:{ACCOUNT}:runtime/ielts_part1_runtime-abcdefghij"
IMAGE = f"{ACCOUNT}.dkr.ecr.cn-northwest-1.amazonaws.com.cn/ielts-part1-backend@sha256:" + "a" * 64
SECRET = f"arn:aws-cn:secretsmanager:cn-northwest-1:{ACCOUNT}:secret:ielts-part1/overseas-model-abcdef"


@pytest.fixture
def setup(monkeypatch):
    args = SimpleNamespace(profile="offline", expected_account=ACCOUNT, region="cn-northwest-1",
                           secret_arn=SECRET, image=IMAGE, bucket=None, apply=False)
    role = Mock(return_value={"operation": "noop"})
    monkeypatch.setattr(m.runtime_role, "provision", role)
    ecr, secrets, control = Mock(), Mock(), Mock()
    ecr.describe_repositories.return_value = {"repositories": [{
        "imageTagMutability": "IMMUTABLE", "repositoryUri": IMAGE.split("@")[0],
    }]}
    ecr.describe_images.return_value = {"imageDetails": [{
        "imageDigest": "sha256:" + "a" * 64, "imageSizeInBytes": 1000,
    }]}
    secrets.describe_secret.return_value = {"Tags": [{
        "Key": "ExpiresAt", "Value": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }]}
    control.get_paginator.return_value.paginate.return_value = [{"agentRuntimes": []}]
    session = Mock()
    session.client.side_effect = lambda service, **kwargs: {
        "ecr": ecr, "secretsmanager": secrets, "bedrock-agentcore-control": control,
    }[service]
    monkeypatch.setattr(m.boto3, "Session", Mock(return_value=session))
    return args, role, ecr, secrets, control, session


def test_dry_run_is_secret_reference_only(setup, monkeypatch):
    args, _, _, secrets, control, _ = setup
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "private-token")
    report = m.release(args)
    assert report["operation"] == "create"
    env = report["configuration"]["environmentVariables"]
    assert env["IELTS_MODEL_SECRET_ARN"] == SECRET
    assert env["IELTS_MODEL_REGION"] == "us-east-1"
    assert env["AWS_REGION"] == "cn-northwest-1"
    assert "private-token" not in str(report)
    control.create_agent_runtime.assert_not_called()
    secrets.get_secret_value.assert_not_called()


@pytest.mark.parametrize("image", [
    IMAGE.replace("@sha256:" + "a" * 64, ":latest"),
    IMAGE.replace("amazonaws.com.cn", "amazonaws.com"),
    IMAGE.replace(ACCOUNT, "999999999999"),
])
def test_wrong_image_fails_before_aws(setup, image):
    args, role, *_ = setup
    args.image = image
    with pytest.raises(m.runtime_role.PreflightError, match="ImageDigest"):
        m.release(args)
    role.assert_not_called()


def test_role_must_already_match(setup):
    args, role, *_ = setup
    role.return_value = {"operation": "update"}
    with pytest.raises(m.runtime_role.PreflightError, match="ProvisionedFirst"):
        m.release(args)


def test_expired_credential_prevents_publish(setup):
    args, _, _, secrets, control, _ = setup
    args.apply = True
    secrets.describe_secret.return_value = {"Tags": [{"Key": "ExpiresAt", "Value": "2020-01-01T00:00:00Z"}]}
    with pytest.raises(m.runtime_role.PreflightError, match="Expiry"):
        m.release(args)
    control.create_agent_runtime.assert_not_called()


def test_unowned_runtime_is_not_adopted(setup):
    args, _, _, _, control, _ = setup
    args.apply = True
    control.get_paginator.return_value.paginate.return_value = [{"agentRuntimes": [{
        "agentRuntimeName": m.NAME, "agentRuntimeArn": ARN, "agentRuntimeId": ARN.split("/")[-1],
    }]}]
    control.list_tags_for_resource.return_value = {"tags": {}}
    with pytest.raises(m.runtime_role.PreflightError, match="Ownership"):
        m.release(args)
    control.update_agent_runtime.assert_not_called()


def test_create_request_matches_real_sdk(setup, monkeypatch):
    args, _, _, _, control, _ = setup
    args.apply = True
    # A real botocore model validates the request; it must not reach the network.
    import botocore.session
    real = botocore.session.get_session().create_client(
        "bedrock-agentcore-control", region_name=args.region,
        aws_access_key_id="offline", aws_secret_access_key="offline",
    )
    response = {"agentRuntimeArn": ARN, "agentRuntimeId": ARN.split("/")[-1],
                "agentRuntimeVersion": "1", "status": "CREATING",
                "createdAt": datetime.now(timezone.utc)}
    with Stubber(real) as stub:
        stub.add_response("create_agent_runtime", response)
        control.create_agent_runtime.side_effect = real.create_agent_runtime
        report = m.release(args)
        assert report["status"] == "CREATING"
        assert report["runtime_arn"] == ARN
        assert report["configuration"]["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == IMAGE
        stub.assert_no_pending_responses()
    real.close()


def owned_runtime(setup):
    args, _, _, _, control, _ = setup
    desired = m.release(args)["configuration"]
    current = dict(desired, agentRuntimeArn=ARN, agentRuntimeId=ARN.split("/")[-1],
                   agentRuntimeVersion="2", status="READY")
    control.get_paginator.return_value.paginate.return_value = [{"agentRuntimes": [{
        "agentRuntimeName": m.NAME, "agentRuntimeArn": ARN,
        "agentRuntimeId": current["agentRuntimeId"],
    }]}]
    control.list_tags_for_resource.return_value = {"tags": {"ManagedBy": m.OWNER}}
    control.get_agent_runtime.return_value = current
    return current


def test_matching_owned_runtime_is_noop(setup):
    current = owned_runtime(setup)
    args, role, _, _, control, _ = setup
    args.apply = True
    assert m.release(args)["operation"] == "noop"
    control.update_agent_runtime.assert_not_called()
    assert all(not call.kwargs.get("apply", False) for call in role.call_args_list)


@pytest.mark.parametrize("field,value", [
    ("status", "UPDATING"),
    ("authorizerConfiguration", {"customJWTAuthorizer": {}}),
    ("requestHeaderConfiguration", {"requestHeaderAllowlist": ["Authorization"]}),
    ("filesystemConfigurations", [{"mountPath": "/data"}]),
])
def test_conflicting_existing_runtime_prevents_update(setup, field, value):
    current = owned_runtime(setup)
    current[field] = value
    args, _, _, _, control, _ = setup
    args.apply = True
    with pytest.raises(m.runtime_role.PreflightError):
        m.release(args)
    control.update_agent_runtime.assert_not_called()


def test_update_sdk_contract_and_new_token_for_later_rollback(setup):
    current = owned_runtime(setup)
    current["agentRuntimeArtifact"] = {"containerConfiguration": {
        "containerUri": IMAGE.replace("a" * 64, "b" * 64)}}
    args, _, _, _, control, _ = setup
    args.apply = True
    import botocore.session
    real = botocore.session.get_session().create_client(
        "bedrock-agentcore-control", region_name=args.region,
        aws_access_key_id="offline", aws_secret_access_key="offline",
    )
    response = {"agentRuntimeArn": ARN, "agentRuntimeId": current["agentRuntimeId"],
                "agentRuntimeVersion": "3", "status": "UPDATING",
                "createdAt": datetime.now(timezone.utc),
                "lastUpdatedAt": datetime.now(timezone.utc)}
    try:
        with Stubber(real) as stub:
            for _ in range(3):
                stub.add_response("update_agent_runtime", response)
            control.update_agent_runtime.side_effect = real.update_agent_runtime
            m.release(args)
            first = control.update_agent_runtime.call_args.kwargs["clientToken"]
            m.release(args)
            assert control.update_agent_runtime.call_args.kwargs["clientToken"] == first
            current["agentRuntimeVersion"] = "4"
            m.release(args)
            assert control.update_agent_runtime.call_args.kwargs["clientToken"] != first
            stub.assert_no_pending_responses()
    finally:
        real.close()
