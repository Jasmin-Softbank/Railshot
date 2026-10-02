#!/usr/bin/env python3
"""Render private platform workloads from published immutable image references."""
import argparse
import json
from pathlib import Path
import re

import yaml


def render(images, target_id, dashboard_node_port=None):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", target_id):
        raise ValueError("operator-approved target_id is required")
    if dashboard_node_port is not None and not 30000 <= dashboard_node_port <= 32767:
        raise ValueError("dashboard NodePort must be in 30000..32767")
    for name in ("dashboard", "api"):
        pattern = rf"ghcr\.io/jasmin-softbank/railshot-{name}@sha256:[a-f0-9]{{64}}"
        if not re.fullmatch(pattern, images.get(name, "")):
            raise ValueError(f"{name}: published Railshot GHCR digest required")
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
        for item in container.get("env", []):
            if item["name"] == "RAILSHOT_TARGET_ID":
                item["value"] = target_id
    return {"apiVersion": "v1", "kind": "List", "items": documents}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", type=Path, help="JSON map of component to GHCR digest")
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--dashboard-node-port", type=int, help="optional allocated ALB backend port; API stays private")
    args = parser.parse_args()
    try:
        print(json.dumps(render(json.loads(args.images.read_text()), args.target_id, args.dashboard_node_port), indent=2))
    except (ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"BLOCKED: {error}\n")
