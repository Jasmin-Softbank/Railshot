#!/usr/bin/env python3
"""Render private platform workloads from published immutable image references."""
import argparse
import json
from pathlib import Path
import re

import yaml


def image_ref(images, name):
    value = images.get(name, "")
    if not re.fullmatch(rf"ghcr\.io/jasmin-softbank/railshot-{name}@sha256:[a-f0-9]{{64}}", value):
        raise ValueError(f"{name}: published Railshot GHCR digest required")
    return value


def render(images, target_id, dashboard_node_port=None):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", target_id):
        raise ValueError("operator-approved target_id is required")
    if dashboard_node_port is not None and not 30000 <= dashboard_node_port <= 32767:
        raise ValueError("dashboard NodePort must be in 30000..32767")
    for name in ("dashboard", "api"):
        image_ref(images, name)
    source = Path(__file__).resolve().parents[1] / "manifests/platform.yaml"
    documents = list(yaml.safe_load_all(source.read_text()))
    for document in documents:
        if dashboard_node_port and document["kind"] == "Service" and document["metadata"]["name"] == "railshot-dashboard":
            document["spec"]["type"] = "NodePort"
            document["spec"]["externalTrafficPolicy"] = "Local"
            document["spec"]["ports"][0]["nodePort"] = dashboard_node_port
        if document["kind"] != "Deployment":
            continue
        container = document["spec"]["template"]["spec"]["containers"][0]
        container["image"] = images[container["name"]]
        for init in document["spec"]["template"]["spec"].get("initContainers", []):
            init["image"] = images["api"]
        for item in container.get("env", []):
            if item["name"] == "RAILSHOT_TARGET_ID":
                item["value"] = target_id
    return {"apiVersion": "v1", "kind": "List", "items": documents}


def render_build_runner(images, name, url, node):
    # Keep privileged build Job/RBAC outside the restricted platform Argo project.
    for value in (name, node):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
            raise ValueError("reviewed runner and build-node names are required")
    if not isinstance(url, str) or not re.fullmatch(r"https://github\.com/Jasmin-Softbank/[A-Za-z0-9_.-]+", url):
        raise ValueError("a reviewed Jasmin-Softbank apps repository URL is required")
    image = image_ref(images, "ci-runner")
    source = Path(__file__).resolve().parents[1] / "manifests/build-runner.yaml"
    documents = list(yaml.safe_load_all(source.read_text()))
    next(item for item in documents if item["kind"] == "ClusterRole")["rules"][0]["resourceNames"] = [node]
    job = next(item for item in documents if item["kind"] == "Job")
    job["metadata"]["name"] = name
    pod = job["spec"]["template"]["spec"]
    pod["nodeSelector"]["kubernetes.io/hostname"] = node
    runner = pod["containers"][0]
    runner["image"] = image
    for item in runner["env"]:
        if item["name"] == "RAILSHOT_RUNNER_URL":
            item["value"] = url
        elif item["name"] == "RAILSHOT_RUNNER_NAME":
            item["value"] = name
    return {"apiVersion": "v1", "kind": "List", "items": documents}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", type=Path, help="JSON map of component to GHCR digest")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--target-id")
    mode.add_argument("--build-runner-name", help="render a separate one-job build runner and scoped RBAC")
    parser.add_argument("--runner-url")
    parser.add_argument("--build-node", help="exact approved build worker hostname label")
    parser.add_argument("--dashboard-node-port", type=int, help="optional allocated ALB backend port; API stays private")
    args = parser.parse_args()
    try:
        images = json.loads(args.images.read_text())
        if args.build_runner_name:
            if args.dashboard_node_port:
                raise ValueError("build runner cannot expose a dashboard port")
            output = render_build_runner(images, args.build_runner_name, args.runner_url, args.build_node)
        else:
            if args.runner_url or args.build_node:
                raise ValueError("runner options require --build-runner-name")
            output = render(images, args.target_id, args.dashboard_node_port)
        print(json.dumps(output, indent=2))
    except (ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"BLOCKED: {error}\n")
