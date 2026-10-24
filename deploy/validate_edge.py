#!/usr/bin/env python3
"""Offline JSON validators for deployment scripts; no AWS calls or SDK dependencies.

stdin is an AWS JSON result (not a --query projection). Conflicts exit 1 with
ERROR: edge conflict. Missing/approved resources print their ID, or an empty line.
"""
import ipaddress
import json
import re
import sys


def fail(message):
    raise ValueError(message)


def one(data, key):
    items = data[key]
    if not isinstance(items, list) or len(items) > 1:
        fail(f"expected at most one {key} resource")
    return items[0] if items else None


def validate(mode, args, data):
    if mode == "runtime":
        name, region, account = args
        runtimes = [r for r in data["agentRuntimes"] if r["agentRuntimeName"] == name]
        if len(runtimes) != 1 or runtimes[0]["status"] != "READY":
            fail("China model/runtime is unavailable or not READY; provider implementation remains pending")
        arn = runtimes[0]["agentRuntimeArn"]
        if not re.fullmatch(f"arn:aws-cn:bedrock-agentcore:{region}:{account}:runtime/[A-Za-z0-9_-]+", arn):
            fail("runtime must belong to the selected China account and region")
        print(arn)
    elif mode == "taskdef":
        region, account, registry, family = args
        td = data["taskDefinition"]
        if td["status"] != "ACTIVE" or td["family"] != family or td["networkMode"] != "awsvpc":
            fail("rollback requires an ACTIVE web task definition with awsvpc networking")
        for field in ("executionRoleArn", "taskRoleArn"):
            if not td[field].startswith(f"arn:aws-cn:iam::{account}:role/"):
                fail("rollback roles must belong to the selected China account")
        containers = td["containerDefinitions"]
        if len(containers) != 1 or containers[0]["name"] != "web":
            fail("rollback requires exactly the web container")
        web = containers[0]
        image = web["image"]
        if not re.fullmatch(re.escape(registry) + r"(?::[\w][\w.-]{0,127}|@sha256:[0-9a-f]{64})", image):
            fail("rollback image must belong to the selected China web repository")
        ports = web["portMappings"]
        if len(ports) != 1 or ports[0]["containerPort"] != 80 or ports[0].get("protocol", "tcp") != "tcp":
            fail("rollback web container must listen on tcp/80")
        if web.get("environmentFiles"):
            fail("rollback environmentFiles require manual review")
        env = {entry["name"]: entry["value"] for entry in web["environment"]}
        if len(env) != len(web["environment"]):
            fail("rollback environment contains duplicate names")
        if env.get("AWS_REGION") != region or env.get("AWS_DEFAULT_REGION", region) != region:
            fail("rollback web region must match the selected China region")
        if env.get("PORT") != "80":
            fail("rollback PORT must be 80")
        runtime = env.get("AGENT_RUNTIME_ARN", "")
        if not re.fullmatch(f"arn:aws-cn:bedrock-agentcore:{region}:{account}:runtime/[A-Za-z0-9_-]+", runtime):
            fail("rollback Runtime must belong to the selected China account and region")
        for name in env:
            if name.startswith(("IELTS_MODEL_", "AWS_ENDPOINT_URL", "AWS_BEARER_TOKEN")):
                fail("rollback cannot introduce model credentials or endpoint overrides")
        for secret in web.get("secrets", []):
            if secret["name"] != "SESSION_SECRET" or not secret["valueFrom"].startswith(
                f"arn:aws-cn:ssm:{region}:{account}:parameter/"
            ):
                fail("rollback only supports the China SSM session secret")
        print(image)
        print(runtime)
        print(env.get("AGENT_RUNTIME_QUALIFIER", ""))
    elif mode == "service-target":
        target, = args
        service = one(data, "services")
        if any(failure.get("reason") != "MISSING" for failure in data.get("failures", [])):
            fail("could not inspect the existing web service")
        if not service or service["status"] == "INACTIVE":
            print("MISSING")
        elif service["status"] != "ACTIVE":
            fail("existing web service is not ACTIVE; wait for deletion to finish")
        elif service["loadBalancers"] != [{
            "targetGroupArn": target, "containerName": "web", "containerPort": 80,
        }]:
            fail("existing service has a conflicting load balancer attachment; review manually")
        else:
            print("ACTIVE")
    elif mode == "service":
        sg, target = args
        service = one(data, "services")
        if data.get("failures") or not service or service["status"] != "ACTIVE":
            fail("web service must exist and be ACTIVE")
        network = service["networkConfiguration"]["awsvpcConfiguration"]
        if network["securityGroups"] != [sg]:
            fail("web service must use only the validated task security group")
        if service["loadBalancers"] != [{
            "targetGroupArn": target, "containerName": "web", "containerPort": 80,
        }]:
            fail("web service must attach only the selected ALB target group on web:80")
    elif mode == "sg":
        kind, port, source, vpc = args
        sg = one(data, "SecurityGroups")
        if not sg:
            print("")
            return
        if sg["VpcId"] != vpc:
            fail("security group VPC mismatch")
        permissions = sg["IpPermissions"]
        present = False
        for rule in permissions:
            if str(rule["IpProtocol"]) not in ("tcp", "6") or rule.get("FromPort") != int(port) or rule.get("ToPort") != int(port):
                fail(f"{kind} SG has an unapproved protocol/port range; review existing ingress manually")
            cidrs = [r["CidrIp"] for r in rule.get("IpRanges", [])]
            groups = [r["GroupId"] for r in rule.get("UserIdGroupPairs", [])]
            if rule.get("Ipv6Ranges") or rule.get("PrefixListIds"):
                fail(f"{kind} SG has unapproved IPv6/prefix-list ingress")
            if kind == "alb":
                if groups or len(cidrs) != 1 or ipaddress.ip_network(cidrs[0]) != ipaddress.ip_network(source):
                    fail("ALB SG ingress conflicts with INGRESS_CIDR; review existing rules manually")
            elif cidrs or groups != [source]:
                fail("task SG must admit only the ALB security group; review existing rules manually")
            present = True
        print(sg["GroupId"] + (" present" if present else " missing"))
    elif mode == "tg":
        vpc, port = args
        tg = one(data, "TargetGroups")
        if tg:
            if (tg["VpcId"], tg["Port"], tg["Protocol"], tg["TargetType"]) != (vpc, int(port), "HTTP", "ip"):
                fail("target group must be HTTP/WEB_PORT, target-type ip, in the selected VPC")
            print(tg["TargetGroupArn"])
    elif mode == "alb":
        vpc, sg = args
        alb = one(data, "LoadBalancers")
        if alb:
            if (alb["VpcId"], alb["Scheme"], alb["Type"]) != (vpc, "internet-facing", "application"):
                fail("ALB must be internet-facing/application in the selected VPC")
            if alb["SecurityGroups"] != [sg]:
                fail("ALB has unexpected attached security groups")
            print(alb["LoadBalancerArn"])
    elif mode == "listeners":
        port, target = args
        listeners = data["Listeners"]
        if len(listeners) > 1:
            fail("unexpected extra ALB listeners; review manually")
        for listener in listeners:
            if listener["Port"] != int(port) or listener["Protocol"] != "HTTP":
                fail("existing listener conflicts with ALB_PUBLIC_PORT/HTTP; review manually")
            actions = listener["DefaultActions"]
            if len(actions) != 1 or actions[0]["Type"] != "forward":
                fail("listener has unexpected default actions")
            action = actions[0]
            targets = action.get("ForwardConfig", {}).get("TargetGroups", [])
            if action.get("TargetGroupArn", target if targets else None) != target or any(t["TargetGroupArn"] != target for t in targets):
                fail("listener forwards to an unexpected target group")
            print(listener["ListenerArn"])
    elif mode == "rules":
        rules = data["Rules"]
        if len(rules) != 1 or not rules[0].get("IsDefault"):
            fail("listener has extra routing rules; review manually")
    elif mode == "cidr":
        network = ipaddress.ip_network(args[0], strict=True)
        if network.version != 4:
            fail("INGRESS_CIDR must be an IPv4 CIDR")
    else:
        fail(f"unknown validator {mode}")


def main():
    try:
        mode = sys.argv[1]
        validate(mode, sys.argv[2:], {} if mode == "cidr" else json.load(sys.stdin))
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"ERROR: edge conflict: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
