#!/usr/bin/env python3
"""Update only platform workload.json from this workflow's published digests.

Run in an ephemeral, clean Actions checkout at the reviewed source SHA. The
dedicated deployment branch preserves its existing tree and only fast-forwards.
This publishes desired state; the following verification job checks live state.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BRANCH = "deployment/platform"
WORKLOAD = "gitops/applications/railshot-platform/workload.json"
COMPONENTS = {"dashboard", "api", "mcp", "ci-runner"}


def git(*args, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode:
        # Git stderr may contain remote credential or endpoint details.
        raise ValueError("Git operation failed; remote state must be checked before rerunning")
    return result


def render(artifacts, target, port, provider_targets="{}", previous=None):
    images = {}
    for item in sorted(artifacts.iterdir()):
        if item.is_symlink() or not item.is_file() or item.suffix != ".json" or item.stem not in COMPONENTS:
            raise ValueError("unexpected publication artifact")
        value = json.loads(item.read_text())
        if not isinstance(value, dict) or set(value) != {item.stem}:
            raise ValueError("publication artifact must identify exactly its component")
        image = value[item.stem]
        if not isinstance(image, str) or not re.fullmatch(
                rf"ghcr\.io/jasmin-softbank/railshot-{item.stem}@sha256:[a-f0-9]{{64}}", image):
            raise ValueError("published component requires its immutable GHCR digest")
        images.update(value)
    changed = set(images) & {"dashboard", "api"}
    if not changed:
        raise ValueError("a platform publication artifact is required")
    preserved = {}
    for component in {"dashboard", "api"} - changed:
        matches = [item for item in (previous or {}).get("items", [])
                   if item.get("kind") == "Deployment" and item.get("metadata", {}).get("name") == "railshot-" + component
                   and item["metadata"].get("namespace") == "railshot-system"]
        if len(matches) != 1:
            raise ValueError("initial deployment requires dashboard and api artifacts")
        preserved[component] = matches[0]
        containers = matches[0]["spec"]["template"]["spec"]["containers"]
        images[component] = next(c["image"] for c in containers if c["name"] == component)

    with tempfile.TemporaryDirectory(prefix="platform-render-") as temporary:
        image_file = Path(temporary) / "images.json"
        image_file.write_text(json.dumps(images))
        rendered = subprocess.run([sys.executable, str(ROOT / "deployment/scripts/render-platform.py"),
                                   str(image_file), "--target-id", target,
                                   "--dashboard-node-port", str(port), "--provider-targets", provider_targets], text=True, capture_output=True)
        if rendered.returncode:
            raise ValueError("platform renderer rejected deployment configuration")
        declaration = json.loads(rendered.stdout)
        # Preserve the entire unchanged Deployment, not only its image: no Pod template churn.
        declaration["items"] = [preserved.get(item.get("metadata", {}).get("name", "").removeprefix("railshot-"), item)
                                if item.get("kind") == "Deployment" else item for item in declaration["items"]]
        return json.dumps(declaration, indent=2) + "\n"


def publish(artifacts, source_sha, target, port, provider_targets="{}"):
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha) or git("rev-parse", "HEAD").stdout.strip() != source_sha:
        raise ValueError("checkout must match the reviewed source SHA")
    if git("status", "--porcelain").stdout:
        raise ValueError("release requires a clean ephemeral checkout")
    reference = f"refs/heads/{BRANCH}"
    remote = git("ls-remote", "--exit-code", "origin", reference, check=False)
    if remote.returncode == 0:
        git("fetch", "--no-tags", "origin", reference)
    elif remote.returncode != 2:
        raise ValueError("cannot inspect the deployment branch")
    previous = json.loads(git("show", f"FETCH_HEAD:{WORKLOAD}").stdout) if remote.returncode == 0 else None
    # Render with reviewed source before switching; untouched Deployment objects stay byte-equivalent.
    declaration = render(artifacts, target, port, provider_targets, previous)
    digests = {container["name"] + "_digest": container["image"].rsplit(":", 1)[1]
               for item in json.loads(declaration)["items"] if item["kind"] == "Deployment"
               for container in item["spec"]["template"]["spec"]["containers"]}
    if remote.returncode == 0:
        git("switch", "--detach", "FETCH_HEAD")
    base = git("rev-parse", "HEAD").stdout.strip()
    path = ROOT / WORKLOAD
    if path.is_symlink() or path.resolve().parent != ROOT / "gitops/applications/railshot-platform":
        raise ValueError("workload path must not follow a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(declaration)
    git("add", "--", WORKLOAD)
    changed = git("diff", "--cached", "--name-only").stdout.splitlines()
    if not changed and remote.returncode == 0:
        return {"status": "unchanged", "revision": base, "branch": BRANCH, "source_sha": source_sha, **digests}
    if changed and changed != [WORKLOAD]:
        raise ValueError("release must change only the platform workload")
    if changed:
        git("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"Deploy platform images from {source_sha}")
    revision = git("rev-parse", "HEAD").stdout.strip()
    # A concurrent update fails normally. Never force, reset or replace history.
    git("push", "origin", f"HEAD:{reference}")
    observed = git("ls-remote", "--exit-code", "origin", reference).stdout.split()
    if not observed or observed[0] != revision:
        raise ValueError("deployment branch changed; verify its current revision")
    return {"status": "published", "revision": revision, "branch": BRANCH, "source_sha": source_sha, **digests}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--dashboard-node-port", type=int, required=True)
    parser.add_argument("--provider-targets", default="{}", help="optional registered provider-to-target JSON map")
    args = parser.parse_args()
    try:
        result = publish(args.artifacts, args.source_sha, args.target_id, args.dashboard_node_port, args.provider_targets)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                for key in ("revision", "dashboard_digest", "api_digest"):
                    output.write(f"{key}={result[key]}\n")
        print(json.dumps(result))
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, f"BLOCKED: {error}\n")
