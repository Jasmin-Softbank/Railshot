#!/usr/bin/env python3
"""Explicit local-only Docker drill. Synthetic secrets never leave its temp directory.

Run with the recovery dependency installed and an accessible Docker daemon:
  python deployment/scripts/tests/live_control_vault.py
Creates uniquely named containers/network and removes only those in finally.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
import re
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VAULT_IMAGE = "hashicorp/vault:2.1.1@sha256:47f14a6acb98f48d798a07df7c83f23a6e636e1cf724c5f8ff165cb32667a1e2"
CUSTODY_IMAGE = "railshot-custody-local-test:20261004"
SCRIPTS = Path(__file__).resolve().parents[1]


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)
    if result.returncode:
        # Docker argv uses only local paths/container IDs; outputs may contain
        # secrets from child tools, so errors deliberately do not forward them.
        raise RuntimeError("local Docker command failed: " + args[0] + "; stderr=" + result.stderr[-2000:])
    return result.stdout.strip()


def write(path, value, mode=0o600):
    data = value if isinstance(value, bytes) else value.encode()
    path.write_bytes(data)
    path.chmod(mode)


def main():
    marker = "railshot-drill-" + uuid.uuid4().hex[:10]
    containers = []
    root = Path(tempfile.mkdtemp(prefix=marker + "-"))
    network_created = False
    results = {}
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-control-drill")])
        now = datetime.now(timezone.utc)
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
              .add_extension(x509.KeyUsage(True,False,False,False,False,True,True,False,False), True).sign(key, hashes.SHA256()))
        write(root / "ca.crt", ca.public_bytes(serialization.Encoding.PEM))
        write(root / "ca.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        server = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "central")]))
                  .issuer_name(name).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=90))
                  .add_extension(x509.BasicConstraints(ca=False,path_length=None), True)
                  .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), False)
                  .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
                  .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in ("localhost", "central", "restore", "custody")]), False)
                  .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False).sign(key, hashes.SHA256()))
        write(root / "server.crt", server.public_bytes(serialization.Encoding.PEM))
        write(root / "server.key", server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        docker("network", "create", marker)
        network_created = True

        def run_container(name, image, args, port, extra=()):
            identifier = marker + "-" + name
            if image == VAULT_IMAGE:
                extra = (*extra, "-v", str(root / (name + "-data")) + ":/vault/data")
            docker("run", "-d", "--name", identifier, "--network", marker, "--network-alias", name,
                   "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                   "-p", "127.0.0.1::" + str(port), "-v", str(root) + ":/fixture",
                   "-v", str(SCRIPTS) + ":/code:ro", *extra, "--entrypoint", args[0], image, *args[1:])
            containers.append(identifier)
            mapping = docker("port", identifier, str(port) + "/tcp")
            return identifier, int(mapping.rsplit(":", 1)[1])

        def api(port, path, method="GET", body=None, token=None, identity=None, raw=False):
            context = ssl.create_default_context(cafile=root / "ca.crt")
            if identity:
                context.load_cert_chain(root / identity / "runtime.crt", root / identity / "runtime.key")
            conn = http.client.HTTPSConnection("localhost", port, context=context, timeout=30)
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Vault-Token"] = token
            payload = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
            try:
                conn.request(method, path, payload, headers)
                response = conn.getresponse()
                data = response.read()
                return response.status, data if raw else json.loads(data) if data else {}
            finally:
                conn.close()

        def wait(port):
            for _ in range(90):
                try:
                    status, result = api(port, "/v1/sys/seal-status")
                    if status == 200:
                        return result
                except Exception:
                    pass
                time.sleep(0.5)
            raise RuntimeError("local Vault listener did not become ready")

        def config(name):
            (root / (name + "-data")).mkdir(mode=0o700)
            write(root / (name + ".hcl"), f'''ui=false
disable_mlock=true
api_addr="https://{name}:8200"
cluster_addr="https://{name}:8201"
listener "tcp" {{ address="0.0.0.0:8200" tls_cert_file="/fixture/server.crt" tls_key_file="/fixture/server.key" }}
storage "raft" {{ path="/fixture/{name}-data" node_id="{name}" }}
''')

        config("central")
        central, port = run_container("central", VAULT_IMAGE, ["vault", "server", "-config=/fixture/central.hcl"], 8200)
        wait(port)
        status, initialization = api(port, "/v1/sys/init", "PUT", {"secret_shares": 5, "secret_threshold": 3})
        assert status == 200
        for share in initialization["keys"][:3]:
            status, unsealed = api(port, "/v1/sys/unseal", "PUT", {"key": share})
        assert not unsealed["sealed"]
        root_token = initialization["root_token"]
        write(root / "root-token", root_token)
        cfg = {"version": 1, "address": "https://central:8200", "ca_file": "/fixture/ca.crt", "operator_token_file": "/fixture/root-token",
               "recovery": {"endpoint": "https://custody:9443/api/v1/escrows", "ca_file": "/fixture/ca.crt", "ca_key_file": "/fixture/ca.key", "registry_file": "/fixture/registry.json"},
               "vault_tls": {"ca_file": "/fixture/ca.crt", "ca_key_file": "/fixture/ca.key"}}
        write(root / "config.json", json.dumps(cfg))

        def local_tool(*args):
            if args[0] == "/code/control_backup.py":
                options = dict(zip(args[2::2], args[3::2]))
                method = {"backup": "backup", "verify": "verify_bundle", "restore": "restore"}[args[1]]
                required = {"backup": ("--snapshot", "--custody", "--keyring", "--reference", "--output"), "verify": ("--bundle", "--keyring"), "restore": ("--bundle", "--keyring", "--output")}[args[1]]
                code = "import sys,json;sys.path.insert(0,'/code');import control_backup;print(json.dumps(control_backup." + method + "(" + ",".join(repr(options[k]) for k in required) + ")))"
                args = ("-c", code)
            elif args[0] == "/code/control_vault.py":
                options = dict(zip(args[2::2], args[3::2]))
                required = {"configure": ("--config", "--operator-output"), "provision": ("--config", "--environment-id", "--output-dir"), "rotate": ("--config", "--environment-id", "--output-dir"), "revoke": ("--config", "--environment-id", "--accessor-file")}[args[1]]
                code = "import sys,json;sys.path.insert(0,'/code');import control_vault;print(json.dumps(control_vault." + args[1] + "(" + ",".join(repr(options[k]) for k in required) + ")))"
                args = ("-c", code)
            return json.loads(docker("run", "--rm", "--network", marker, "-v", str(root) + ":/fixture", "-v", str(SCRIPTS) + ":/code:ro", "--entrypoint", "python3", CUSTODY_IMAGE, *args))

        local_tool("/code/control_vault.py", "configure", "--config", "/fixture/config.json", "--operator-output", "/fixture/operator-token")
        cfg["operator_token_file"] = "/fixture/operator-token"
        write(root / "config.json", json.dumps(cfg))
        for environment in ("env-a", "env-b"):
            result = local_tool("/code/control_vault.py", "provision", "--config", "/fixture/config.json", "--environment-id", environment, "--output-dir", "/fixture/" + environment)
            assert result["status"] == "issued"
        results["independent_environment_keys_and_certificates"] = (root / "env-a/runtime.key").read_bytes() != (root / "env-b/runtime.key").read_bytes()
        token_a = json.loads((root / "env-a/seal-env.json").read_text())["VAULT_TOKEN"]
        token_b = json.loads((root / "env-b/seal-env.json").read_text())["VAULT_TOKEN"]
        assert token_a != token_b
        status, _ = api(port, "/v1/transit/encrypt/railshot-env-b", "POST", {"plaintext": base64.b64encode(b"synthetic").decode()}, token_a)
        assert status == 403
        status, encrypted = api(port, "/v1/transit/encrypt/railshot-env-a", "POST", {"plaintext": base64.b64encode(b"synthetic").decode()}, token_a)
        assert status == 200
        results["transit_cross_environment_denied"] = True
        (root / "child-data").mkdir(mode=0o700)
        write(root / "child.env", "VAULT_TOKEN=" + token_a + "\nVAULT_ADDR=https://localhost:8200\nVAULT_CACERT=/fixture/env-a/vault-ca.crt\n")
        write(root / "child.hcl", '''ui=false
disable_mlock=true
api_addr="https://localhost:8200"
cluster_addr="https://localhost:8201"
listener "tcp" { address="0.0.0.0:8200" tls_cert_file="/fixture/env-a/vault.crt" tls_key_file="/fixture/env-a/vault.key" }
storage "raft" { path="/fixture/child-data" node_id="child" }
seal "transit" { address="https://central:8200" token="env://VAULT_TOKEN" key_name="railshot-env-a" mount_path="transit/" tls_ca_cert="/fixture/env-a/transit-ca.crt" }
''')
        child, child_port = run_container("child", VAULT_IMAGE, ["vault", "server", "-config=/fixture/child.hcl"], 8200, ("--env-file", str(root / "child.env")))
        wait(child_port)
        # Exercise actual CLI serialization; HTTP init has a different shape.
        # Capture only in this private process and never print the material.
        child_init = json.loads(docker("exec", child, "vault", "operator", "init",
                                       "-recovery-shares=5", "-recovery-threshold=3", "-format=json"))
        assert not wait(child_port)["sealed"]
        child_token = child_init["root_token"]
        child_material = child_init
        assert child_material["unseal_shares"] == child_material["unseal_threshold"] == 1
        assert child_material["unseal_keys_b64"] == child_material["unseal_keys_hex"] == []
        status, _ = api(child_port, "/v1/sys/mounts/check", "POST", {"type": "kv", "options": {"version": "2"}}, child_token)
        assert status in (200, 204)
        assert api(child_port, "/v1/check/data/probe", "POST", {"data": {"value": "synthetic-retained"}}, child_token)[0] in (200, 204)
        docker("restart", child)
        child_port = int(docker("port", child, "8200/tcp").rsplit(":", 1)[1])
        assert not wait(child_port)["sealed"]
        status, retained = api(child_port, "/v1/check/data/probe", token=child_token)
        assert retained["data"]["data"]["value"] == "synthetic-retained"
        results["child_auto_unseal_restart_and_data_retention"] = True
        (root / "custody-data").mkdir(mode=0o700)
        write(root / "keyring.json", json.dumps({"version": 1, "active_key_id": "key-1", "keys": {"key-1": base64.b64encode(os.urandom(32)).decode()}}))
        custody, custody_port = run_container("custody", CUSTODY_IMAGE, ["python3", "/opt/railshot/recovery_service.py", "--data-dir", "/fixture/custody-data", "--keyring", "/fixture/keyring.json", "serve", "--listen", "0.0.0.0", "--port", "9443", "--registry", "/fixture/registry.json", "--cert", "/fixture/server.crt", "--key", "/fixture/server.key", "--ca", "/fixture/ca.crt"], 9443)
        for _ in range(30):
            try:
                status, _ = api(custody_port, "/api/v1/healths", identity="env-a")
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        value = {"version": 1, "environment_id": "env-a", "kind": "vault-initialization", "operation_id": "live-bootstrap", "material": child_material}
        status, receipt = api(custody_port, "/api/v1/escrows", "POST", value, identity="env-a")
        assert status == 201
        assert api(custody_port, "/api/v1/escrows", "POST", value, identity="env-b")[0] == 403
        docker("restart", custody)
        custody_port = int(docker("port", custody, "9443/tcp").rsplit(":", 1)[1])
        time.sleep(1)
        assert api(custody_port, "/api/v1/escrows?kind=vault-initialization&operation_id=live-bootstrap", identity="env-a")[1] == receipt
        local_tool("/code/recovery_service.py", "--data-dir", "/fixture/custody-data", "--keyring", "/fixture/keyring.json", "backup", "--output", "/fixture/custody-backup.sqlite3")
        (root / "custody-restored").mkdir(mode=0o700)
        shutil.copyfile(root / "custody-backup.sqlite3", root / "custody-restored/custody.sqlite3")
        (root / "custody-restored/custody.sqlite3").chmod(0o600)
        assert local_tool("/code/recovery_service.py", "--data-dir", "/fixture/custody-restored", "--keyring", "/fixture/keyring.json", "verify")["verified_records"] == 1
        results["custody_mtls_restart_and_restore"] = True
        # Snapshot produced by real Raft storage; restore into a separate empty Vault.
        status, snapshot = api(port, "/v1/sys/storage/raft/snapshot", token=root_token, raw=True)
        assert status == 200
        write(root / "central.snapshot", snapshot)
        write(root / "reference.json", json.dumps({"version": 1, "vault_version": "2.1.1", "transit_key_ids": ["railshot-env-a", "railshot-env-b"]}))
        local_tool("/code/control_backup.py", "backup", "--snapshot", "/fixture/central.snapshot", "--custody", "/fixture/custody-backup.sqlite3", "--keyring", "/fixture/keyring.json", "--reference", "/fixture/reference.json", "--output", "/fixture/bundle")
        local_tool("/code/control_backup.py", "verify", "--bundle", "/fixture/bundle", "--keyring", "/fixture/keyring.json")
        local_tool("/code/control_backup.py", "restore", "--bundle", "/fixture/bundle", "--keyring", "/fixture/keyring.json", "--output", "/fixture/restored-bundle")
        snapshot = (root / "restored-bundle/vault.snapshot").read_bytes()
        results["backup_bundle_create_verify_restore"] = True
        config("restore")
        restored, restored_port = run_container("restore", VAULT_IMAGE, ["vault", "server", "-config=/fixture/restore.hcl"], 8200)
        wait(restored_port)
        _, fresh = api(restored_port, "/v1/sys/init", "PUT", {"secret_shares": 1, "secret_threshold": 1})
        api(restored_port, "/v1/sys/unseal", "PUT", {"key": fresh["keys"][0]})
        assert api(restored_port, "/v1/sys/storage/raft/snapshot-force", "POST", snapshot, fresh["root_token"], raw=True)[0] in (200, 204)
        time.sleep(2)
        for share in initialization["keys"][:3]:
            api(restored_port, "/v1/sys/unseal", "PUT", {"key": share})
        assert not wait(restored_port)["sealed"]
        status, clear = api(restored_port, "/v1/transit/decrypt/railshot-env-a", "POST", {"ciphertext": encrypted["data"]["ciphertext"]}, token_a)
        assert status == 200 and base64.b64decode(clear["data"]["plaintext"]) == b"synthetic"
        results["central_snapshot_restore_decrypts_previous_ciphertext"] = True
        # Explicit token rotation does not revoke old credential until requested.
        local_tool("/code/control_vault.py", "rotate", "--config", "/fixture/config.json", "--environment-id", "env-a", "--output-dir", "/fixture/env-a-rotation")
        newer = json.loads((root / "env-a-rotation/seal-env.json").read_text())["VAULT_TOKEN"]
        assert newer != token_a
        assert api(port, "/v1/auth/token/lookup-self", token=newer)[0] == 200
        assert api(port, "/v1/auth/token/lookup-self", token=token_a)[0] == 200
        local_tool("/code/control_vault.py", "revoke", "--config", "/fixture/config.json", "--environment-id", "env-a", "--accessor-file", "/fixture/env-a/seal-token-accessor.json")
        assert api(port, "/v1/auth/token/lookup-self", token=token_a)[0] == 403
        results["explicit_rotation_preserves_then_revokes_previous_token"] = True
        assert api(port, "/v1/auth/token/revoke-self", "POST", {}, root_token)[0] in (200, 204)
        operator_token = (root / "operator-token").read_text().strip()
        assert api(port, "/v1/auth/token/lookup-self", token=operator_token)[0] == 200
        assert api(port, "/v1/auth/token/lookup-self", token=newer)[0] == 200
        assert api(port, "/v1/auth/token/renew-self", "POST", {}, operator_token)[0] == 200
        results["operator_and_child_survive_parent_root_revocation"] = True
        results["operator_periodic_token_renewal"] = True
        print(json.dumps({"vault_image": VAULT_IMAGE, "results": results}, indent=2))
    except Exception:
        for container in containers:
            logs = subprocess.run(["docker", "logs", "--tail", "25", container], capture_output=True, text=True).stderr
            errors = [line for line in logs.splitlines() if "error" in line.lower() or "failed" in line.lower()]
            safe = re.sub(r"(?:hvs|hvb|s)\.[A-Za-z0-9_.-]+", "<redacted>", "\n".join(errors))
            if safe:
                print(container.rsplit("-", 1)[-1] + " diagnostics: " + safe[-2000:], flush=True)
        raise
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        if network_created:
            subprocess.run(["docker", "network", "rm", marker], capture_output=True, timeout=30)
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
