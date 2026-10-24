"""No Docker daemon or AWS requests: verify immutable push boundaries."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deploy import push_image as m

ACCOUNT = "123456789012"
REGISTRY = f"{ACCOUNT}.dkr.ecr.cn-northwest-1.amazonaws.com.cn"


@pytest.fixture
def setup(monkeypatch):
    args = SimpleNamespace(profile="offline", account=ACCOUNT, region="cn-northwest-1",
                           repository="ielts-part1-backend", tag="release-test",
                           local_image="local-backend", apply=False)
    sts, ecr = Mock(), Mock()
    sts.get_caller_identity.return_value = {
        "Account": ACCOUNT, "Arn": f"arn:aws-cn:iam::{ACCOUNT}:user/offline"}
    ecr.describe_repositories.return_value = {"repositories": [{
        "imageTagMutability": "IMMUTABLE", "repositoryUri": f"{REGISTRY}/{args.repository}"}]}
    class ImageNotFound(Exception):
        pass
    ecr.exceptions.ImageNotFoundException = ImageNotFound
    ecr.describe_images.side_effect = ImageNotFound()
    session = Mock()
    session.client.side_effect = lambda service, **kwargs: {"sts": sts, "ecr": ecr}[service]
    session_factory = Mock(return_value=session)
    monkeypatch.setattr(m.boto3, "Session", session_factory)
    local = {"Architecture": "arm64", "Os": "linux", "Size": 1000, "Id": "sha256:local-id"}
    run = Mock(return_value=SimpleNamespace(stdout=json.dumps([local])))
    monkeypatch.setattr(m.subprocess, "run", run)
    return args, ecr, run, session_factory, local


def test_dryrun_does_not_login_tag_or_push(setup):
    args, ecr, run, _, _ = setup
    assert m.push(args)["mode"] == "dry-run"
    assert run.call_count == 1
    assert run.call_args.args[0][:3] == ["docker", "image", "inspect"]
    ecr.get_authorization_token.assert_not_called()


@pytest.mark.parametrize("profile", ["", "  "])
def test_profile_required_before_session(setup, profile):
    args, _, _, session_factory, _ = setup
    args.profile = profile
    with pytest.raises(ValueError, match="profile"):
        m.push(args)
    session_factory.assert_not_called()


def test_existing_tag_never_pushes(setup):
    args, ecr, run, _, _ = setup
    args.apply = True
    ecr.describe_images.side_effect = None
    with pytest.raises(ValueError, match="already exists"):
        m.push(args)
    run.assert_not_called()
    ecr.get_authorization_token.assert_not_called()


def test_access_denied_is_not_treated_as_missing_image(setup):
    args, ecr, run, _, _ = setup
    ecr.describe_images.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        m.push(args)
    run.assert_not_called()


@pytest.mark.parametrize("field,value", [("Architecture", "amd64"), ("Os", "windows"),
                                         ("Size", 2_000_000_001)])
def test_invalid_local_image_never_authenticates(setup, field, value):
    args, ecr, run, _, local = setup
    args.apply = True
    local[field] = value
    run.return_value.stdout = json.dumps([local])
    with pytest.raises(ValueError):
        m.push(args)
    ecr.get_authorization_token.assert_not_called()


@pytest.mark.parametrize("push_fails", [False, True])
@pytest.mark.parametrize("context", ["default", "desktop-linux"])
def test_apply_stdin_auth_pinned_source_and_cleanup(setup, push_fails, context):
    args, ecr, run, _, _ = setup
    args.apply = True
    digest = "sha256:" + "a" * 64
    ecr.describe_images.side_effect = [ecr.exceptions.ImageNotFoundException(),
                                     {"imageDetails": [{"imageDigest": digest}]}]
    password = "synthetic-secret"
    ecr.get_authorization_token.return_value = {"authorizationData": [{
        "proxyEndpoint": f"https://{REGISTRY}",
        "authorizationToken": base64.b64encode(f"AWS:{password}".encode()).decode(),
    }]}
    inspect_result = run.return_value
    calls = []
    def invoke(command, **kwargs):
        calls.append((command, kwargs))
        assert password not in " ".join(command)
        if "login" in command or "push" in command:
            assert Path(command[2]).is_dir()
        if "push" in command and push_fails:
            raise m.subprocess.CalledProcessError(1, command, stderr=password)
        if command == ["docker", "context", "show"]:
            return SimpleNamespace(stdout=context + "\n")
        if command[:3] == ["docker", "context", "export"]:
            return SimpleNamespace(stdout=b"synthetic-context-export")
        return inspect_result
    run.side_effect = invoke
    if push_fails:
        with pytest.raises(m.subprocess.CalledProcessError):
            m.push(args)
    else:
        report = m.push(args)
        assert report["pinned_image"] == f"{REGISTRY}/{args.repository}@{digest}"
        assert password not in str(report)
    login, login_kwargs = next(call for call in calls if "login" in call[0])
    assert "--password-stdin" in login
    assert login_kwargs["input"] == password
    tag = next(command for command, _ in calls if "tag" in command)
    assert tag[0:3] == ["docker", "tag", "sha256:local-id"]
    push = next(command for command, _ in calls if "push" in command)
    if context != "default":
        assert push[3:5] == ["--context", context]
        imported = next(call for call in calls if "import" in call[0])
        assert imported[1]["input"] == b"synthetic-context-export"
    assert not Path(login[2]).exists()
