#!/usr/bin/env python3
"""Exercise real container entrypoints; never dispatch CI or touch a cluster."""
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True, timeout=90, **kwargs).stdout.strip()


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        path = values.get("src") if tag == "script" else values.get("href") if tag == "link" and values.get("rel") == "stylesheet" else None
        if path and path.startswith("/"):
            self.paths.append(path)


def smoke(component, image):
    if component == "ci-runner":
        # Registration and host firewall integration need the dedicated CI VM.
        output = docker("run", "--rm", "--network", "none", image, "--check-image")
        print(output)
        return
    if component == "mcp":
        # stdin stays open until the initialize response arrives; stderr is not MCP data.
        name = "railshot-mcp-smoke-" + secrets.token_hex(6)
        process = subprocess.Popen(["docker", "run", "--rm", "-i", "--name", name, "--network", "none", image],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        import selectors
        try:
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "container-smoke", "version": "1"}}}
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(30), "MCP initialize timed out"
                response = json.loads(process.stdout.readline())
            assert response.get("id") == 1 and response.get("result", {}).get("serverInfo"), response
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
            process.communicate(timeout=15)
        return
    port = "8080" if component == "dashboard" else "4173"
    token = secrets.token_hex(32)
    secret_dir = tempfile.TemporaryDirectory(prefix="railshot-container-smoke-")
    options = ["--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-p", f"127.0.0.1::{port}"]
    if component == "api":
        token_file = Path(secret_dir.name) / "token"
        token_file.write_text(token)
        # Random test token only; private parent directory, readable nonroot mount.
        token_file.chmod(0o444)
        options += ["-e", "RAILSHOT_BIND_HOST=0.0.0.0", "-e", "RAILSHOT_ALLOWED_HOSTS=localhost,127.0.0.1", "-e", "RAILSHOT_API_TOKEN_FILE=/run/secrets/api-token", "-v", f"{token_file}:/run/secrets/api-token:ro"]
    container = docker("run", "-d", *options, image)
    try:
        endpoint = "http://" + docker("port", container, port).splitlines()[0]
        for attempt in range(50):
            try:
                with urlopen(endpoint + "/healthz", timeout=2) as response:
                    assert response.status == 200
                break
            except (URLError, ConnectionError):
                if attempt == 49:
                    raise
                time.sleep(0.2)
        if component == "dashboard":
            with urlopen(endpoint) as response:
                assets = Assets()
                assets.feed(response.read().decode())
            assert assets.paths, "Vite output assets missing"
            for path in assets.paths:
                with urlopen(endpoint + path) as response:
                    assert response.status == 200 and response.read(), path
        else:
            for headers, expected in [({}, 401), ({"Authorization": "Bearer " + token}, 503), ({"Host": "untrusted.invalid"}, 403)]:
                try:
                    urlopen(Request(endpoint + "/api/runs/1", headers=headers), timeout=3)
                    raise AssertionError("unexpectedly accepted request")
                except HTTPError as error:
                    assert error.code == expected, (error.code, expected)
    finally:
        docker("rm", "-f", container)
        secret_dir.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("dashboard", "api", "mcp", "ci-runner"))
    parser.add_argument("image")
    args = parser.parse_args()
    smoke(args.component, args.image)
    print(f"{args.component}: container smoke passed")
