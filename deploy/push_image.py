"""Push a verified ARM64 image to immutable China ECR; dry-run by default."""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import subprocess
import tempfile

import boto3
from botocore.config import Config


def push(args) -> dict:
    if not args.profile.strip():
        raise ValueError("Explicit deployment profile required")
    if args.region not in ("cn-northwest-1", "cn-north-1") or not re.fullmatch(r"[0-9]{12}", args.account):
        raise ValueError("China account and region required")
    if args.repository not in ("ielts-part1-backend", "ielts-part1-frontend"):
        raise ValueError("Project repository required")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", args.tag):
        raise ValueError("Invalid immutable release tag")
    config = Config(connect_timeout=5, read_timeout=20,
                    retries={"mode": "standard", "total_max_attempts": 2},
                    ignore_configured_endpoint_urls=True)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    identity = session.client("sts", config=config).get_caller_identity()
    if identity["Account"] != args.account or not identity["Arn"].startswith("arn:aws-cn:"):
        raise ValueError("China identity mismatch")
    ecr = session.client("ecr", config=config)
    registry = f"{args.account}.dkr.ecr.{args.region}.amazonaws.com.cn"
    repo = ecr.describe_repositories(repositoryNames=[args.repository])["repositories"][0]
    if repo["imageTagMutability"] != "IMMUTABLE" or repo["repositoryUri"] != f"{registry}/{args.repository}":
        raise ValueError("Immutable China repository required")
    try:
        ecr.describe_images(repositoryName=args.repository, imageIds=[{"imageTag": args.tag}])
    except ecr.exceptions.ImageNotFoundException:
        pass
    else:
        raise ValueError("Release tag already exists; never overwrite it")
    result = subprocess.run(["docker", "image", "inspect", args.local_image],
                            check=True, capture_output=True, text=True, timeout=30)
    local = json.loads(result.stdout)[0]
    if local["Architecture"] != "arm64" or local["Os"] != "linux":
        raise ValueError("Linux ARM64 local image required")
    if args.repository == "ielts-part1-backend" and local["Size"] > 2_000_000_000:
        raise ValueError("Runtime image exceeds project size limit")
    image = f"{registry}/{args.repository}:{args.tag}"
    report = {"mode": "apply" if args.apply else "dry-run", "image": image,
              "local_id": local["Id"], "size_bytes": local["Size"], "architecture": local["Architecture"]}
    if not args.apply:
        return report
    auth = ecr.get_authorization_token(registryIds=[args.account])["authorizationData"][0]
    if auth["proxyEndpoint"] != f"https://{registry}":
        raise ValueError("ECR authentication endpoint mismatch")
    username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    if username != "AWS" or not password:
        raise ValueError("Invalid ECR authorization")
    context = subprocess.run(["docker", "context", "show"], check=True,
                             capture_output=True, text=True, timeout=30).stdout.strip()
    context_args = []
    # The password is passed through stdin; Docker auth config is removed after push.
    with tempfile.TemporaryDirectory(prefix="ielts-ecr-auth-") as directory:
        # A fresh --config loses Desktop/named context metadata as well as auth.
        # Export/import preserves its endpoint and TLS material only in this
        # temporary directory; neither the export nor credentials are printed.
        if context != "default":
            exported = subprocess.run(["docker", "context", "export", context, "-"],
                                      check=True, capture_output=True, timeout=30)
            subprocess.run(["docker", "--config", directory, "context", "import",
                            context, "-"], input=exported.stdout,
                           check=True, capture_output=True, timeout=30)
            context_args = ["--context", context]
        subprocess.run(["docker", "--config", directory, "login", "--username", "AWS",
                        "--password-stdin", registry], input=password, text=True,
                       capture_output=True, check=True, timeout=60)
        subprocess.run(["docker", "tag", local["Id"], image], check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "--config", directory, *context_args, "push", image],
                       check=True, capture_output=True, text=True, timeout=1200)
    detail = ecr.describe_images(repositoryName=args.repository, imageIds=[{"imageTag": args.tag}])["imageDetails"][0]
    report["digest"] = detail["imageDigest"]
    report["pinned_image"] = f"{registry}/{args.repository}@{detail['imageDigest']}"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="cn-northwest-1")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--local-image", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(push(args)))
        return 0
    except Exception as exc:
        report = {"error": type(exc).__name__}
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9]{1,80}", code):
            report["code"] = code
        print(json.dumps(report))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
