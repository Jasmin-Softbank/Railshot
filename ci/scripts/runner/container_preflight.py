#!/usr/bin/env python3
"""Fail closed before registering the trusted runner on its dedicated CI host."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = "/var/lib/railshot-runner"
TOKEN = "/run/secrets/github-runner-registration-token"
READ_ONLY = {
    "/etc/railshot": "/etc/railshot",
    "/usr/local/sbin/railshot-ci-network": "/usr/local/sbin/railshot-ci-network",
    "/var/lib/railshot-ci": "/var/lib/railshot-ci",
    "/host/etc/rancher": "/etc/rancher",
    "/host/var/lib/rancher": "/var/lib/rancher",
}
READ_WRITE = {path: path for path in (
    "/var/run/docker.sock", "/run/railshot-ci-network.lock",
    ROOT + "/work", ROOT + "/runs", ROOT + "/codex",
)}


def validate_container(container):
    host = container["HostConfig"]
    if host.get("NetworkMode") != "host" or host.get("Privileged") or host.get("PidMode") == "host":
        raise ValueError("runner requires host network without privileged/host PID mode")
    if set(host.get("CapAdd") or []) != {"NET_ADMIN"}:
        raise ValueError("runner requires only NET_ADMIN in addition to Docker default capabilities")
    mounts = {mount["Destination"]: mount for mount in container["Mounts"]}
    if set(mounts) != set(READ_ONLY) | set(READ_WRITE) | {TOKEN}:
        raise ValueError("unexpected/missing mount; do not mount kubeconfig or cloud credentials")
    for paths, writable in ((READ_ONLY, False), (READ_WRITE, True)):
        for target, source in paths.items():
            mount = mounts[target]
            if mount["Type"] != "bind" or mount["Source"] != source or mount["RW"] != writable:
                raise ValueError("incorrect mount: " + target)
    if mounts[TOKEN]["RW"] or mounts[TOKEN]["Type"] != "bind":
        raise ValueError("registration token must be a read-only file mount")


def owned_file(path, mode):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("root-owned file with mode " + oct(mode) + " required: " + path)


def check_image():
    for command in ("python3", "git", "curl", "jq", "docker", "iptables", "ip6tables", "flock", "sysctl"):
        if not shutil.which(command):
            raise ValueError("missing executable: " + command)
    subprocess.run(["docker", "buildx", "version"], check=True)
    subprocess.run(["/home/runner/bin/Runner.Listener", "--version"], check=True)


def check_host():
    if os.geteuid() != 0:
        raise ValueError("trusted runner must use root on the dedicated CI VM")
    for key in ("KUBECONFIG", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
                "AZURE_CLIENT_SECRET", "ARM_CLIENT_SECRET", "OS_PASSWORD"):
        if os.environ.get(key):
            raise ValueError("operational credentials must not enter the CI runner: " + key)
    if os.environ.get("DOCKER_HOST") != "unix:///var/run/docker.sock":
        raise ValueError("only this CI VM's Docker socket is supported")
    if os.environ.get("TMPDIR") != ROOT + "/work/_temp":
        raise ValueError("TMPDIR must be inside the workspace's same-path host mount")
    owned_file("/etc/railshot/ci-runner-host", 0o644)
    if Path("/etc/railshot/ci-runner-host").read_text().strip() != "dedicated-ci-ubuntu-24.04":
        raise ValueError("dedicated CI host marker mismatch")
    for path in ("/host/etc/rancher/k3s", "/host/var/lib/rancher/k3s"):
        if Path(path).exists() or Path(path).is_symlink():
            raise ValueError("refusing a K3s host")
    owned_file("/usr/local/sbin/railshot-ci-network", 0o755)
    owned_file("/etc/railshot/ci-executor.yaml", 0o644)
    owned_file("/var/lib/railshot-ci/network-verified.sha256", 0o600)
    owned_file(TOKEN, 0o600)
    for suffix in ("work", "runs", "codex"):
        path = Path(ROOT) / suffix
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("private root-owned directory required: " + str(path))
    container = json.loads(subprocess.check_output(["docker", "inspect", "railshot-ci-runner"], text=True))[0]
    validate_container(container)
    # Reuse the same gate implementation as jobs; no second firewall/builder contract.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
    from gate import require_ci_network
    require_ci_network("railshot-quality")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check-image"]:
            check_image()
        elif sys.argv[1:] == ["--check-builder"]:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
            from gate import require_ci_builder
            require_ci_builder()
        elif not sys.argv[1:]:
            check_host()
        else:
            raise ValueError("unsupported preflight argument")
    except Exception as exc:
        print("CI runner preflight failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
