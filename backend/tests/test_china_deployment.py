"""Offline deployment contracts: every aws executable below is a local fake."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def shell_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "aws.jsonl"
    fake = fake_bin / "aws"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['FAKE_AWS_LOG'], 'a') as f:\n"
        "    f.write(json.dumps(args) + '\\n')\n"
        "if args[:2] == ['sts', 'get-caller-identity']:\n"
        "    query = args[args.index('--query') + 1] if '--query' in args else ''\n"
        "    if query == 'Account': print('123456789012')\n"
        "    elif query == 'Arn': print('arn:aws-cn:iam::123456789012:user/test')\n"
        "    else: print(json.dumps({'Account':'123456789012',"
        "'Arn':'arn:aws-cn:iam::123456789012:user/test'}))\n"
        "elif args[:2] == ['ecr', 'describe-images']:\n"
        "    print('already-present')\n"
        "else:\n"
        "    print('UNEXPECTED AWS CALL: ' + repr(args), file=sys.stderr)\n"
        "    sys.exit(97)\n"
    )
    fake.chmod(0o755)
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AWS_", "IELTS_", "ALB_", "WEB_", "SUBNET", "VPC_"))
        and key not in {"ACCOUNT_ID", "S3_BUCKET"}
    }
    env.update({
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_AWS_LOG": str(log),
        "ACCOUNT_ID": "123456789012",
        "VPC_ID": "vpc-test",
        "SUBNET_IDS": "subnet-a subnet-b",
        "AWS_EC2_METADATA_DISABLED": "true",
    })
    return env, log


def run_shell(env, script, *args):
    return subprocess.run(
        ["bash", script, *args], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=15,
    )


def calls(log):
    return [json.loads(row) for row in log.read_text().splitlines()] if log.exists() else []


def settings(env):
    return subprocess.run(
        ["bash", "-c", 'source deploy/settings.sh && printf "%s\\n" '
         '"$AWS_REGION" "$AWS_DEFAULT_REGION" "$AWS_PARTITION" "$AWS_DNS_SUFFIX" '
         '"$ALB_PUBLIC_PORT" "$WEB_PORT"'],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("region", ["cn-northwest-1", "cn-north-1"])
def test_china_settings_ignore_model_region(shell_env, region):
    env, log = shell_env
    env.update(AWS_REGION=region, IELTS_MODEL_REGION="us-east-1")
    result = settings(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        region, region, "aws-cn", "amazonaws.com.cn", "9000", "80",
    ]
    assert calls(log) == []


def test_default_and_cli_region_fallback(shell_env):
    env, _ = shell_env
    assert settings(env).stdout.splitlines()[0] == "cn-northwest-1"
    env["AWS_DEFAULT_REGION"] = "cn-north-1"
    assert settings(env).stdout.splitlines()[:2] == ["cn-north-1", "cn-north-1"]


@pytest.mark.parametrize("port", ["80", "443", "8080", "0", "65536", "abc", "-1"])
def test_invalid_public_port_fails_without_aws(shell_env, port):
    env, log = shell_env
    env["ALB_PUBLIC_PORT"] = port
    result = settings(env)
    assert result.returncode != 0
    assert calls(log) == []


def test_public_port_does_not_change_backend_port(shell_env):
    env, _ = shell_env
    env["ALB_PUBLIC_PORT"] = "19000"
    result = settings(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == ["19000", "80"]


@pytest.mark.parametrize("script", [
    "deploy/provision.sh", "deploy/edge.sh", "deploy/service.sh",
    "deploy/runtime.sh", "deploy/start.sh", "deploy/stop.sh",
    "deploy/teardown.sh",
])
def test_global_region_rejected_before_cloud_mutations(shell_env, script):
    env, log = shell_env
    env["AWS_REGION"] = "us-east-1"
    result = run_shell(env, script, "--yes" if "teardown" in script else "test-tag")
    assert result.returncode != 0
    assert all(row[:2] == ["sts", "get-caller-identity"] for row in calls(log))


@pytest.mark.parametrize("script,args", [
    ("deploy/runtime.sh", ["test-tag"]),
    ("deploy/probe-runtime.sh", []),
    ("deploy/rollback.sh", ["--runtime-image", "test-tag"]),
    ("deploy/rollback.sh", ["--runtime-version", "1"]),
])
def test_model_deployment_remains_pending(shell_env, script, args):
    env, log = shell_env
    # Even explicit credentials/region must not silently enable cross-border use.
    env.update(IELTS_MODEL_REGION="us-east-1", AWS_BEARER_TOKEN_BEDROCK="fake-not-a-secret")
    result = run_shell(env, script, *args)
    assert result.returncode != 0
    assert "model" in (result.stdout + result.stderr).lower()
    assert all(row[:2] == ["sts", "get-caller-identity"] for row in calls(log))
    assert "fake-not-a-secret" not in result.stdout + result.stderr


def test_image_build_checks_china_ecr_independent_of_model(shell_env):
    env, log = shell_env
    env.update(AWS_REGION="cn-northwest-1", IELTS_MODEL_REGION="us-east-1")
    result = run_shell(
        env, "backend/scripts/deploy.sh",
        "123456789012.dkr.ecr.cn-northwest-1.amazonaws.com.cn/ielts-part1-backend",
        "existing-tag",
    )
    # Existing tag must stop the script before Docker/login/push.
    assert result.returncode != 0
    ecr_calls = [row for row in calls(log) if row[:2] == ["ecr", "describe-images"]]
    assert ecr_calls
    assert all(row[row.index("--region") + 1] == "cn-northwest-1" for row in ecr_calls)
    assert all(row[:2] in (["ecr", "describe-images"], ["sts", "get-caller-identity"])
               for row in calls(log))


@pytest.fixture
def edge_env(shell_env, tmp_path):
    env, log = shell_env
    executable = Path(env["PATH"].split(os.pathsep)[0]) / "aws"
    source = (ROOT / "backend/tests/fixtures/china_fake_aws.py").read_text()
    executable.write_text(f"#!{sys.executable}\n" + source)
    state = tmp_path / "edge-state.json"
    env["FAKE_EDGE_STATE"] = str(state)
    return env, log, state


def test_edge_creation_and_repeat_preserve_public_private_boundary(edge_env):
    env, log, state_file = edge_env
    first = run_shell(env, "deploy/edge.sh")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "http://test-alb.cn-northwest-1.elb.amazonaws.com.cn:9000" in first.stdout
    state = json.loads(state_file.read_text())
    assert state["listeners"][0]["Port"] == 9000
    assert state["tg"]["Port"] == 80
    assert state["ielts-part1-alb-sg"]["IpPermissions"] == [{
        "IpProtocol": "tcp", "FromPort": 9000, "ToPort": 9000,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    }]
    assert state["ielts-part1-web-sg"]["IpPermissions"] == [{
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
        "UserIdGroupPairs": [{"GroupId": "sg-alb"}],
    }]
    before = len(calls(log))
    second = run_shell(env, "deploy/edge.sh")
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(state_file.read_text()) == state
    assert not any(row[1].startswith(("create-", "authorize-")) for row in calls(log)[before:])
    assert not any(row[0] == "cloudfront" for row in calls(log))


@pytest.mark.parametrize("conflict", ["public-task", "extra-listener", "extra-rule", "wide-alb"])
def test_existing_edge_conflicts_fail_before_mutations(edge_env, conflict):
    env, log, state_file = edge_env
    assert run_shell(env, "deploy/edge.sh").returncode == 0
    state = json.loads(state_file.read_text())
    if conflict == "public-task":
        state["ielts-part1-web-sg"]["IpPermissions"][0] = {
            "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
        }
    elif conflict == "extra-listener":
        state["listeners"].append({**state["listeners"][0], "Port": 80})
    elif conflict == "extra-rule":
        state["rules"] = [{"IsDefault": True}, {"IsDefault": False}]
    else:
        env["INGRESS_CIDR"] = "192.0.2.0/24"
    state_file.write_text(json.dumps(state))
    before = len(calls(log))
    result = run_shell(env, "deploy/edge.sh")
    assert result.returncode != 0
    assert "conflict" in result.stderr
    assert all(row[0] == "sts" or row[1].startswith("describe-")
               for row in calls(log)[before:])
    assert json.loads(state_file.read_text()) == state


def test_access_denied_is_not_treated_as_missing_edge(edge_env):
    env, log, _ = edge_env
    env["FAKE_ALB_ACCESS_DENIED"] = "1"
    result = run_shell(env, "deploy/edge.sh")
    assert result.returncode != 0
    assert "AccessDenied" in result.stderr
    assert all(row[0] == "sts" or row[1].startswith("describe-") for row in calls(log))


def test_status_reports_alb_port_and_never_task_ip(edge_env):
    env, log, state_file = edge_env
    env["ALB_PUBLIC_PORT"] = "19000"
    assert run_shell(env, "deploy/edge.sh").returncode == 0
    result = run_shell(env, "deploy/status.sh")
    assert result.returncode == 0, result.stderr
    assert "http://test-alb.cn-northwest-1.elb.amazonaws.com.cn:19000" in result.stdout
    state_file.write_text("{}")
    result = run_shell(env, "deploy/status.sh")
    assert "no public entrypoint" in result.stdout
    assert "CLIENT URL:" not in result.stdout
    assert not any(row[0] == "cloudfront" or row[1] == "describe-network-interfaces"
                   for row in calls(log))


def test_check_only_never_creates_missing_resources(edge_env):
    env, log, _ = edge_env
    result = run_shell(env, "deploy/edge.sh", "--check-only")
    assert result.returncode != 0
    assert "incomplete" in result.stderr
    assert all(row[0] == "sts" or row[1].startswith("describe-") for row in calls(log))


def test_start_rejects_public_backend_before_scaling(edge_env):
    env, log, state_file = edge_env
    assert run_shell(env, "deploy/edge.sh").returncode == 0
    state = json.loads(state_file.read_text())
    state["ielts-part1-web-sg"]["IpPermissions"][0] = {
        "IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    }
    state_file.write_text(json.dumps(state))
    before = len(calls(log))
    result = run_shell(env, "deploy/start.sh")
    assert result.returncode != 0
    assert not any(row[:2] == ["ecs", "update-service"] for row in calls(log)[before:])


def test_web_rollback_dry_run_and_actual_update(edge_env):
    env, log, state_file = edge_env
    assert run_shell(env, "deploy/edge.sh").returncode == 0
    before = len(calls(log))
    dry = run_shell(env, "deploy/rollback.sh", "--taskdef", "2", "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert all(row[0] == "sts" or row[1].startswith("describe-") for row in calls(log)[before:])
    real = run_shell(env, "deploy/rollback.sh", "--taskdef", "2")
    assert real.returncode == 0, real.stdout + real.stderr
    assert "CLIENT URL: http://test-alb.cn-northwest-1.elb.amazonaws.com.cn:9000" in real.stdout
    assert json.loads(state_file.read_text())["current_revision"] == "2"
    assert not any(row[0].startswith("bedrock") for row in calls(log))


@pytest.mark.parametrize("state_kind", ["absent", "creating", "foreign"])
def test_service_requires_ready_china_runtime_before_any_mutation(edge_env, state_kind):
    env, log, state_file = edge_env
    runtime = {
        "agentRuntimeName": "ielts_part1_runtime",
        "status": "READY",
        "agentRuntimeArn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:runtime/test",
    }
    if state_kind == "creating":
        runtime["status"] = "CREATING"
    elif state_kind == "foreign":
        runtime["agentRuntimeArn"] = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test"
    state_file.write_text(json.dumps({"runtimes": [] if state_kind == "absent" else [runtime]}))
    result = run_shell(env, "deploy/service.sh", "test-tag")
    assert result.returncode != 0
    assert all(row[:2] in (["sts", "get-caller-identity"],
                          ["bedrock-agentcore-control", "list-agent-runtimes"])
               for row in calls(log))


@pytest.mark.parametrize("error", ["existing-tag", "denied", "mutable"])
def test_service_rejects_bad_release_before_edge_or_secret_mutations(edge_env, error):
    env, log, state_file = edge_env
    state = {"runtimes": [{
        "agentRuntimeName": "ielts_part1_runtime", "status": "READY",
        "agentRuntimeArn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:runtime/test",
    }]}
    if error == "denied":
        state["image_error"] = "AccessDenied"
    elif error == "mutable":
        state["mutability"] = "MUTABLE"
    state_file.write_text(json.dumps(state))
    result = run_shell(env, "deploy/service.sh", "existing-tag")
    assert result.returncode != 0
    assert all(row[0] in {"sts", "bedrock-agentcore-control", "ecr"} for row in calls(log))
    assert not any(row[1].startswith(("create-", "put-", "update-")) for row in calls(log))


@pytest.mark.parametrize("failure", ["repository-delete", "object-delete"])
def test_teardown_does_not_claim_success_after_delete_errors(shell_env, failure):
    env, log = shell_env
    executable = Path(env["PATH"].split(os.pathsep)[0]) / "aws"
    env["FAKE_DELETE_FAILURE"] = failure
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "a=sys.argv[1:]; op=tuple(a[:2])\n"
        "with open(os.environ['FAKE_AWS_LOG'],'a') as f: f.write(json.dumps(a)+'\\n')\n"
        "failure=os.environ['FAKE_DELETE_FAILURE']\n"
        "if op==('iam','get-role'):\n"
        " print('(NoSuchEntity)',file=sys.stderr); sys.exit(254)\n"
        "if op==('ecr','delete-repository') and failure=='repository-delete':\n"
        " print('(AccessDenied)',file=sys.stderr); sys.exit(254)\n"
        "answers={('sts','get-caller-identity'):'123456789012',"
        "('ecs','describe-services'):'None',('elbv2','describe-load-balancers'):'None',"
        "('elbv2','describe-target-groups'):'None',('ecs','delete-cluster'):'',"
        "('ecr','delete-repository'):'',('ec2','describe-security-groups'):'None',"
        "('logs','delete-log-group'):'',('s3','ls'):'',"
        "('s3api','list-object-versions'):'[{\"Key\":\"test\",\"VersionId\":\"1\"}]',"
        "('s3api','delete-objects'):'{\"Errors\":[{\"Code\":\"AccessDenied\"}]}' }\n"
        "if op not in answers:\n"
        " print('UNEXPECTED AWS CALL: '+repr(a),file=sys.stderr); sys.exit(97)\n"
        "print(answers[op])\n"
    )
    arguments = ["--yes"] + (["--purge-s3"] if failure == "object-delete" else [])
    result = run_shell(env, "deploy/teardown.sh", *arguments)
    assert result.returncode != 0
    assert "Teardown commands finished" not in result.stdout
    assert "incomplete" in result.stderr.lower() or "object deletion errors" in result.stderr
    assert not any(row[:2] == ["s3api", "delete-bucket"] or row[0] == "cloudfront"
                   or row[0].startswith("bedrock") for row in calls(log))


@pytest.mark.parametrize("region", ["cn-northwest-1", "cn-north-1"])
def test_provision_renders_china_permissions_without_model_permissions(shell_env, region):
    env, log = shell_env
    env["AWS_REGION"] = region
    executable = Path(env["PATH"].split(os.pathsep)[0]) / "aws"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "a = sys.argv[1:]; op = tuple(a[:2])\n"
        "with open(os.environ['FAKE_AWS_LOG'],'a') as f: f.write(json.dumps(a)+'\\n')\n"
        "answers = {('sts','get-caller-identity'):'123456789012',"
        "('s3api','head-bucket'):'', ('ecr','describe-repositories'):'',"
        "('logs','describe-log-groups'):'/ecs/ielts-part1-web',"
        "('ec2','describe-security-groups'):'sg-task',"
        "('ecs','describe-clusters'):'ACTIVE',"
        "('iam','create-role'):'test-role', ('iam','attach-role-policy'):'',"
        "('iam','put-role-policy'):'', ('ecr','put-image-tag-mutability'):''}\n"
        "if op == ('iam','get-role'): sys.exit(254)\n"
        "if op not in answers:\n"
        " print('UNEXPECTED AWS CALL: '+repr(a),file=sys.stderr); sys.exit(97)\n"
        "print(answers[op])\n"
    )
    result = run_shell(env, "deploy/provision.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    policies = [
        json.loads(row[row.index("--policy-document") + 1])
        for row in calls(log) if "--policy-document" in row
    ]
    assert policies
    resources = []
    actions = []
    for policy in policies:
        for statement in policy["Statement"]:
            action = statement["Action"]
            actions.extend([action] if isinstance(action, str) else action)
            resource = statement.get("Resource", [])
            resources.extend([resource] if isinstance(resource, str) else resource)
    assert "bedrock-agentcore:InvokeAgentRuntime" in actions
    assert not any(action.startswith(("bedrock:", "bedrock-mantle:")) for action in actions)
    assert all(resource == "*" or resource.startswith("arn:aws-cn:") for resource in resources)
    assert any(f":{region}:123456789012:runtime/" in resource for resource in resources)
    assert any("arn:aws-cn:s3:::" in resource for resource in resources)
    assert any(
        row[row.index("--policy-arn") + 1].startswith("arn:aws-cn:iam::aws:policy/")
        for row in calls(log) if "--policy-arn" in row
    )


@pytest.mark.parametrize("script", sorted((ROOT / "deploy").glob("*.sh"))
                         + [ROOT / "backend/scripts/deploy.sh"])
def test_shell_syntax(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
