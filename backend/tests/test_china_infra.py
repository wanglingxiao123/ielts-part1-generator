"""Offline release contracts; validate calls against actual botocore API models."""

import copy
import json
from types import SimpleNamespace

import boto3
import pytest
from botocore import xform_name
from botocore.exceptions import ClientError
from botocore.session import Session
from botocore.validate import validate_parameters

from deploy import china_infra as infra


ACCOUNT = "123456789012"
S = infra.Settings(
    "china-test", ACCOUNT, image=f"{ACCOUNT}.dkr.ecr.cn-northwest-1.amazonaws.com.cn/"
    "ielts-part1-frontend@sha256:" + "a" * 64,
    runtime_arn=f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:{ACCOUNT}:"
    "runtime/ielts_part1_runtime-Ab12Cd34Ef",
)
BASE = ["--profile", S.profile, "--expected-account", ACCOUNT, "--region", S.region]
TG = S.arn("elasticloadbalancing", "targetgroup/ielts-part1-tg/1234567890abcdef")
ALB = S.arn("elasticloadbalancing", "loadbalancer/app/ielts-part1-alb/1234567890abcdef")
LISTENER = S.arn("elasticloadbalancing", "listener/app/ielts-part1-alb/1234567890abcdef/1234567890abcdef")
TD = S.arn("ecs", "task-definition/ielts-part1-web:1")
MUTATIONS = ("create_", "put_", "register_", "update_", "modify_", "authorize_")


def error(code="AccessDenied"):
    return ClientError({"Error": {"Code": code, "Message": "synthetic-private-detail"}}, "Operation")


class Client:
    def __init__(self, aws, service):
        self.aws, self.service = aws, service
        self.model = aws.models.get_service_model(service)
        self.operations = {xform_name(name): name for name in self.model.operation_names}

    def close(self):
        pass

    def get_paginator(self, method):
        # Verify this really is paginated in the installed SDK.
        config = self.aws.models.get_paginator_model(self.service)
        assert self.operations[method] in config._paginator_config
        return SimpleNamespace(paginate=lambda **params: iter([self.call(method, params)]))

    def call(self, method, params):
        operation = self.model.operation_model(self.operations[method])
        validate_parameters(params, operation.input_shape)
        self.aws.calls.append((self.service, method, copy.deepcopy(params)))
        response = self.aws.responses[(self.service, method)]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response(params) if callable(response) else response)

    def __getattr__(self, method):
        assert method in self.operations, f"Not an SDK operation: {self.service}.{method}"
        return lambda **params: self.call(method, params)


class AWS:
    def __init__(self):
        self.models = Session()
        self.clients, self.calls, self.responses = {}, [], {}
        self.sessions = []

    def factory(self, **kwargs):
        self.sessions.append(kwargs)
        return SimpleNamespace(client=self.client)

    def client(self, name, **kwargs):
        assert kwargs["config"].ignore_configured_endpoint_urls
        if name not in self.clients:
            self.clients[name] = Client(self, name)
        return self.clients[name]

    def set(self, service, method, response):
        self.responses[(service, method)] = response

    @property
    def writes(self):
        return [c for c in self.calls if c[1].startswith(MUTATIONS)]

    def validate_plan(self, plan):
        for action in plan:
            client = self.clients.get(action["service"]) or Client(self, action["service"])
            model = client.model.operation_model(client.operations[action["method"]])
            validate_parameters(action["params"], model.input_shape)


@pytest.fixture
def aws(monkeypatch):
    aws = AWS()
    monkeypatch.setattr(boto3, "Session", aws.factory)
    monkeypatch.setattr(infra.secrets, "token_urlsafe", lambda _: "synthetic-session-token")
    aws.set("sts", "get_caller_identity", {
        "Account": ACCOUNT, "Arn": f"arn:aws-cn:sts::{ACCOUNT}:assumed-role/operator/session",
    })
    return aws


def absent_prepare(aws):
    aws.set("s3", "get_bucket_location", error("NoSuchBucket"))
    aws.set("ecr", "describe_repositories", error("RepositoryNotFoundException"))
    aws.set("ecs", "describe_clusters", {"clusters": [], "failures": [{"reason": "MISSING"}]})
    aws.set("logs", "describe_log_groups", {"logGroups": []})
    aws.set("iam", "get_role", error("NoSuchEntity"))


def network(aws):
    aws.set("ec2", "describe_vpcs", {"Vpcs": [{"VpcId": "vpc-test"}]})
    aws.set("ec2", "describe_subnets", {"Subnets": [
        {"SubnetId": f"subnet-{i}", "AvailabilityZone": f"cn-northwest-1{az}",
         "State": "available", "AvailableIpAddressCount": 100}
        for i, az in ((1, "a"), (2, "b"))
    ]})
    aws.set("ec2", "describe_internet_gateways", {"InternetGateways": [
        {"InternetGatewayId": "igw-test", "Attachments": [{"VpcId": "vpc-test", "State": "available"}]},
    ]})
    aws.set("ec2", "describe_route_tables", {"RouteTables": [{
        "Associations": [{"Main": True}],
        "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-test", "State": "active"}],
    }]})


def absent_edge(aws):
    network(aws)
    aws.set("ec2", "describe_security_groups", {"SecurityGroups": []})
    aws.set("elbv2", "describe_target_groups", error("TargetGroupNotFound"))
    aws.set("elbv2", "describe_load_balancers", error("LoadBalancerNotFound"))


def existing_edge(aws):
    network(aws)
    groups = {
        infra.ALB_SG: {"GroupId": "sg-alb", "GroupName": infra.ALB_SG,
                       "IpPermissions": [infra.ingress(9000, "0.0.0.0/0")]},
        infra.TASK_SG: {"GroupId": "sg-task", "GroupName": infra.TASK_SG,
                        "IpPermissions": [infra.ingress(80, "sg-alb", group=True)]},
    }
    for group in groups.values():
        group.update(VpcId="vpc-test", Tags=infra.tags(), IpPermissionsEgress=[
            {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        ])
    aws.set("ec2", "describe_security_groups", lambda p: {
        "SecurityGroups": [groups[p["Filters"][1]["Values"][0]]],
    })
    aws.set("elbv2", "describe_target_groups", {"TargetGroups": [{
        "TargetGroupArn": TG, "VpcId": "vpc-test", "Port": 80, "Protocol": "HTTP",
        "TargetType": "ip", "HealthCheckPath": "/healthz", "HealthCheckProtocol": "HTTP",
        "HealthCheckPort": "traffic-port", "Matcher": {"HttpCode": "200"},
    }]})
    aws.set("elbv2", "describe_load_balancers", {"LoadBalancers": [{
        "LoadBalancerArn": ALB, "VpcId": "vpc-test", "Scheme": "internet-facing",
        "Type": "application", "IpAddressType": "ipv4", "SecurityGroups": ["sg-alb"],
        "AvailabilityZones": [{"SubnetId": "subnet-1"}, {"SubnetId": "subnet-2"}],
        "DNSName": "synthetic.cn-northwest-1.elb.amazonaws.com.cn", "State": {"Code": "active"},
    }]})
    aws.set("elbv2", "describe_tags", lambda p: {"TagDescriptions": [
        {"ResourceArn": p["ResourceArns"][0], "Tags": infra.tags()},
    ]})
    actions = [{"Type": "forward", "TargetGroupArn": TG}]
    aws.set("elbv2", "describe_listeners", {"Listeners": [
        {"ListenerArn": LISTENER, "Port": 9000, "Protocol": "HTTP", "DefaultActions": actions},
    ]})
    aws.set("elbv2", "describe_rules", {"Rules": [{"IsDefault": True, "Actions": actions, "Conditions": []}]})
    return groups


def existing_web(aws):
    groups = existing_edge(aws)
    aws.set("ecs", "describe_clusters", {"clusters": [
        {"status": "ACTIVE", "clusterArn": S.arn("ecs", f"cluster/{infra.PROJECT}"), "tags": infra.tags(True)},
    ]})
    aws.set("ecr", "describe_repositories", {"repositories": [{
        "repositoryArn": S.arn("ecr", "repository/ielts-part1-frontend"),
        "imageTagMutability": "IMMUTABLE", "encryptionConfiguration": {"encryptionType": "AES256"},
    }]})
    aws.set("ecr", "list_tags_for_resource", {"tags": infra.tags()})
    aws.set("ecr", "describe_images", {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]})
    aws.set("bedrock-agentcore-control", "get_agent_runtime", {
        "agentRuntimeArn": S.runtime_arn, "status": "READY",
    })
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT},
                      "ArnLike": {"aws:SourceArn": S.arn("ecs", "*")}},
    }]}
    aws.set("iam", "get_role", lambda p: {"Role": {
        "Arn": S.role(p["RoleName"]), "Path": "/", "AssumeRolePolicyDocument": trust,
    }})
    aws.set("iam", "list_role_tags", {"Tags": infra.tags()})
    aws.set("iam", "list_attached_role_policies", {"AttachedPolicies": []})
    aws.set("iam", "list_role_policies", lambda p: {"PolicyNames": [p["RoleName"] + "-inline"]})
    aws.set("iam", "get_role_policy", lambda p: {"PolicyDocument": (
        infra.execution_policy(S) if p["RoleName"] == infra.EXEC_ROLE else infra.web_policy(S)
    )})
    aws.set("ecs", "describe_services", {"services": [], "failures": [{"reason": "MISSING"}]})
    aws.set("ecs", "list_task_definitions", {"taskDefinitionArns": []})
    aws.set("ssm", "describe_parameters", {"Parameters": []})
    return groups


def test_prepare_default_plan_uses_real_sdk_parameter_contracts(aws, capsys):
    absent_prepare(aws)
    assert infra.main(["prepare", *BASE]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert aws.sessions == [{"profile_name": "china-test", "region_name": infra.REGION}]
    assert not aws.writes
    aws.validate_plan(plan["actions"])
    actions = plan["actions"]
    repositories = [a["params"] for a in actions if a["method"] == "create_repository"]
    assert len(repositories) == 2
    assert all(r["imageTagMutability"] == "IMMUTABLE" for r in repositories)
    assert all(r["tags"] == infra.tags() for r in repositories)
    assert next(a for a in actions if a["method"] == "put_public_access_block")["params"][
        "PublicAccessBlockConfiguration"
    ] == infra.PUBLIC_BLOCK
    assert next(a for a in actions if a["method"] == "put_retention_policy")["params"]["retentionInDays"] == 7
    for a in actions:
        if a["method"] == "create_role":
            trust = json.loads(a["params"]["AssumeRolePolicyDocument"])
            assert trust["Statement"][0]["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert "cloudfront" not in json.dumps(plan).lower()


def test_prepare_apply_writes_only_after_all_reads(aws, capsys):
    absent_prepare(aws)
    for service, methods in {
        "s3": ["create_bucket", "put_bucket_tagging", "put_public_access_block",
               "put_bucket_versioning", "put_bucket_encryption"],
        "ecr": ["create_repository"], "ecs": ["create_cluster"],
        "logs": ["create_log_group", "put_retention_policy"], "iam": ["create_role", "put_role_policy"],
    }.items():
        for method in methods:
            aws.set(service, method, {})
    assert infra.main(["prepare", *BASE, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "apply"
    first_write = next(i for i, c in enumerate(aws.calls) if c[1].startswith(MUTATIONS))
    assert all(c[1].startswith(MUTATIONS) for c in aws.calls[first_write:])


@pytest.mark.parametrize("code", ["AccessDenied", "ThrottlingException", "InvalidToken"])
def test_denied_is_never_absence_even_with_apply(aws, code, capsys):
    absent_prepare(aws)
    aws.set("iam", "get_role", error(code))
    assert infra.main(["prepare", *BASE, "--apply"]) == 1
    output = capsys.readouterr()
    assert not aws.writes
    assert json.loads(output.err)["code"] == code
    assert "synthetic-private-detail" not in output.err


@pytest.mark.parametrize("change", [
    {"--profile": " "}, {"--expected-account": "123"}, {"--region": "cn-north-1"},
    {"--email-domains": "*"}, {"--email-domains": "example.com, *"},
])
def test_invalid_configuration_never_constructs_session(aws, capsys, change):
    argv = ["prepare", *BASE]
    for key, value in change.items():
        if key in argv:
            argv[argv.index(key) + 1] = value
        else:
            argv.extend([key, value])
    assert infra.main(argv) == 1
    assert not aws.sessions
    capsys.readouterr()


def test_account_mismatch_stops_before_discovery(aws, capsys):
    aws.set("sts", "get_caller_identity", {"Account": "999999999999", "Arn": "foreign"})
    assert infra.main(["prepare", *BASE, "--apply"]) == 1
    assert len(aws.calls) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "CallerIdentityMismatch"


def test_unowned_bucket_is_not_adopted(aws, capsys):
    aws.set("s3", "get_bucket_location", {"LocationConstraint": infra.REGION})
    aws.set("s3", "get_bucket_tagging", {"TagSet": []})
    assert infra.main(["prepare", *BASE, "--apply"]) == 1
    assert not aws.writes
    assert json.loads(capsys.readouterr().err)["code"] == "OwnershipConflict"


def test_edge_plan_and_apply_resolve_created_ids(aws, capsys):
    absent_edge(aws)
    aws.set("ec2", "create_security_group",
            lambda p: {"GroupId": "sg-alb" if p["GroupName"] == infra.ALB_SG else "sg-task"})
    aws.set("ec2", "authorize_security_group_ingress", {})
    aws.set("elbv2", "create_target_group", {"TargetGroups": [{"TargetGroupArn": TG}]})
    aws.set("elbv2", "create_load_balancer", {"LoadBalancers": [{"LoadBalancerArn": ALB}]})
    for method in ("modify_target_group_attributes", "modify_load_balancer_attributes", "create_listener"):
        aws.set("elbv2", method, {})
    assert infra.main(["edge", *BASE]) == 0
    plan = json.loads(capsys.readouterr().out)
    aws.validate_plan(plan["actions"])
    assert not aws.writes
    assert infra.main(["edge", *BASE, "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["alb_arn"] == ALB
    assert result["task_sg"] == "sg-task"
    rules = [c[2] for c in aws.writes if c[1] == "authorize_security_group_ingress"]
    assert rules == [
        {"GroupId": "sg-alb", "IpPermissions": [infra.ingress(9000, "0.0.0.0/0")]},
        {"GroupId": "sg-task", "IpPermissions": [infra.ingress(80, "sg-alb", group=True)]},
    ]
    listener = next(c[2] for c in aws.writes if c[1] == "create_listener")
    assert listener["Port"] == 9000
    assert listener["DefaultActions"] == [{"Type": "forward", "TargetGroupArn": TG}]


@pytest.mark.parametrize("bad", ["one_az", "blackhole", "private_override", "detached_igw"])
def test_public_routes_are_required(aws, bad):
    network(aws)
    if bad == "one_az":
        aws.responses[("ec2", "describe_subnets")]["Subnets"][1]["AvailabilityZone"] = "cn-northwest-1a"
    elif bad == "blackhole":
        aws.responses[("ec2", "describe_route_tables")]["RouteTables"][0]["Routes"][0]["State"] = "blackhole"
    elif bad == "private_override":
        aws.responses[("ec2", "describe_route_tables")]["RouteTables"].append({
            "Associations": [{"SubnetId": "subnet-2"}], "Routes": [],
        })
    else:
        aws.set("ec2", "describe_internet_gateways", {"InternetGateways": []})
    with pytest.raises(infra.Conflict, match="TwoPublicAvailabilityZones"):
        infra.Infra(S).network()
    assert not aws.writes


@pytest.mark.parametrize("bad", [
    "public_task", "extra_alb_port", "unowned_sg", "unowned_alb", "extra_listener", "wrong_port",
    "extra_rule", "wrong_tg", "ipv6_ingress",
])
def test_edge_conflicts_fail_without_cleanup(aws, capsys, bad):
    groups = existing_edge(aws)
    if bad == "public_task":
        groups[infra.TASK_SG]["IpPermissions"] = [infra.ingress(80, "0.0.0.0/0")]
    elif bad == "extra_alb_port":
        groups[infra.ALB_SG]["IpPermissions"].append(infra.ingress(443, "0.0.0.0/0"))
    elif bad == "unowned_sg":
        groups[infra.TASK_SG]["Tags"] = []
    elif bad == "unowned_alb":
        aws.set("elbv2", "describe_tags", lambda p: {"TagDescriptions": [
            {"ResourceArn": p["ResourceArns"][0], "Tags": []},
        ]})
    elif bad == "extra_listener":
        aws.responses[("elbv2", "describe_listeners")]["Listeners"] *= 2
    elif bad == "wrong_port":
        aws.responses[("elbv2", "describe_listeners")]["Listeners"][0]["Port"] = 80
    elif bad == "extra_rule":
        aws.responses[("elbv2", "describe_rules")]["Rules"].append({"IsDefault": False})
    elif bad == "wrong_tg":
        aws.responses[("elbv2", "describe_target_groups")]["TargetGroups"][0]["Port"] = 8080
    else:
        groups[infra.TASK_SG]["IpPermissions"][0]["Ipv6Ranges"] = [{"CidrIpv6": "::/0"}]
    assert infra.main(["edge", *BASE, "--apply"]) == 1
    assert not aws.writes
    assert json.loads(capsys.readouterr().err)["error"] == "Conflict"


def test_web_dry_run_task_contract_and_no_secret_reads(aws, capsys):
    existing_web(aws)
    assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn]) == 0
    plan = json.loads(capsys.readouterr().out)
    aws.validate_plan(plan["actions"])
    assert not aws.writes
    task = next(a["params"] for a in plan["actions"] if a["method"] == "register_task_definition")
    container = task["containerDefinitions"][0]
    assert task["runtimePlatform"] == {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"}
    assert container["user"] == "10001"
    assert container["portMappings"] == [{"containerPort": 80, "protocol": "tcp"}]
    assert container["systemControls"] == [{"namespace": "net.ipv4.ip_unprivileged_port_start", "value": "0"}]
    assert container["secrets"] == [{"name": "SESSION_SECRET", "valueFrom": S.parameter_arn}]
    env = {e["name"]: e["value"] for e in container["environment"]}
    assert env["ALLOWED_EMAIL_DOMAINS"] == "example.com"
    assert env["AGENT_RUNTIME_ARN"] == S.runtime_arn
    assert "SESSION_SECRET" not in env
    service = next(a["params"] for a in plan["actions"] if a["method"] == "create_service")
    assert service["desiredCount"] == 1
    assert service["deploymentConfiguration"]["deploymentCircuitBreaker"] == {"enable": True, "rollback": True}
    assert "alarms" not in service["deploymentConfiguration"]
    assert not any(c[1] in ("get_parameter", "get_parameters", "get_secret_value") for c in aws.calls)


def test_web_apply_generates_session_secret_only_in_memory(aws, capsys):
    existing_web(aws)
    aws.set("ssm", "put_parameter", {"Version": 1})
    aws.set("ecs", "register_task_definition", {"taskDefinition": {"taskDefinitionArn": TD}})
    aws.set("ecs", "create_service", {"service": {"serviceName": infra.SERVICE}})
    assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn, "--apply"]) == 0
    output = capsys.readouterr().out
    assert "synthetic-session-token" not in output
    assert json.loads(output)["task_definition"] == TD
    put = next(c[2] for c in aws.writes if c[1] == "put_parameter")
    assert put["Value"] == "synthetic-session-token"
    assert put["Overwrite"] is False
    service = next(c[2] for c in aws.writes if c[1] == "create_service")
    assert service["taskDefinition"] == TD


@pytest.mark.parametrize("bad", ["not_ready", "wrong_runtime", "wrong_image", "unowned_secret",
                                "plaintext_parameter", "custom_kms", "role_trust", "extra_role_policy"])
def test_web_preflight_blocks_writes(aws, capsys, bad):
    existing_web(aws)
    if bad == "not_ready":
        aws.responses[("bedrock-agentcore-control", "get_agent_runtime")]["status"] = "CREATING"
    elif bad == "wrong_runtime":
        aws.responses[("bedrock-agentcore-control", "get_agent_runtime")]["agentRuntimeArn"] += "wrong"
    elif bad == "wrong_image":
        aws.responses[("ecr", "describe_images")]["imageDetails"][0]["imageDigest"] = "sha256:" + "b" * 64
    elif bad in ("unowned_secret", "plaintext_parameter", "custom_kms"):
        aws.set("ssm", "describe_parameters", {"Parameters": [{
            "Name": infra.PARAMETER, "Type": "String" if bad == "plaintext_parameter" else "SecureString",
            "KeyId": "customer-key" if bad == "custom_kms" else "alias/aws/ssm",
        }]})
        aws.set("ssm", "list_tags_for_resource", {"TagList": []})
    elif bad == "role_trust":
        aws.set("iam", "get_role", {"Role": {
            "Arn": S.role(infra.EXEC_ROLE), "Path": "/", "AssumeRolePolicyDocument": {},
        }})
    else:
        aws.set("iam", "list_role_policies", {"PolicyNames": ["unrelated"]})
    assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn, "--apply"]) == 1
    assert not aws.writes
    assert json.loads(capsys.readouterr().err)["error"] == "Conflict"


def test_existing_secret_never_rotated_and_update_records_rollback(aws, capsys):
    existing_web(aws)
    aws.set("ssm", "describe_parameters", {"Parameters": [{
        "Name": infra.PARAMETER, "Type": "SecureString", "KeyId": "alias/aws/ssm",
    }]})
    aws.set("ssm", "list_tags_for_resource", {"TagList": infra.tags()})
    aws.set("ecs", "list_task_definitions", {"taskDefinitionArns": [TD]})
    aws.set("ecs", "describe_task_definition", {
        "tags": infra.tags(True), "taskDefinition": {"taskDefinitionArn": TD},
    })
    aws.set("ecs", "describe_services", {"services": [{
        "status": "ACTIVE", "serviceName": infra.SERVICE, "tags": infra.tags(True),
        "launchType": "FARGATE", "taskDefinition": TD,
        "deploymentController": {"type": "ECS"},
        "loadBalancers": [{"targetGroupArn": TG, "containerName": "web", "containerPort": 80}],
        "networkConfiguration": {"awsvpcConfiguration": {
            "subnets": ["subnet-1", "subnet-2"], "securityGroups": ["sg-task"], "assignPublicIp": "ENABLED",
        }},
    }]})
    assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn]) == 0
    plan = json.loads(capsys.readouterr().out)
    aws.validate_plan(plan["actions"])
    assert plan["previous_task_definition"] == TD
    assert not any(a["method"] == "put_parameter" for a in plan["actions"])
    assert plan["actions"][-1]["method"] == "update_service"


@pytest.mark.parametrize("image,runtime", [
    (S.image.replace("@sha256:" + "a" * 64, ":latest"), S.runtime_arn),
    (S.image.replace(ACCOUNT, "999999999999"), S.runtime_arn),
    (S.image, S.runtime_arn.replace("aws-cn", "aws")),
    (S.image, S.runtime_arn + "/endpoint/DEFAULT"),
])
def test_bad_publish_references_are_local_errors(aws, capsys, image, runtime):
    assert infra.main(["web", *BASE, "--image", image, "--runtime-arn", runtime]) == 1
    assert not aws.sessions
    capsys.readouterr()


def test_status_is_read_only_bounded_health_snapshot(aws, capsys):
    existing_web(aws)
    aws.set("elbv2", "describe_target_health", {"TargetHealthDescriptions": [
        {"Target": {"Id": "10.0.0.2", "Port": 80}, "TargetHealth": {"State": "healthy"}},
    ]})
    assert infra.main(["status", *BASE]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["targets"][0]["state"] == "healthy"
    assert result["url"].endswith(":9000")
    assert result["actions"] == []
    assert not aws.writes


def test_execution_and_web_roles_have_separate_permissions():
    execution = infra.execution_policy(S)["Statement"]
    web = infra.web_policy(S)["Statement"]
    assert next(p["Resource"] for p in execution if "ssm:GetParameters" in p["Action"]) == S.parameter_arn
    assert not any("ssm:GetParameters" in p["Action"] for p in web)
    assert not any(a.startswith(("bedrock:", "bedrock-mantle:", "iam:"))
                   for p in execution + web for a in p["Action"])
    assert all(p["Resource"] != "*" for p in web)


def converged_web(aws):
    """Model a real describe response, including defaults absent from registration."""
    existing_web(aws)
    aws.set("ssm", "describe_parameters", {"Parameters": [{
        "Name": infra.PARAMETER, "Type": "SecureString", "KeyId": "alias/aws/ssm",
    }]})
    aws.set("ssm", "list_tags_for_resource", {"TagList": infra.tags()})
    task = copy.deepcopy(infra.task_definition(S))
    task.pop("tags")
    task.update(
        taskDefinitionArn=TD, revision=1, status="ACTIVE", registeredBy=S.role("operator"),
        requiresAttributes=[{"name": "ecs.capability.task-iam-role"}],
        compatibilities=["EC2", "FARGATE"], volumes=[], placementConstraints=[],
        enableFaultInjection=False, ephemeralStorage={"sizeInGiB": 20},
    )
    container = task["containerDefinitions"][0]
    container.update(cpu=0, mountPoints=[], volumesFrom=[], environmentFiles=[])
    container["portMappings"][0]["hostPort"] = 80
    container["environment"].reverse()
    container["logConfiguration"]["secretOptions"] = []
    service = {
        "status": "ACTIVE", "serviceName": infra.SERVICE, "tags": infra.tags(True),
        "launchType": "FARGATE", "taskDefinition": TD, "desiredCount": 1,
        "deploymentController": {"type": "ECS"}, "propagateTags": "TASK_DEFINITION",
        "healthCheckGracePeriodSeconds": 90, "platformVersion": "1.4.0",
        "deploymentConfiguration": {
            "deploymentCircuitBreaker": {"enable": True, "rollback": True},
            "maximumPercent": 200, "minimumHealthyPercent": 100, "strategy": "ROLLING",
        },
        "loadBalancers": [{"targetGroupArn": TG, "containerName": "web", "containerPort": 80}],
        "networkConfiguration": {"awsvpcConfiguration": {
            "subnets": ["subnet-2", "subnet-1"], "securityGroups": ["sg-task"], "assignPublicIp": "ENABLED",
        }},
    }
    aws.set("ecs", "describe_services", {"services": [service]})
    aws.set("ecs", "list_task_definitions", {"taskDefinitionArns": [TD]})
    aws.set("ecs", "describe_task_definition", {"tags": infra.tags(True), "taskDefinition": task})
    return task, service


def test_web_apply_twice_with_sdk_defaults_has_no_mutations(aws, capsys):
    task, _ = converged_web(aws)
    original = copy.deepcopy(task)
    for _ in range(2):
        assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn, "--apply"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["actions"] == []
        assert output["task_definition"] == TD
    assert not aws.writes
    assert task == original


def test_observed_china_circuit_breaker_defaults_do_not_update_service(aws, capsys):
    _, service = converged_web(aws)
    service["deploymentConfiguration"]["deploymentCircuitBreaker"].update(
        resetOnHealthyTask=True,
        thresholdConfiguration={"type": "BOUNDED_PERCENT", "value": 50},
    )
    service["deploymentConfiguration"]["bakeTimeInMinutes"] = 0
    original = copy.deepcopy(service)
    for _ in range(2):
        assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn, "--apply"]) == 0
        assert json.loads(capsys.readouterr().out)["actions"] == []
    assert not aws.writes
    assert service == original


@pytest.mark.parametrize("change", ["enable", "rollback", "reset", "threshold"])
def test_circuit_breaker_defaults_do_not_hide_real_changes(aws, capsys, change):
    _, service = converged_web(aws)
    breaker = service["deploymentConfiguration"]["deploymentCircuitBreaker"]
    breaker.update(
        resetOnHealthyTask=True,
        thresholdConfiguration={"type": "BOUNDED_PERCENT", "value": 50},
    )
    if change in ("enable", "rollback"):
        breaker[change] = False
    elif change == "reset":
        breaker["resetOnHealthyTask"] = False
    else:
        breaker["thresholdConfiguration"]["value"] = 60
    assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [a["method"] for a in plan["actions"]] == ["update_service"]
    assert not aws.writes


@pytest.mark.parametrize("change", [
    "image", "environment", "secret", "port", "command", "extra_container",
    "privileged", "volume", "ephemeral_storage",
])
def test_real_task_changes_still_create_revision(aws, capsys, change):
    task, _ = converged_web(aws)
    container = task["containerDefinitions"][0]
    if change == "image":
        container["image"] = S.image.replace("a" * 64, "b" * 64)
    elif change == "environment":
        next(row for row in container["environment"] if row["name"] == "PORT")["value"] = "8080"
    elif change == "secret":
        container["secrets"][0]["valueFrom"] += "-other"
    elif change == "port":
        container["portMappings"][0]["containerPort"] = 8080
    elif change == "command":
        container["command"] = ["python", "-m", "different.app"]
    elif change == "extra_container":
        task["containerDefinitions"].append({"name": "sidecar", "image": S.image, "essential": True})
    elif change == "privileged":
        container["privileged"] = True
    elif change == "volume":
        task["volumes"] = [{"name": "unexpected"}]
    else:
        task["ephemeralStorage"] = {"sizeInGiB": 50}
    assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn]) == 0
    plan = json.loads(capsys.readouterr().out)
    aws.validate_plan(plan["actions"])
    assert [a["method"] for a in plan["actions"]] == ["register_task_definition", "update_service"]
    assert not aws.writes


def test_task_normalization_preserves_command_and_entrypoint_order():
    first = infra.task_definition(S)
    first["containerDefinitions"][0].update(command=["first", "second"], entryPoint=["one", "two"])
    for key in ("command", "entryPoint"):
        second = copy.deepcopy(first)
        second["containerDefinitions"][0][key].reverse()
        assert infra.normalized_task_definition(first) != infra.normalized_task_definition(second)


@pytest.mark.parametrize("change", ["count", "tags", "grace", "platform", "breaker", "revision"])
def test_service_drift_updates_service_without_registering_task(aws, capsys, change):
    _, service = converged_web(aws)
    if change == "count":
        service["desiredCount"] = 0
    elif change == "tags":
        service["propagateTags"] = "SERVICE"
    elif change == "grace":
        service["healthCheckGracePeriodSeconds"] = 30
    elif change == "platform":
        service["platformVersion"] = "LATEST"
    elif change == "breaker":
        service["deploymentConfiguration"]["deploymentCircuitBreaker"]["rollback"] = False
    else:
        service["taskDefinition"] = TD.replace(":1", ":2")
    assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn]) == 0
    plan = json.loads(capsys.readouterr().out)
    aws.validate_plan(plan["actions"])
    assert [a["method"] for a in plan["actions"]] == ["update_service"]
    assert plan["actions"][0]["params"]["taskDefinition"] == TD
    assert not aws.writes


@pytest.mark.parametrize("stage", ["edge", "web"])
@pytest.mark.parametrize("group_name", [infra.ALB_SG, infra.TASK_SG])
@pytest.mark.parametrize("rules", [
    [], [infra.ingress(22, "0.0.0.0/0")],
    [{"IpProtocol": "-1", "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}],
    [{"IpProtocol": "udp", "FromPort": 0, "ToPort": 65535, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
])
def test_missing_egress_blocks_all_mutations(aws, capsys, stage, group_name, rules):
    groups = existing_web(aws) if stage == "web" else existing_edge(aws)
    groups[group_name]["IpPermissionsEgress"] = rules
    argv = [stage, *BASE, "--apply"]
    if stage == "web":
        argv += ["--image", S.image, "--runtime-arn", S.runtime_arn]
    assert infra.main(argv) == 1
    assert json.loads(capsys.readouterr().err)["code"] == (
        "AlbSecurityGroupEgressConflict" if group_name == infra.ALB_SG else "TaskSecurityGroupEgressConflict"
    )
    assert not aws.writes


def test_scoped_egress_is_accepted_without_broadening(aws, capsys):
    groups = existing_web(aws)
    groups[infra.ALB_SG]["IpPermissionsEgress"] = [infra.ingress(80, "sg-task", group=True)]
    groups[infra.TASK_SG]["IpPermissionsEgress"] = [infra.ingress(443, "0.0.0.0/0")]
    assert infra.main(["web", *BASE, "--image", S.image, "--runtime-arn", S.runtime_arn]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert not any(a["service"] == "ec2" for a in plan["actions"])
    assert not aws.writes


def test_alb_egress_to_another_security_group_is_rejected(aws, capsys):
    groups = existing_edge(aws)
    groups[infra.ALB_SG]["IpPermissionsEgress"] = [infra.ingress(80, "sg-other", group=True)]
    assert infra.main(["edge", *BASE, "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "AlbSecurityGroupEgressConflict"
    assert not aws.writes


def existing_bucket(aws, encryption):
    absent_prepare(aws)
    aws.set("s3", "get_bucket_location", {"LocationConstraint": S.region})
    aws.set("s3", "get_bucket_tagging", {"TagSet": infra.tags()})
    aws.set("s3", "get_bucket_policy_status", error("NoSuchBucketPolicy"))
    aws.set("s3", "get_bucket_acl", {
        "Owner": {"ID": "owner"}, "Grants": [
            {"Grantee": {"Type": "CanonicalUser", "ID": "owner"}, "Permission": "FULL_CONTROL"},
        ],
    })
    aws.set("s3", "get_bucket_encryption", encryption)


@pytest.mark.parametrize("algorithm", ["aws:kms", "aws:kms:dsse"])
def test_existing_kms_bucket_is_rejected_before_any_write(aws, capsys, algorithm):
    existing_bucket(aws, {"ServerSideEncryptionConfiguration": {"Rules": [{
        "ApplyServerSideEncryptionByDefault": {
            "SSEAlgorithm": algorithm, "KMSMasterKeyID": S.arn("kms", "key/offline"),
        }, "BucketKeyEnabled": True,
    }]}})
    assert infra.main(["prepare", *BASE, "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "BucketEncryptionConflict"
    assert not aws.writes


def test_existing_aes256_bucket_preserves_encryption(aws, capsys):
    existing_bucket(aws, {"ServerSideEncryptionConfiguration": {"Rules": [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}, "BucketKeyEnabled": False,
    }]}})
    assert infra.main(["prepare", *BASE]) == 0
    plan = json.loads(capsys.readouterr().out)
    aws.validate_plan(plan["actions"])
    assert not any(a["method"] == "put_bucket_encryption" for a in plan["actions"])
    request = next(c[2] for c in aws.calls if c[1] == "get_bucket_encryption")
    assert request == {"Bucket": S.bucket, "ExpectedBucketOwner": ACCOUNT}


def test_missing_bucket_encryption_can_be_initialized(aws, capsys):
    existing_bucket(aws, error("ServerSideEncryptionConfigurationNotFoundError"))
    assert infra.main(["prepare", *BASE]) == 0
    actions = json.loads(capsys.readouterr().out)["actions"]
    assert len([a for a in actions if a["method"] == "put_bucket_encryption"]) == 1


def test_bucket_encryption_access_denied_does_not_trigger_repair(aws, capsys):
    existing_bucket(aws, error("AccessDenied"))
    assert infra.main(["prepare", *BASE, "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "AccessDenied"
    assert not aws.writes
