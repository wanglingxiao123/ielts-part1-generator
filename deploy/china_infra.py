"""Bounded Ningxia infrastructure release: prepare, edge, web, status.

All stages require --profile, --expected-account and --region cn-northwest-1.
Default is a read-only plan; repeat with --apply after reviewing it. Web also
requires --image <full frontend ECR URI>@sha256:<digest> and --runtime-arn.
Run prepare, edge, then web; use status separately until targets are healthy.
No resource deletion, policy detachment, secret reads, image build or CLI calls.
IAM and ALB propagation may require rerunning a stage; no long blocking waiters.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import secrets
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

PROJECT = "ielts-part1"
REGION = "cn-northwest-1"
OWNER = "ielts-part1-china"
TAGS = {"Project": PROJECT, "ManagedBy": OWNER}
SERVICE = FAMILY = f"{PROJECT}-web"
LOG_GROUP = f"/ecs/{SERVICE}"
PARAMETER = f"/{PROJECT}/session-secret"
ALB_NAME, TG_NAME = f"{PROJECT}-alb", f"{PROJECT}-tg"
ALB_SG, TASK_SG = f"{PROJECT}-alb-sg", f"{PROJECT}-web-sg"
EXEC_ROLE, WEB_ROLE = f"{PROJECT}-ecs-exec", f"{PROJECT}-web-task"
PUBLIC_BLOCK = dict.fromkeys(
    ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"), True
)


class Conflict(Exception):
    """Only constant diagnostic codes belong in this exception."""


def require(condition: Any, code: str) -> None:
    if not condition:
        raise Conflict(code)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise Conflict("InvalidArguments")


def tags(lower: bool = False) -> list[dict]:
    key, value = ("key", "value") if lower else ("Key", "Value")
    return [{key: k, value: v} for k, v in TAGS.items()]


def owned(values: Any) -> None:
    if not isinstance(values, dict):
        values = {t.get("Key", t.get("key")): t.get("Value", t.get("value")) for t in values}
    require(all(values.get(k) == v for k, v in TAGS.items()), "OwnershipConflict")


def optional(client: Any, method: str, missing: tuple[str, ...], **kwargs: Any) -> Any:
    # S3/IAM/ELB use some errors absent from the SDK's modeled exception classes.
    # Only the explicitly named absence code is recoverable, never a blanket 4xx.
    try:
        return getattr(client, method)(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in missing:
            raise
        return None


def rows(client: Any, method: str, key: str, **kwargs: Any) -> list:
    return [
        item for page in client.get_paginator(method).paginate(**kwargs)
        for item in page.get(key, [])
    ]


def one(values: list, code: str) -> Any:
    require(len(values) <= 1, code)
    return values[0] if values else None


@dataclass(frozen=True)
class Settings:
    profile: str
    account: str
    region: str = REGION
    image: str | None = None
    runtime_arn: str | None = None
    email_domains: str = "example.com"

    @property
    def bucket(self) -> str:
        return f"{PROJECT}-materials-{self.account}"

    def arn(self, service: str, resource: str) -> str:
        return f"arn:aws-cn:{service}:{self.region}:{self.account}:{resource}"

    def role(self, name: str) -> str:
        return f"arn:aws-cn:iam::{self.account}:role/{name}"

    @property
    def parameter_arn(self) -> str:
        return self.arn("ssm", f"parameter{PARAMETER}")

    @property
    def registry(self) -> str:
        return f"{self.account}.dkr.ecr.{self.region}.amazonaws.com.cn"


def validate(args: argparse.Namespace) -> Settings:
    require(bool(args.profile.strip()) and re.fullmatch(r"[0-9]{12}", args.expected_account),
            "InvalidIdentityArguments")
    require(args.region == REGION, "NingxiaOnly")
    domains = args.email_domains.split(",")
    require(all(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", d)
                for d in domains), "InvalidEmailDomains")
    s = Settings(args.profile, args.expected_account, args.region,
                 args.image, args.runtime_arn, args.email_domains)
    if args.stage == "web":
        require(s.image and re.fullmatch(
            re.escape(f"{s.registry}/{PROJECT}-frontend@sha256:") + r"[0-9a-f]{64}", s.image
        ), "DigestPinnedFrontendImageRequired")
        require(s.runtime_arn and re.fullmatch(
            re.escape(s.arn("bedrock-agentcore", "runtime/ielts_part1_runtime-"))
            + r"[A-Za-z0-9]+", s.runtime_arn
        ), "ExactChinaRuntimeRequired")
    require(not (args.stage == "status" and args.apply), "StatusIsReadOnly")
    return s


class Infra:
    def __init__(self, s: Settings):
        self.s = s
        self.session = boto3.Session(profile_name=s.profile, region_name=s.region)
        self.clients: dict[str, Any] = {}
        self.actions: list[dict] = []
        self.info: dict[str, Any] = {}
        identity = self.client("sts").get_caller_identity()
        require(identity["Account"] == s.account and re.fullmatch(
            rf"arn:aws-cn:(iam|sts)::{s.account}:.+", identity["Arn"]
        ), "CallerIdentityMismatch")

    def client(self, name: str) -> Any:
        if name not in self.clients:
            self.clients[name] = self.session.client(name, config=Config(
                connect_timeout=5, read_timeout=20,
                retries={"mode": "standard", "total_max_attempts": 3},
                ignore_configured_endpoint_urls=True,
            ))
        return self.clients[name]

    def add(self, aws_service: str, method: str, *, result: str | None = None, **params: Any) -> str:
        name = f"step{len(self.actions)}"
        self.actions.append({"id": name, "service": aws_service, "method": method, "params": params})
        return f"@{name}.{result}" if result else name

    def execute(self) -> None:
        results: dict[str, Any] = {}

        def resolve(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("@step"):
                name, *path = value[1:].split(".")
                value = results[name]
                for key in path:
                    value = value[int(key)] if isinstance(value, list) else value[key]
                return value
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            if isinstance(value, list):
                return [resolve(v) for v in value]
            return value

        for action in self.actions:
            params = resolve(action["params"])
            client = self.client(action["service"])
            if action["method"] == "put_parameter":
                # Never put plaintext in the plan, output, exception message or SDK log.
                params["Value"] = secrets.token_urlsafe(48)
                previous = logging.root.manager.disable
                logging.disable(logging.CRITICAL)
                try:
                    results[action["id"]] = client.put_parameter(**params)
                finally:
                    params.pop("Value", None)
                    logging.disable(previous)
            else:
                results[action["id"]] = getattr(client, action["method"])(**params)
        self.info = resolve(self.info)

    def close(self) -> None:
        for client in self.clients.values():
            client.close()

    def role(self, name: str, policy: dict, *, must_exist: bool = False) -> None:
        iam = self.client("iam")
        trust = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": self.s.account},
                "ArnLike": {"aws:SourceArn": self.s.arn("ecs", "*")},
            },
        }]}
        result = optional(iam, "get_role", ("NoSuchEntity",), RoleName=name)
        policy_name = f"{name}-inline"
        if result is None:
            require(not must_exist, "PrepareRolesFirst")
            self.add("iam", "create_role", RoleName=name, Path="/", Tags=tags(),
                     AssumeRolePolicyDocument=json.dumps(trust))
        else:
            existing = result["Role"]
            owned(rows(iam, "list_role_tags", "Tags", RoleName=name))
            actual_trust = existing["AssumeRolePolicyDocument"]
            if isinstance(actual_trust, str):
                actual_trust = json.loads(unquote(actual_trust))
            require(existing["Arn"] == self.s.role(name) and existing["Path"] == "/"
                    and actual_trust == trust and not existing.get("PermissionsBoundary"),
                    "RoleTrustConflict")
            require(not rows(iam, "list_attached_role_policies", "AttachedPolicies", RoleName=name),
                    "RoleAttachedPolicyConflict")
            names = rows(iam, "list_role_policies", "PolicyNames", RoleName=name)
            require(set(names) <= {policy_name}, "RoleInlinePolicyConflict")
            if policy_name in names:
                current = iam.get_role_policy(RoleName=name, PolicyName=policy_name)["PolicyDocument"]
                if isinstance(current, str):
                    current = json.loads(unquote(current))
                if current == policy:
                    return
            require(not must_exist, "PrepareRolePolicyFirst")
        self.add("iam", "put_role_policy", RoleName=name, PolicyName=policy_name,
                 PolicyDocument=json.dumps(policy))

    def repository(self, name: str, *, must_exist: bool = False) -> None:
        ecr = self.client("ecr")
        result = optional(ecr, "describe_repositories", ("RepositoryNotFoundException",),
                          repositoryNames=[name])
        if result is None:
            require(not must_exist, "PrepareRepositoryFirst")
            self.add("ecr", "create_repository", repositoryName=name, tags=tags(),
                     imageTagMutability="IMMUTABLE",
                     imageScanningConfiguration={"scanOnPush": True},
                     encryptionConfiguration={"encryptionType": "AES256"})
        else:
            repo = one(result["repositories"], "RepositoryAmbiguous")
            require(repo and repo["repositoryArn"] == self.s.arn("ecr", f"repository/{name}"),
                    "RepositoryIdentityConflict")
            owned(ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"])["tags"])
            require(repo["imageTagMutability"] == "IMMUTABLE"
                    and repo.get("encryptionConfiguration", {}).get("encryptionType") == "AES256",
                    "RepositoryConfigurationConflict")

    def cluster(self, *, must_exist: bool = False) -> None:
        result = self.client("ecs").describe_clusters(clusters=[PROJECT], include=["TAGS"])
        require(all(f.get("reason") == "MISSING" for f in result.get("failures", [])),
                "ClusterLookupFailure")
        cluster = one(result["clusters"], "ClusterAmbiguous")
        if cluster:
            owned(cluster.get("tags", []))
            require(cluster["status"] == "ACTIVE"
                    and cluster["clusterArn"] == self.s.arn("ecs", f"cluster/{PROJECT}"),
                    "ClusterConfigurationConflict")
        else:
            require(not must_exist, "PrepareClusterFirst")
            self.add("ecs", "create_cluster", clusterName=PROJECT, tags=tags(lower=True))

    def prepare(self) -> None:
        s3 = self.client("s3")
        bucket_args = {"Bucket": self.s.bucket, "ExpectedBucketOwner": self.s.account}
        location = optional(s3, "get_bucket_location", ("NoSuchBucket",), **bucket_args)
        configure_encryption = location is None
        if location is None:
            self.add("s3", "create_bucket", Bucket=self.s.bucket,
                     CreateBucketConfiguration={"LocationConstraint": self.s.region},
                     ObjectOwnership="BucketOwnerEnforced")
            self.add("s3", "put_bucket_tagging", **bucket_args, Tagging={"TagSet": tags()})
        else:
            require(location["LocationConstraint"] == self.s.region, "BucketRegionConflict")
            owned(s3.get_bucket_tagging(**bucket_args)["TagSet"])
            policy = optional(s3, "get_bucket_policy_status", ("NoSuchBucketPolicy",), **bucket_args)
            require(not policy or not policy["PolicyStatus"]["IsPublic"], "PublicBucketPolicyConflict")
            acl = s3.get_bucket_acl(**bucket_args)
            require(all(g["Grantee"].get("Type") == "CanonicalUser"
                        and g["Grantee"].get("ID") == acl["Owner"]["ID"]
                        for g in acl["Grants"]), "BucketAclConflict")
            encryption = optional(
                s3, "get_bucket_encryption", ("ServerSideEncryptionConfigurationNotFoundError",),
                **bucket_args,
            )
            configure_encryption = encryption is None
            if encryption is not None:
                rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
                require(len(rules) == 1 and rules[0].get("ApplyServerSideEncryptionByDefault")
                        == {"SSEAlgorithm": "AES256"} and not rules[0].get("BucketKeyEnabled"),
                        "BucketEncryptionConflict")
        # Reconcile only owned bucket settings; each operation strengthens privacy.
        self.add("s3", "put_public_access_block", **bucket_args, PublicAccessBlockConfiguration=PUBLIC_BLOCK)
        self.add("s3", "put_bucket_versioning", **bucket_args, VersioningConfiguration={"Status": "Enabled"})
        if configure_encryption:
            self.add("s3", "put_bucket_encryption", **bucket_args, ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
            })
        for name in (f"{PROJECT}-backend", f"{PROJECT}-frontend"):
            self.repository(name)
        self.cluster()
        logs = self.client("logs")
        groups = [g for g in rows(logs, "describe_log_groups", "logGroups",
                                 logGroupNamePrefix=LOG_GROUP) if g["logGroupName"] == LOG_GROUP]
        group = one(groups, "LogGroupAmbiguous")
        if group:
            owned(logs.list_tags_log_group(logGroupName=LOG_GROUP)["tags"])
            require(not group.get("kmsKeyId"), "LogGroupKmsConflict")
        else:
            self.add("logs", "create_log_group", logGroupName=LOG_GROUP, tags=TAGS)
        self.add("logs", "put_retention_policy", logGroupName=LOG_GROUP, retentionInDays=7)
        self.role(EXEC_ROLE, execution_policy(self.s))
        self.role(WEB_ROLE, web_policy(self.s))
        self.info.update(bucket=self.s.bucket, registry=self.s.registry, cluster=PROJECT)

    def network(self) -> tuple[str, list[str]]:
        ec2 = self.client("ec2")
        vpcs = rows(ec2, "describe_vpcs", "Vpcs", Filters=[{"Name": "is-default", "Values": ["true"]}])
        require(len(vpcs) == 1, "DefaultVpcRequired")
        vpc = vpcs[0]["VpcId"]
        filters = [{"Name": "vpc-id", "Values": [vpc]}]
        tables = rows(ec2, "describe_route_tables", "RouteTables", Filters=filters)
        gateways = rows(ec2, "describe_internet_gateways", "InternetGateways", Filters=[
            {"Name": "attachment.vpc-id", "Values": [vpc]},
        ])
        gateway_ids = {g["InternetGatewayId"] for g in gateways if any(
            a["VpcId"] == vpc and a["State"] == "available" for a in g.get("Attachments", [])
        )}
        public = {}
        for subnet in sorted(rows(ec2, "describe_subnets", "Subnets", Filters=filters),
                             key=lambda sub: sub["SubnetId"]):
            if subnet.get("State") != "available" or subnet.get("AvailableIpAddressCount", 0) < 8:
                continue
            explicit = [t for t in tables if any(
                a.get("SubnetId") == subnet["SubnetId"] for a in t["Associations"]
            )]
            selected = explicit or [t for t in tables if any(a.get("Main") for a in t["Associations"])]
            if len(selected) == 1 and any(
                r.get("DestinationCidrBlock") == "0.0.0.0/0"
                and r.get("GatewayId") in gateway_ids and r.get("State") == "active"
                for r in selected[0]["Routes"]
            ):
                public.setdefault(subnet["AvailabilityZone"], subnet["SubnetId"])
        require(len(public) >= 2, "TwoPublicAvailabilityZonesRequired")
        return vpc, sorted(public.values())

    def group(self, name: str, vpc: str) -> Any:
        groups = rows(self.client("ec2"), "describe_security_groups", "SecurityGroups", Filters=[
            {"Name": "vpc-id", "Values": [vpc]}, {"Name": "group-name", "Values": [name]},
        ])
        group = one(groups, "SecurityGroupAmbiguous")
        if group:
            owned(group.get("Tags", []))
            require(group["VpcId"] == vpc and group["GroupName"] == name, "SecurityGroupIdentityConflict")
        return group

    def elb_resource(self, kind: str, name: str) -> Any:
        elb = self.client("elbv2")
        key, error, arn_key = (
            ("LoadBalancers", "LoadBalancerNotFound", "LoadBalancerArn") if kind == "load_balancers"
            else ("TargetGroups", "TargetGroupNotFound", "TargetGroupArn")
        )
        result = optional(elb, f"describe_{kind}", (error,), Names=[name])
        resource = one(result[key], "LoadBalancerResourceAmbiguous") if result else None
        if resource:
            tag_sets = elb.describe_tags(ResourceArns=[resource[arn_key]])["TagDescriptions"]
            require(len(tag_sets) == 1 and tag_sets[0]["ResourceArn"] == resource[arn_key],
                    "ResourceTagIdentityConflict")
            owned(tag_sets[0]["Tags"])
        return resource

    def edge(self, *, must_exist: bool = False) -> dict:
        vpc, subnets = self.network()
        alb_sg, task_sg = self.group(ALB_SG, vpc), self.group(TASK_SG, vpc)
        alb_id = alb_sg["GroupId"] if alb_sg else None
        alb_rule = ingress(9000, "0.0.0.0/0")
        task_rule = ingress(80, alb_id or "not-created", group=True)
        for group, expected in ((alb_sg, alb_rule), (task_sg, task_rule)):
            if group:
                actual = normalize_ingress(group.get("IpPermissions", []))
                require(actual in ([], normalize_ingress([expected])), "SecurityGroupIngressConflict")
                require(not must_exist or bool(actual), "EdgeIngressIncomplete")
        if alb_sg:
            require(allows_egress(alb_sg.get("IpPermissionsEgress", []), 80,
                                 task_sg["GroupId"] if task_sg else None),
                    "AlbSecurityGroupEgressConflict")
        if task_sg:
            require(allows_egress(task_sg.get("IpPermissionsEgress", []), 443),
                    "TaskSecurityGroupEgressConflict")
        tg = self.elb_resource("target_groups", TG_NAME)
        alb = self.elb_resource("load_balancers", ALB_NAME)
        elb = self.client("elbv2")
        listener = None
        if tg:
            require(tg["VpcId"] == vpc and tg["Port"] == 80 and tg["Protocol"] == "HTTP"
                    and tg["TargetType"] == "ip" and tg.get("HealthCheckPath") == "/healthz"
                    and tg.get("HealthCheckProtocol") == "HTTP"
                    and tg.get("HealthCheckPort") == "traffic-port"
                    and tg.get("Matcher") == {"HttpCode": "200"}, "TargetGroupConflict")
        if alb:
            require(alb["VpcId"] == vpc and alb["Scheme"] == "internet-facing"
                    and alb["Type"] == "application" and alb["IpAddressType"] == "ipv4"
                    and alb["SecurityGroups"] == [alb_id]
                    and {z["SubnetId"] for z in alb["AvailabilityZones"]} == set(subnets),
                    "LoadBalancerConflict")
            listeners = rows(elb, "describe_listeners", "Listeners", LoadBalancerArn=alb["LoadBalancerArn"])
            listener = one(listeners, "UnexpectedListeners")
            if listener:
                require(tg and listener["Protocol"] == "HTTP" and listener["Port"] == 9000
                        and forwards_to(listener["DefaultActions"], tg["TargetGroupArn"]),
                        "ListenerConflict")
                rules = rows(elb, "describe_rules", "Rules", ListenerArn=listener["ListenerArn"])
                require(len(rules) == 1 and rules[0]["IsDefault"]
                        and not rules[0].get("Conditions")
                        and forwards_to(rules[0]["Actions"], tg["TargetGroupArn"]), "ListenerRulesConflict")
        if must_exist:
            require(alb_sg and task_sg and tg and alb and listener, "EdgeIncomplete")
            return {"subnets": subnets, "task_sg": task_sg["GroupId"], "tg": tg["TargetGroupArn"],
                    "alb": alb["LoadBalancerArn"], "url": f"http://{alb['DNSName']}:9000"}
        if not alb_sg:
            alb_id = self.add("ec2", "create_security_group", result="GroupId", GroupName=ALB_SG,
                             Description="IELTS ALB HTTP9000", VpcId=vpc,
                             TagSpecifications=[{"ResourceType": "security-group", "Tags": tags()}])
        if not alb_sg or not alb_sg.get("IpPermissions"):
            self.add("ec2", "authorize_security_group_ingress", GroupId=alb_id, IpPermissions=[alb_rule])
        task_id = task_sg["GroupId"] if task_sg else self.add(
            "ec2", "create_security_group", result="GroupId", GroupName=TASK_SG,
            Description="IELTS web ALB-only", VpcId=vpc,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": tags()}],
        )
        if not task_sg or not task_sg.get("IpPermissions"):
            self.add("ec2", "authorize_security_group_ingress", GroupId=task_id,
                     IpPermissions=[ingress(80, alb_id, group=True)])
        tg_arn = tg["TargetGroupArn"] if tg else self.add(
            "elbv2", "create_target_group", result="TargetGroups.0.TargetGroupArn",
            Name=TG_NAME, Protocol="HTTP", Port=80, VpcId=vpc, TargetType="ip", Tags=tags(),
            HealthCheckProtocol="HTTP", HealthCheckPort="traffic-port", HealthCheckPath="/healthz",
            HealthCheckIntervalSeconds=30, HealthyThresholdCount=2, UnhealthyThresholdCount=3,
            Matcher={"HttpCode": "200"},
        )
        self.add("elbv2", "modify_target_group_attributes", TargetGroupArn=tg_arn,
                 Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}])
        alb_arn = alb["LoadBalancerArn"] if alb else self.add(
            "elbv2", "create_load_balancer", result="LoadBalancers.0.LoadBalancerArn",
            Name=ALB_NAME, Subnets=subnets, SecurityGroups=[alb_id], Scheme="internet-facing",
            Type="application", IpAddressType="ipv4", Tags=tags(),
        )
        self.add("elbv2", "modify_load_balancer_attributes", LoadBalancerArn=alb_arn,
                 Attributes=[{"Key": "idle_timeout.timeout_seconds", "Value": "120"}])
        if not listener:
            self.add("elbv2", "create_listener", LoadBalancerArn=alb_arn, Protocol="HTTP", Port=9000,
                     DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}], Tags=tags())
        self.info.update(alb_arn=alb_arn, target_group_arn=tg_arn, task_sg=task_id, subnets=subnets)
        if alb:
            self.info["url"] = f"http://{alb['DNSName']}:9000"
        return self.info

    def parameter(self) -> None:
        ssm = self.client("ssm")
        found = rows(ssm, "describe_parameters", "Parameters", ParameterFilters=[
            {"Key": "Name", "Option": "Equals", "Values": [PARAMETER]},
        ])
        param = one(found, "SessionParameterAmbiguous")
        if param:
            require(param["Name"] == PARAMETER and param["Type"] == "SecureString"
                    and param.get("KeyId") == "alias/aws/ssm", "SessionParameterConflict")
            owned(ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=PARAMETER)["TagList"])
        else:
            self.add("ssm", "put_parameter", Name=PARAMETER, Type="SecureString",
                     Value="<generated-in-memory>", KeyId="alias/aws/ssm",
                     Overwrite=False, Tags=tags(), Tier="Standard")

    def service(self) -> Any:
        response = self.client("ecs").describe_services(
            cluster=PROJECT, services=[SERVICE], include=["TAGS"],
        )
        require(all(f.get("reason") == "MISSING" for f in response.get("failures", [])),
                "ServiceLookupFailure")
        service = one(response["services"], "ServiceAmbiguous")
        if service:
            owned(service.get("tags", []))
            require(service["status"] == "ACTIVE" and service["serviceName"] == SERVICE,
                    "ServiceStateConflict")
        return service

    def web(self) -> None:
        s = self.s
        self.cluster(must_exist=True)
        self.repository(f"{PROJECT}-frontend", must_exist=True)
        image = self.client("ecr").describe_images(
            repositoryName=f"{PROJECT}-frontend", imageIds=[{"imageDigest": s.image.split("@")[1]}],
        )["imageDetails"]
        require(len(image) == 1 and image[0]["imageDigest"] == s.image.split("@")[1], "ImageDigestConflict")
        runtime = self.client("bedrock-agentcore-control").get_agent_runtime(
            agentRuntimeId=s.runtime_arn.rsplit("/", 1)[1],
        )
        require(runtime["agentRuntimeArn"] == s.runtime_arn and runtime["status"] == "READY",
                "RuntimeNotReady")
        self.role(EXEC_ROLE, execution_policy(s), must_exist=True)
        self.role(WEB_ROLE, web_policy(s), must_exist=True)
        edge = self.edge(must_exist=True)
        ecs = self.client("ecs")
        service = self.service()
        if service:
            require(service.get("launchType") == "FARGATE"
                    and service.get("deploymentController", {}).get("type", "ECS") == "ECS"
                    and service.get("loadBalancers") == [
                        {"targetGroupArn": edge["tg"], "containerName": "web", "containerPort": 80}
                    ] and not service.get("serviceRegistries"), "ServiceRoutingConflict")
            net = service["networkConfiguration"]["awsvpcConfiguration"]
            require(net["securityGroups"] == [edge["task_sg"]]
                    and set(net["subnets"]) == set(edge["subnets"])
                    and net["assignPublicIp"] == "ENABLED", "ServiceNetworkConflict")
            self.info["previous_task_definition"] = service["taskDefinition"]
        definitions = rows(ecs, "list_task_definitions", "taskDefinitionArns",
                           familyPrefix=FAMILY, status="ACTIVE", sort="DESC")
        exact = [arn for arn in definitions if re.fullmatch(
            re.escape(s.arn("ecs", f"task-definition/{FAMILY}:")) + r"[0-9]+", arn
        )]
        latest = ecs.describe_task_definition(taskDefinition=exact[0], include=["TAGS"]) if exact else None
        if latest:
            owned(latest.get("tags", []))
        if service and service["taskDefinition"] not in exact[:1]:
            owned(ecs.describe_task_definition(
                taskDefinition=service["taskDefinition"], include=["TAGS"],
            ).get("tags", []))
        self.parameter()
        desired = task_definition(s)
        current = latest["taskDefinition"] if latest else None
        td_arn = current["taskDefinitionArn"] if current and (
            normalized_task_definition(current) == normalized_task_definition(desired)
        ) else self.add("ecs", "register_task_definition", result="taskDefinition.taskDefinitionArn", **desired)
        deployment = {"deploymentCircuitBreaker": {"enable": True, "rollback": True},
                      "maximumPercent": 200, "minimumHealthyPercent": 100}
        net_config = {"awsvpcConfiguration": {
            "subnets": edge["subnets"], "securityGroups": [edge["task_sg"]], "assignPublicIp": "ENABLED",
        }}
        common = dict(cluster=PROJECT, taskDefinition=td_arn, desiredCount=1,
                      deploymentConfiguration=deployment, networkConfiguration=net_config,
                      healthCheckGracePeriodSeconds=90, platformVersion="1.4.0")
        if service:
            if not service_matches(service, common):
                self.add("ecs", "update_service", service=SERVICE, propagateTags="TASK_DEFINITION", **common)
        else:
            self.add("ecs", "create_service", serviceName=SERVICE, launchType="FARGATE",
                     loadBalancers=[{"targetGroupArn": edge["tg"], "containerName": "web", "containerPort": 80}],
                     tags=tags(lower=True), propagateTags="TASK_DEFINITION", **common)
        self.info.update(url=edge["url"], task_definition=td_arn, image=s.image, runtime_arn=s.runtime_arn)

    def status(self) -> None:
        alb = self.elb_resource("load_balancers", ALB_NAME)
        tg = self.elb_resource("target_groups", TG_NAME)
        self.cluster(must_exist=True)
        service = self.service()
        self.info["service"] = None if not service else {
            key: service.get(key) for key in ("serviceName", "desiredCount", "runningCount", "pendingCount",
                                            "taskDefinition")
        }
        if service:
            self.info["deployments"] = [
                {key: d.get(key) for key in ("status", "rolloutState", "taskDefinition",
                                           "desiredCount", "runningCount", "pendingCount")}
                for d in service.get("deployments", [])
            ]
        if alb:
            self.info.update(url=f"http://{alb['DNSName']}:9000", alb_state=alb["State"]["Code"])
        if tg:
            self.info["targets"] = [
                {"target": h["Target"], "state": h["TargetHealth"]["State"],
                 "reason": h["TargetHealth"].get("Reason")}
                for h in self.client("elbv2").describe_target_health(
                    TargetGroupArn=tg["TargetGroupArn"]
                )["TargetHealthDescriptions"]
            ]


def normalized_task_definition(definition: dict) -> dict:
    """Compare task semantics without treating SDK defaults as a new release.

    Unknown non-default settings remain visible. Ordered command/entryPoint arrays
    are never sorted or dropped, so a real launch change still creates a revision.
    """
    value = copy.deepcopy(definition)
    for key in ("taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
                "registeredAt", "registeredBy", "deregisteredAt", "tags"):
        value.pop(key, None)
    for key, default in (
        ("volumes", []), ("placementConstraints", []), ("inferenceAccelerators", []),
        ("enableFaultInjection", False), ("ephemeralStorage", {"sizeInGiB": 20}),
    ):
        if value.get(key) == default:
            value.pop(key)
    if "requiresCompatibilities" in value:
        value["requiresCompatibilities"].sort()
    for container in value.get("containerDefinitions", []):
        for key, default in (
            ("cpu", 0), ("mountPoints", []), ("volumesFrom", []), ("environmentFiles", []),
            ("dependsOn", []), ("resourceRequirements", []), ("ulimits", []),
            ("extraHosts", []), ("dockerLabels", {}), ("environment", []), ("secrets", []),
            ("systemControls", []), ("portMappings", []),
        ):
            if container.get(key) == default:
                container.pop(key)
        for key, sort_key in (
            ("environment", "name"), ("secrets", "name"), ("systemControls", "namespace"),
        ):
            if key in container:
                container[key].sort(key=lambda row: row[sort_key])
        for port in container.get("portMappings", []):
            port.setdefault("protocol", "tcp")
            if value.get("networkMode") == "awsvpc" and port.get("hostPort") == port.get("containerPort"):
                port.pop("hostPort", None)
        if "portMappings" in container:
            container["portMappings"].sort(key=lambda row: json.dumps(row, sort_keys=True))
        logging_config = container.get("logConfiguration", {})
        if logging_config.get("secretOptions") == []:
            logging_config.pop("secretOptions")
    if "containerDefinitions" in value:
        value["containerDefinitions"].sort(key=lambda row: row["name"])
    return value


def service_matches(service: dict, desired: dict) -> bool:
    """Compare managed service settings; observed rollout counts are not intent."""
    if service.get("propagateTags") != "TASK_DEFINITION":
        return False
    for key, value in desired.items():
        if key == "cluster":  # describe_services returns the full cluster ARN.
            continue
        actual = service.get(key)
        if key == "networkConfiguration":
            # Routing and network identity were checked before reaching this point.
            continue
        if key == "deploymentConfiguration":
            # ECS returns these circuit-breaker defaults even when create_service
            # omitted them. Normalize only the known default values; altered
            # thresholds/reset behavior must still be detected as configuration drift.
            actual = copy.deepcopy(actual)
            if isinstance(actual, dict):
                breaker = actual.get("deploymentCircuitBreaker")
                desired_breaker = value.get("deploymentCircuitBreaker", {})
                if isinstance(breaker, dict):
                    for field, default in (
                        ("resetOnHealthyTask", True),
                        ("thresholdConfiguration", {"type": "BOUNDED_PERCENT", "value": 50}),
                    ):
                        if field not in desired_breaker and breaker.get(field) == default:
                            breaker.pop(field)
            if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in value.items()):
                return False
        elif actual != value:
            return False
    return True


def allows_egress(permissions: list, port: int, target_group: str | None = None) -> bool:
    """Support default IPv4 egress or a scoped ALB-to-task rule; never broaden it."""
    for rule in permissions:
        protocol = rule.get("IpProtocol")
        if protocol != "-1" and not (
            protocol in ("tcp", "6")
            and rule.get("FromPort", -1) <= port <= rule.get("ToPort", -1)
        ):
            continue
        if any(source.get("CidrIp") == "0.0.0.0/0" for source in rule.get("IpRanges", [])):
            return True
        if target_group and any(
            source.get("GroupId") == target_group and not source.get("VpcPeeringConnectionId")
            for source in rule.get("UserIdGroupPairs", [])
        ):
            return True
    return False


def ingress(port: int, source: str, *, group: bool = False) -> dict:
    return {"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
            ("UserIdGroupPairs" if group else "IpRanges"):
                [{("GroupId" if group else "CidrIp"): source}]}


def normalize_ingress(permissions: list) -> list:
    normalized = []
    for p in permissions:
        sources = []
        for kind, key in (("IpRanges", "CidrIp"), ("Ipv6Ranges", "CidrIpv6"),
                          ("UserIdGroupPairs", "GroupId"), ("PrefixListIds", "PrefixListId")):
            for source in p.get(kind, []):
                # VPC peering sources are never equivalent to our local ALB.
                require(not source.get("VpcPeeringConnectionId"), "PeeredIngressConflict")
                sources.append((kind, source[key]))
        normalized.append((p["IpProtocol"], p.get("FromPort"), p.get("ToPort"), tuple(sorted(sources))))
    return sorted(normalized)


def forwards_to(actions: list, target: str) -> bool:
    if len(actions) != 1 or actions[0].get("Type") != "forward":
        return False
    action = actions[0]
    if action.get("TargetGroupArn") != target:
        return False
    config = action.get("ForwardConfig", {})
    return (not config.get("TargetGroupStickinessConfig", {}).get("Enabled")
            and (not config.get("TargetGroups") or
                 config["TargetGroups"] in ([{"TargetGroupArn": target, "Weight": 1}],
                                            [{"TargetGroupArn": target}])))


def policy(statements: list[tuple[list[str], Any]]) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": actions, "Resource": resource} for actions, resource in statements
    ]}


def execution_policy(s: Settings) -> dict:
    return policy([
        (["ecr:GetAuthorizationToken"], "*"),
        (["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
         s.arn("ecr", f"repository/{PROJECT}-frontend")),
        (["logs:CreateLogStream", "logs:PutLogEvents"], s.arn("logs", f"log-group:{LOG_GROUP}:log-stream:*")),
        (["ssm:GetParameters"], s.parameter_arn),
    ])


def web_policy(s: Settings) -> dict:
    runtime = s.arn("bedrock-agentcore", "runtime/ielts_part1_runtime-*")
    return policy([
        (["bedrock-agentcore:InvokeAgentRuntime"], [runtime, f"{runtime}/endpoint/*"]),
        (["s3:GetObject", "s3:PutObject"], f"arn:aws-cn:s3:::{s.bucket}/*"),
        (["s3:ListBucket"], f"arn:aws-cn:s3:::{s.bucket}"),
    ])


def task_definition(s: Settings) -> dict:
    environment = {
        "AWS_REGION": s.region, "AGENT_RUNTIME_ARN": s.runtime_arn,
        "IELTS_AUDIO_BUCKET": s.bucket, "ALLOWED_EMAIL_DOMAINS": s.email_domains,
        "WEB_FANOUT_CONCURRENCY": "20", "USER_STORE_S3_BUCKET": s.bucket,
        "USER_STORE_S3_KEY": "web/users.json", "PORT": "80",
    }
    return {
        "family": FAMILY, "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"],
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "cpu": "512", "memory": "1024", "executionRoleArn": s.role(EXEC_ROLE),
        "taskRoleArn": s.role(WEB_ROLE), "tags": tags(lower=True),
        "containerDefinitions": [{
            "name": "web", "image": s.image, "essential": True, "user": "10001",
            "portMappings": [{"containerPort": 80, "protocol": "tcp"}],
            "systemControls": [{"namespace": "net.ipv4.ip_unprivileged_port_start", "value": "0"}],
            "environment": [{"name": k, "value": v} for k, v in environment.items()],
            "secrets": [{"name": "SESSION_SECRET", "valueFrom": s.parameter_arn}],
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": LOG_GROUP, "awslogs-region": s.region, "awslogs-stream-prefix": "web",
            }},
        }],
    }


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "edge", "web", "status"])
    for name in ("profile", "expected-account", "region"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--image")
    parser.add_argument("--runtime-arn")
    parser.add_argument("--email-domains", default="example.com")
    parser.add_argument("--apply", action="store_true")
    infra = None
    try:
        args = parser.parse_args(argv)
        infra = Infra(validate(args))
        getattr(infra, args.stage)()
        if args.apply:
            infra.execute()
        print(json.dumps({"stage": args.stage, "mode": "apply" if args.apply else "dry-run",
                          "actions": infra.actions, **infra.info}, indent=2, sort_keys=True))
        return 0
    except Conflict as exc:
        print(json.dumps({"error": "Conflict", "code": exc.args[0]}), file=sys.stderr)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        safe = code if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", code) else "UnknownAwsError"
        print(json.dumps({"error": "ClientError", "code": safe}), file=sys.stderr)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
    finally:
        if infra:
            infra.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
