"""Executable fake AWS CLI for China edge integration tests; never calls AWS."""

import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
operation = tuple(args[:2])
with open(os.environ["FAKE_AWS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
state_path = Path(os.environ["FAKE_EDGE_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}


def arg(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def output(value):
    print(json.dumps(value) if isinstance(value, (dict, list)) else value)


def missing(code):
    print(f"An error occurred ({code}) when calling fake AWS", file=sys.stderr)
    sys.exit(254)


def permission(port, source, group=False):
    return {
        "IpProtocol": "tcp", "FromPort": int(port), "ToPort": int(port),
        "UserIdGroupPairs" if group else "IpRanges":
            [{"GroupId" if group else "CidrIp": source}],
    }


def task_definition(revision):
    account = "123456789012"
    return {
        "status": "ACTIVE", "family": "ielts-part1-web", "networkMode": "awsvpc",
        "executionRoleArn": f"arn:aws-cn:iam::{account}:role/ielts-part1-ecs-exec",
        "taskRoleArn": f"arn:aws-cn:iam::{account}:role/ielts-part1-web-task",
        "containerDefinitions": [{
            "name": "web",
            "image": f"{account}.dkr.ecr.cn-northwest-1.amazonaws.com.cn/ielts-part1-frontend:tag-{revision}",
            "portMappings": [{"containerPort": 80}],
            "environment": [
                {"name": "AWS_REGION", "value": "cn-northwest-1"},
                {"name": "PORT", "value": "80"},
                {"name": "AGENT_RUNTIME_ARN", "value":
                 f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:{account}:runtime/test-runtime"},
            ],
        }],
    }


if operation == ("sts", "get-caller-identity"):
    output("123456789012")
elif operation == ("ec2", "describe-security-groups"):
    name = next(x.split("Values=", 1)[1] for x in args if x.startswith("Name=group-name"))
    output({"SecurityGroups": [state[name]] if name in state else []})
elif operation == ("ec2", "create-security-group"):
    name = arg("--group-name")
    group_id = "sg-alb" if name.endswith("-alb-sg") else "sg-task"
    state[name] = {"GroupId": group_id, "VpcId": arg("--vpc-id"), "IpPermissions": []}
    output(group_id)
elif operation == ("ec2", "authorize-security-group-ingress"):
    sg = next(row for row in state.values()
              if isinstance(row, dict) and row.get("GroupId") == arg("--group-id"))
    sg["IpPermissions"].append(permission(
        arg("--port"), arg("--source-group") or arg("--cidr"), "--source-group" in args,
    ))
elif operation == ("elbv2", "describe-target-groups"):
    if "tg" not in state:
        missing("TargetGroupNotFound")
    if "--query" in args:
        output(state["tg"]["TargetGroupArn"])
    else:
        output({"TargetGroups": [state["tg"]]})
elif operation == ("elbv2", "create-target-group"):
    state["tg"] = {
        "TargetGroupArn": "arn:aws-cn:elasticloadbalancing:cn-northwest-1:123456789012:targetgroup/test/1",
        "VpcId": arg("--vpc-id"), "Port": int(arg("--port")),
        "Protocol": arg("--protocol"), "TargetType": arg("--target-type"),
    }
    output(state["tg"]["TargetGroupArn"])
elif operation == ("elbv2", "describe-load-balancers"):
    if os.environ.get("FAKE_ALB_ACCESS_DENIED"):
        missing("AccessDenied")
    if "alb" not in state:
        missing("LoadBalancerNotFound")
    if "--query" in args:
        output("test-alb.cn-northwest-1.elb.amazonaws.com.cn")
    else:
        output({"LoadBalancers": [state["alb"]]})
elif operation == ("elbv2", "create-load-balancer"):
    state["alb"] = {
        "LoadBalancerArn": "arn:aws-cn:elasticloadbalancing:cn-northwest-1:123456789012:loadbalancer/app/test/1",
        "VpcId": "vpc-test", "Scheme": arg("--scheme"), "Type": arg("--type"),
        "SecurityGroups": [arg("--security-groups")],
    }
    output(state["alb"]["LoadBalancerArn"])
elif operation == ("elbv2", "describe-listeners"):
    output({"Listeners": state.get("listeners", [])})
elif operation == ("elbv2", "create-listener"):
    state["listeners"] = [{
        "ListenerArn": "arn:aws-cn:elasticloadbalancing:cn-northwest-1:123456789012:listener/app/test/1",
        "Port": int(arg("--port")), "Protocol": arg("--protocol"),
        "DefaultActions": [{
            "Type": "forward",
            "TargetGroupArn": arg("--default-actions").split("TargetGroupArn=")[1],
        }],
    }]
elif operation == ("elbv2", "describe-rules"):
    output({"Rules": state.get("rules", [{"IsDefault": True}])})
elif operation in {
    ("elbv2", "modify-target-group"), ("elbv2", "modify-target-group-attributes"),
    ("elbv2", "modify-load-balancer-attributes"), ("elbv2", "wait"),
}:
    pass
elif operation == ("s3api", "head-bucket"):
    pass
elif operation == ("s3", "ls"):
    pass
elif operation == ("ecr", "describe-repositories"):
    output(state.get("mutability", "IMMUTABLE") if "--query" in args else {})
elif operation == ("ecr", "list-images"):
    output("test-tag")
elif operation == ("ecr", "describe-images"):
    if state.get("image_error"):
        missing(state["image_error"])
    output({})
elif operation == ("ecs", "describe-clusters"):
    output("ACTIVE")
elif operation == ("ecs", "describe-services"):
    if "--query" not in args:
        output({"services": [{
            "status": "ACTIVE",
            "networkConfiguration": {"awsvpcConfiguration": {"securityGroups": ["sg-task"]}},
            "loadBalancers": [{
                "targetGroupArn": state["tg"]["TargetGroupArn"],
                "containerName": "web", "containerPort": 80,
            }],
        }]})
    elif arg("--query") == "services[0].taskDefinition":
        output("ielts-part1-web:" + str(state.get("current_revision", 1)))
    else:
        output("1")
elif operation == ("ecs", "describe-task-definition"):
    revision = arg("--task-definition").split(":")[-1]
    output({"taskDefinition": state.get("taskdefs", {}).get(revision, task_definition(revision))})
elif operation == ("ecs", "update-service"):
    if "--task-definition" in args:
        state["current_revision"] = arg("--task-definition").split(":")[-1]
    output("1")
elif operation == ("ecs", "wait"):
    pass
elif operation == ("bedrock-agentcore-control", "list-agent-runtimes"):
    output({"agentRuntimes": state.get("runtimes", [])})
else:
    print(f"UNEXPECTED AWS CALL: {args!r}", file=sys.stderr)
    sys.exit(97)
state_path.write_text(json.dumps(state))
