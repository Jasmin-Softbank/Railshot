#!/usr/bin/env python3
"""Trusted local control Vault operations; secrets stay in files and HTTPS bodies."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import ssl
import sys
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from recovery_service import canonical, private_read, private_directory, exclusive_write, audit
from recovery_enroll import issue

ENV = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise ValueError("redirect rejected")


class Vault:
    def __init__(self, address, ca_file, token):
        parsed = urlsplit(address)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("invalid central address")
        self.address, self.token = address.rstrip("/"), token
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cadata=private_read(ca_file).decode())), NoRedirect())

    def call(self, method, path, value=None, absent=False):
        request = Request(self.address + "/v1/" + path, method=method, data=None if value is None else canonical(value), headers={"Content-Type": "application/json", "X-Vault-Token": self.token})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("Vault response limit")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            if absent and exc.code == 404:
                return None
            raise ValueError("central Vault operation failed: " + path + " status " + str(exc.code)) from None


def read_config(path):
    config = json.loads(private_read(path))
    if not isinstance(config, dict) or set(config) != {"version", "address", "ca_file", "operator_token_file", "recovery", "vault_tls"} or config["version"] != 1:
        raise ValueError("invalid provisioner config")
    if set(config["recovery"]) != {"endpoint", "ca_file", "ca_key_file", "registry_file"} or set(config["vault_tls"]) != {"ca_file", "ca_key_file"}:
        raise ValueError("incomplete certificate authority configuration")
    endpoint = urlsplit(config["recovery"]["endpoint"])
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path != "/api/v1/escrows":
        raise ValueError("invalid custody endpoint")
    for key in ("ca_file", "operator_token_file"):
        private_read(config[key])
    return config


def client(config, token=None):
    return Vault(config["address"], config["ca_file"], token or private_read(config["operator_token_file"]).decode().strip())


def seal_policy(key):
    return (f'path "transit/encrypt/{key}" {{ capabilities=["update"] }}\n'
            f'path "transit/decrypt/{key}" {{ capabilities=["update"] }}\n'
            'path "auth/token/renew-self" { capabilities=["update"] }\n'
            'path "auth/token/lookup-self" { capabilities=["read"] }\n')


def issue_token(vault, environment):
    key = "railshot-" + environment
    if vault.call("GET", "transit/keys/" + key, absent=True) is None:
        vault.call("POST", "transit/keys/" + key, {"type": "aes256-gcm96", "exportable": False, "allow_plaintext_backup": False})
    vault.call("PUT", "sys/policies/acl/" + key, {"policy": seal_policy(key)})
    issued = vault.call("POST", "auth/token/create/railshot-seal", {"policies": [key], "no_default_policy": True, "display_name": key})
    token, accessor = issued["auth"]["client_token"], issued["auth"]["accessor"]
    if not isinstance(token, str) or not token or not isinstance(accessor, str) or not accessor:
        raise ValueError("missing issued token")
    return token, accessor


def child_certificate(config, output):
    ca_bytes = private_read(config["ca_file"])
    ca = x509.load_pem_x509_certificate(ca_bytes)
    ca_key = serialization.load_pem_private_key(private_read(config["ca_key_file"]), None)
    if ca_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != ca.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("invalid child CA")
    now = datetime.now(timezone.utc)
    if ca.not_valid_after_utc < now + timedelta(days=90) or ca.not_valid_before_utc > now:
        raise ValueError("child CA lifetime")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vault.railshot-secrets.svc")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=90))
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in ("vault", "vault-0.vault", "vault.railshot-secrets.svc", "vault.railshot-secrets.svc.cluster.local", "localhost")]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).sign(ca_key, hashes.SHA256()))
    result = {"cert_file": str(output / "vault.crt"), "key_file": str(output / "vault.key"), "ca_file": str(output / "vault-ca.crt")}
    exclusive_write(result["cert_file"], cert.public_bytes(serialization.Encoding.PEM))
    exclusive_write(result["key_file"], key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    exclusive_write(result["ca_file"], ca_bytes)
    return result


def provision(config_path, environment, output):
    if not ENV.fullmatch(environment):
        raise ValueError("invalid environment")
    config = read_config(config_path)
    output = Path(output)
    private_directory(output.parent)
    if output.exists():
        private_directory(output)
        profile = json.loads(private_read(output / "transit-profile.json"))
        if profile["environment_id"] != environment or profile["seal"]["key_name"] != "railshot-" + environment:
            raise ValueError("existing environment mismatch")
        for path in [profile["seal_env_file"], *profile["vault_tls"].values(), profile["recovery"]["client_key_file"], profile["recovery"]["client_cert_file"], profile["recovery"]["ca_file"]]:
            private_read(path)
        delivery = json.loads(private_read(profile["delivery_recovery_config_file"]))
        for field in ("ca_file", "client_cert_file", "client_key_file"):
            private_read(delivery[field])
        return {"environment_id": environment, "status": "reused", "profile_path": str(output / "transit-profile.json"), "credentials_generation": profile["credentials_generation"]}
    output.mkdir(mode=0o700)
    token, accessor = issue_token(client(config), environment)
    recovery = config["recovery"]
    issued = issue(environment, "both", recovery["ca_file"], recovery["ca_key_file"], recovery["registry_file"], output)
    vault_tls = child_certificate(config["vault_tls"], output)
    exclusive_write(output / "transit-ca.crt", private_read(config["ca_file"]))
    exclusive_write(output / "seal-env.json", canonical({"VAULT_TOKEN": token}) + b"\n")
    exclusive_write(output / "seal-token-accessor.json", canonical({"environment_id": environment, "accessor": accessor}) + b"\n")
    runtime, manager = issued["clients"]["runtime"], issued["clients"]["manager"]
    def recovery_config(identity):
        return {"endpoint": recovery["endpoint"], "ca_file": issued["ca_file"], "client_cert_file": identity["cert_file"], "client_key_file": identity["key_file"]}
    manager_path = output / "delivery-recovery.json"
    exclusive_write(manager_path, canonical({"version": 1, **recovery_config(manager)}) + b"\n")
    profile = {"version": 1, "environment_id": environment, "credentials_generation": str(uuid.uuid4()),
               "seal": {"type": "transit", "address": config["address"], "key_name": "railshot-" + environment, "mount_path": "transit/", "ca_file": str(output / "transit-ca.crt")},
               "seal_env_file": str(output / "seal-env.json"), "vault_tls": vault_tls, "recovery": recovery_config(runtime),
               "delivery_recovery_config_file": str(manager_path)}
    # This profile is the last completion marker. Partial state must be inspected,
    # never silently replaced/reissued after an uncertain external mutation.
    exclusive_write(output / "transit-profile.json", canonical(profile) + b"\n")
    return {"environment_id": environment, "status": "issued", "profile_path": str(output / "transit-profile.json"), "credentials_generation": profile["credentials_generation"]}


def configure(config_path, operator_output):
    config = read_config(config_path)
    vault = client(config)
    mounts = vault.call("GET", "sys/mounts")
    if "transit/" not in mounts.get("data", mounts):
        vault.call("POST", "sys/mounts/transit", {"type": "transit"})
    audits = vault.call("GET", "sys/audit")
    if "file/" not in audits.get("data", audits):
        vault.call("PUT", "sys/audit/file", {"type": "file", "options": {"file_path": "/vault/data/audit.log", "log_raw": "false"}})
    vault.call("POST", "auth/token/roles/railshot-seal", {"allowed_policies_glob": ["railshot-*"], "orphan": True, "renewable": True, "token_period": "24h", "token_no_default_policy": True})
    # An operator provisions keys/policies and invokes one constrained token role.
    policy = ('path "transit/keys/railshot-*" { capabilities=["create","update","read"] }\n'
              'path "sys/policies/acl/railshot-*" { capabilities=["create","update","read"] }\n'
              'path "auth/token/create/railshot-seal" { capabilities=["create","update"] }\n'
              'path "auth/token/lookup-accessor" { capabilities=["update"] }\n'
              'path "auth/token/revoke-accessor" { capabilities=["update"] }\n'
              'path "auth/token/renew-self" { capabilities=["update"] }\n'
              'path "auth/token/lookup-self" { capabilities=["read"] }\n')
    vault.call("PUT", "sys/policies/acl/railshot-provisioner", {"policy": policy})
    if not operator_output:
        raise ValueError("operator output is required")
    operator = vault.call("POST", "auth/token/create", {"policies": ["railshot-provisioner"], "no_default_policy": True, "no_parent": True, "period": "24h", "display_name": "railshot-provisioner"})["auth"]["client_token"]
    exclusive_write(operator_output, operator.encode() + b"\n")
    client(config, operator).call("GET", "auth/token/lookup-self")
    return {"configured": True, "operator_policy": "railshot-provisioner", "operator_token_file": str(operator_output)}


def rotate(config_path, environment, output):
    if not ENV.fullmatch(environment):
        raise ValueError("invalid environment")
    config = read_config(config_path)
    output = Path(output)
    private_directory(output.parent)
    output.mkdir(mode=0o700)
    token, accessor = issue_token(client(config), environment)
    exclusive_write(output / "seal-env.json", canonical({"VAULT_TOKEN": token}) + b"\n")
    exclusive_write(output / "seal-token-accessor.json", canonical({"environment_id": environment, "accessor": accessor}) + b"\n")
    return {"environment_id": environment, "status": "issued", "seal_env_file": str(output / "seal-env.json"), "accessor_file": str(output / "seal-token-accessor.json")}


def revoke(config_path, environment, accessor_file):
    if not ENV.fullmatch(environment):
        raise ValueError("invalid environment")
    value = json.loads(private_read(accessor_file))
    if set(value) != {"environment_id", "accessor"} or value["environment_id"] != environment:
        raise ValueError("accessor environment mismatch")
    vault = client(read_config(config_path))
    found = vault.call("POST", "auth/token/lookup-accessor", {"accessor": value["accessor"]})
    if found["data"]["policies"] != ["railshot-" + environment]:
        raise ValueError("accessor policy mismatch")
    vault.call("POST", "auth/token/revoke-accessor", {"accessor": value["accessor"]})
    return {"revoked": True, "environment_id": environment}


def monitor(config_path):
    config = read_config(config_path)
    vault = client(config)
    health = vault.call("GET", "sys/health")
    if health.get("sealed") is not False or health.get("initialized") is not True:
        raise ValueError("central Vault not ready")
    renewed = vault.call("POST", "auth/token/renew-self", {})
    if renewed.get("auth", {}).get("renewable") is not True:
        raise ValueError("operator token not renewable")
    now = datetime.now(timezone.utc)
    for path in (config["ca_file"], config["recovery"]["ca_file"], config["vault_tls"]["ca_file"]):
        certificate = x509.load_pem_x509_certificate(private_read(path))
        if certificate.not_valid_after_utc < now + timedelta(days=7):
            raise ValueError("central CA expires soon")
    return {"central_ready": True, "operator_token_renewed": True, "central_ca_valid_for_seven_days": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "provision", "rotate", "revoke", "monitor"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--environment-id")
    parser.add_argument("--output-dir")
    parser.add_argument("--accessor-file")
    parser.add_argument("--operator-output")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise ValueError("root required")
        os.umask(0o077)
        if args.command == "monitor":
            result = monitor(args.config)
        elif args.command == "configure":
            result = configure(args.config, args.operator_output)
        elif args.command == "provision":
            result = provision(args.config, args.environment_id, args.output_dir)
        elif args.command == "rotate":
            result = rotate(args.config, args.environment_id, args.output_dir)
        else:
            result = revoke(args.config, args.environment_id, args.accessor_file)
        audit("central-" + args.command, "succeeded", environment_id=args.environment_id)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception:
        audit("central-" + args.command, "failed")
        print('{"error":"CONTROL_OPERATION_FAILED"}', file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
