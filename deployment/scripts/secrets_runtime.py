#!/usr/bin/env python3
"""Reconcile and prove a single-node Vault/External Secrets runtime.

This program runs on the registered K3s node through the existing Ansible/SSH
transport.  Its private operator profile is the only source of storage, TLS,
seal, image and recovery-escrow settings.  It never prints bootstrap material.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
IMAGE = re.compile(r"[^\s@]+@sha256:[a-f0-9]{64}")
SIZE = re.compile(r"[1-9][0-9]*(?:Gi|Ti)")
ESCROW_HELPER = Path("/usr/local/libexec/railshot-recovery-escrow")


class RuntimeErrorCode(RuntimeError):
    def __init__(self, code, message, *, unknown=False):
        super().__init__(message); self.code, self.unknown = code, unknown


def private_json(path):
    path = Path(path)
    try:
        info = path.lstat(); raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeErrorCode("SECRETS_CONFIGURATION_REQUIRED", "private secrets profile is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or not raw or len(raw) > 1024 * 1024:
        raise RuntimeErrorCode("SECRETS_CONFIGURATION_UNSAFE", "secrets profile must be an owned private regular file")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeErrorCode("SECRETS_CONFIGURATION_INVALID", "secrets profile is not valid JSON") from exc


def validate(config, environment_id, provider):
    if not isinstance(config, dict) or config.get("version") != 1 or config.get("environment_id") != environment_id:
        raise RuntimeErrorCode("SECRETS_PROFILE_MISMATCH", "secrets profile does not match the environment")
    profile = config.get("provider_profile")
    required = {"provider", "namespace", "storage", "vault", "external_secrets", "recovery"}
    if not isinstance(profile, dict) or set(profile) != required or profile.get("provider") != provider:
        raise RuntimeErrorCode("SECRETS_PROFILE_MISMATCH", "provider profile does not match the registered target")
    if profile["namespace"] != "railshot-secrets":
        raise RuntimeErrorCode("SECRETS_PROFILE_INVALID", "the fixed secrets namespace is required")
    storage = profile["storage"]
    if (not isinstance(storage, dict) or set(storage) != {"class_name", "capacity", "manifest_sha256"}
            or LABEL.fullmatch(storage.get("class_name", "")) is None or SIZE.fullmatch(storage.get("capacity", "")) is None):
        raise RuntimeErrorCode("PERSISTENT_STORAGE_PROFILE_REQUIRED", "a registered persistent storage class and capacity are required")
    vault = profile["vault"]
    if (not isinstance(vault, dict) or set(vault) != {"image", "tls_secret", "seal_secret", "seal", "tls", "seal_env_file"}
            or IMAGE.fullmatch(vault.get("image", "")) is None
            or any(LABEL.fullmatch(vault.get(key, "")) is None for key in ("tls_secret", "seal_secret"))):
        raise RuntimeErrorCode("VAULT_PROFILE_INVALID", "pinned Vault image and registered TLS/seal Secrets are required")
    seal = vault["seal"]
    allowed = {"aws": "awskms", "gcp": "gcpckms", "openstack": "transit", "onprem": "transit"}
    if not isinstance(seal, dict) or seal.get("type") not in (allowed.get(provider), "transit"):
        raise RuntimeErrorCode("AUTO_UNSEAL_PROFILE_REQUIRED", "provider-compatible external auto-unseal is required")
    if seal["type"] == "transit":
        if set(seal) != {"type", "address", "key_name", "mount_path", "ca_file"}:
            raise RuntimeErrorCode("TRANSIT_PROFILE_INVALID", "registered Transit address, key and CA are required")
        address = urlsplit(seal["address"])
        if (address.scheme != "https" or not address.hostname or address.username or address.password
                or address.query or address.fragment or address.path not in ("", "/")
                or seal["key_name"] != "railshot-" + environment_id or seal["mount_path"] != "transit/"
                or not Path(seal["ca_file"]).is_absolute()):
            raise RuntimeErrorCode("TRANSIT_PROFILE_INVALID", "Transit profile must bind a distinct environment key and trusted CA")
    tls = vault["tls"]
    if (not isinstance(tls, dict) or set(tls) != {"cert_file", "key_file", "ca_file"}
            or any(not isinstance(value, str) or not Path(value).is_absolute() for value in tls.values())
            or not isinstance(vault["seal_env_file"], str) or not Path(vault["seal_env_file"]).is_absolute()):
        raise RuntimeErrorCode("VAULT_TLS_REQUIRED", "staged Vault TLS and seal material are required")
    eso = profile["external_secrets"]
    if (not isinstance(eso, dict) or set(eso) != {"manifest_sha256"}
            or re.fullmatch(r"[a-f0-9]{64}", eso.get("manifest_sha256", "")) is None):
        raise RuntimeErrorCode("ESO_PROFILE_INVALID", "a pinned External Secrets manifest digest is required")
    recovery = profile["recovery"]
    if recovery != {"helper": str(ESCROW_HELPER)}:
        raise RuntimeErrorCode("RECOVERY_ESCROW_REQUIRED", "the fixed external recovery escrow helper is required")
    return profile


class Runner:
    def command(self, argv, *, input_text=None, timeout=120, sensitive=False, json_output=False, accepted=(0,), denied_false=False):
        try:
            result = subprocess.run(argv, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=timeout, env={"PATH": os.environ.get("PATH", "")})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeErrorCode("SECRETS_COMMAND_UNKNOWN" if sensitive else "SECRETS_COMMAND_FAILED",
                                   "secrets runtime command did not complete", unknown=sensitive) from exc
        if result.returncode not in accepted:
            if denied_false and re.search(r"Code:\s*403\b", result.stderr):
                return False
            raise RuntimeErrorCode("SECRETS_COMMAND_UNKNOWN" if sensitive else "SECRETS_COMMAND_FAILED",
                                   "secrets runtime command failed", unknown=sensitive)
        if json_output:
            try:
                return json.loads(result.stdout)
            except ValueError as exc:
                raise RuntimeErrorCode("SECRETS_READBACK_INVALID", "secrets runtime returned invalid JSON", unknown=sensitive) from exc
        return result.stdout

    def kube(self, *args, document=None, sensitive=False, timeout=120):
        raw = self.command(["/usr/local/bin/k3s", "kubectl", "--request-timeout=15s", *args],
                           input_text=None if document is None else json.dumps(document), timeout=timeout,
                           sensitive=sensitive)
        # Unlike get/apply, delete supports only human/name output, not JSON.
        # Its checked exit status plus --wait confirms deletion; callers read
        # the newly created resource separately during restart/readiness checks.
        if "delete" in args:
            return None
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError as exc:
            raise RuntimeErrorCode("KUBERNETES_READBACK_INVALID", "Kubernetes returned invalid JSON") from exc

    def vault(self, token, args, payload=None, *, json_output=False, sensitive=False, denied_false=False):
        # Token and optional policy never appear in argv, the process list or logs.
        script = ('IFS= read -r token; export VAULT_TOKEN="$token"; IFS= read -r body || true; '
                  'if [ -n "$body" ]; then printf %s "$body" | base64 -d | exec vault "$@"; '
                  'else exec vault "$@"; fi')
        encoded = "" if payload is None else base64.b64encode(payload.encode()).decode()
        return self.command(["/usr/local/bin/k3s", "kubectl", "-n", "railshot-secrets", "exec", "-i", "vault-0", "--",
                             "sh", "-ceu", script, "railshot-vault", *args], input_text=token + "\n" + encoded + "\n",
                            timeout=120, sensitive=sensitive, json_output=json_output, denied_false=denied_false)

    def token_valid(self, token):
        result = self.vault(token, ["token", "lookup", "-format=json"], json_output=True, sensitive=True, denied_false=True)
        if result is False:
            return False
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise RuntimeErrorCode("ROOT_STATUS_UNKNOWN", "root credential status could not be proven", unknown=True)
        return True

    def custody(self, environment_id, kind, operation_id, *, action="lookup", material=None):
        try:
            info = ESCROW_HELPER.lstat()
        except OSError as exc:
            raise RuntimeErrorCode("RECOVERY_ESCROW_REQUIRED", "recovery escrow helper is unavailable") from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or not os.access(ESCROW_HELPER, os.X_OK)):
            raise RuntimeErrorCode("RECOVERY_ESCROW_UNSAFE", "recovery escrow helper ownership or mode is unsafe")
        payload = {"version": 1, "environment_id": environment_id, "kind": kind, "operation_id": operation_id, "action": action}
        if action == "store": payload["material"] = material
        result = self.command([str(ESCROW_HELPER)], input_text=json.dumps(payload, separators=(",", ":")), sensitive=True,
                              json_output=True)
        if result == {"stored": False} and action != "store":
            return None
        expected = {"stored", "receipt_id"} | ({"material"} if action == "recover" else set())
        if (not isinstance(result, dict) or set(result) != expected or result.get("stored") is not True
                or not isinstance(result.get("receipt_id"), str)):
            raise RuntimeErrorCode("RECOVERY_ESCROW_UNKNOWN", "recovery escrow acknowledgement is invalid", unknown=True)
        return result


def seal_stanza(seal):
    if seal["type"] == "awskms":
        return 'seal "awskms" {}'
    if seal["type"] == "gcpckms":
        return 'seal "gcpckms" {}'
    # Vault's supported token variable is VAULT_TOKEN. The address and CA are
    # explicit so the local CLI's VAULT_ADDR/VAULT_CACERT cannot redirect seal traffic.
    return ('seal "transit" {\n  address = ' + json.dumps(seal["address"]) +
            '\n  token = "env://VAULT_TOKEN"\n  key_name = ' + json.dumps(seal["key_name"]) +
            '\n  mount_path = "transit/"\n  tls_ca_cert = "/vault/transit/ca.crt"\n}\n')


def documents(profile, environment_id):
    ns, vault, storage = profile["namespace"], profile["vault"], profile["storage"]
    material = profile.get("_material_names", {})
    tls_name, seal_name = material.get("tls", vault["tls_secret"]), material.get("seal", vault["seal_secret"])
    owner = {"app.kubernetes.io/managed-by": "railshot-secrets", "railshot.io/environment": environment_id}
    hcl = ('ui = false\ndisable_mlock = true\nmax_request_duration = "420s"\napi_addr = "https://vault.railshot-secrets.svc:8200"\n'
           'cluster_addr = "https://vault.railshot-secrets.svc:8201"\n'
           'listener "tcp" { address = "0.0.0.0:8200" cluster_address = "0.0.0.0:8201" tls_cert_file = "/vault/tls/tls.crt" '
           'tls_key_file = "/vault/tls/tls.key" tls_client_ca_file = "/vault/tls/ca.crt" }\n'
           'storage "raft" { path = "/vault/data" node_id = "vault-0" }\n' + seal_stanza(vault["seal"]) + '\n')
    config_name = "vault-config-" + hashlib.sha256(hcl.encode()).hexdigest()[:16]
    result = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": owner}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "vault", "namespace": ns, "labels": owner}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": "railshot-vault-token-reviewer-" + hashlib.sha256(environment_id.encode()).hexdigest()[:12], "labels": owner},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "system:auth-delegator"},
         "subjects": [{"kind": "ServiceAccount", "name": "vault", "namespace": ns}]},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "railshot-readiness", "namespace": ns, "labels": owner}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": ns, "labels": owner},
         "immutable": True, "data": {"vault.hcl": hcl}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "vault-data-vault-0", "namespace": ns, "labels": owner},
         "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage["class_name"],
                  "resources": {"requests": {"storage": storage["capacity"]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "vault", "namespace": ns, "labels": owner},
         "spec": {"selector": {"app.kubernetes.io/name": "vault"}, "ports": [
             {"name": "https", "port": 8200, "targetPort": 8200},
             {"name": "cluster", "port": 8201, "targetPort": 8201}]}},
        {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "vault", "namespace": ns, "labels": owner},
         "spec": {"serviceName": "vault", "replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "vault"}},
                  "template": {"metadata": {"labels": {**owner, "app.kubernetes.io/name": "vault"}}, "spec": {
                      "serviceAccountName": "vault", "automountServiceAccountToken": True,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 100, "runAsGroup": 1000, "fsGroup": 1000},
                      # The image entrypoint adds -config=/vault/config itself;
                      # invoking Vault directly prevents loading the seal twice.
                      "containers": [{"name": "vault", "image": vault["image"], "command": ["/bin/vault"],
                         "args": ["server", "-config=/vault/config/vault.hcl"],
                         "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]}},
                         "ports": [{"name": "https", "containerPort": 8200},
                                   {"name": "cluster", "containerPort": 8201}], "env": [
                             {"name": "VAULT_ADDR", "value": "https://vault.railshot-secrets.svc:8200"},
                             {"name": "VAULT_CACERT", "value": "/vault/tls/ca.crt"}],
                         "envFrom": [{"secretRef": {"name": seal_name}}],
                         "readinessProbe": {"tcpSocket": {"port": "https"}, "periodSeconds": 2,
                                            "timeoutSeconds": 1, "failureThreshold": 90},
                         "volumeMounts": [{"name": "config", "mountPath": "/vault/config", "readOnly": True},
                                          {"name": "tls", "mountPath": "/vault/tls", "readOnly": True},
                                          {"name": "data", "mountPath": "/vault/data"}]}],
                      "volumes": [{"name": "config", "configMap": {"name": config_name}},
                                  {"name": "tls", "secret": {"secretName": tls_name}},
                                  {"name": "data", "persistentVolumeClaim": {"claimName": "vault-data-vault-0"}}]}}}}]
    if vault["seal"]["type"] == "transit":
        pod = result[-1]["spec"]["template"]["spec"]
        pod["volumes"].append({"name": "transit", "secret": {"secretName": material.get("transit", "vault-transit-ca")}})
        pod["containers"][0]["volumeMounts"].append({"name": "transit", "mountPath": "/vault/transit", "readOnly": True})
    return result


def marker(runner, environment_id, phase, evidence=None):
    doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "railshot-secrets-state",
           "namespace": "railshot-secrets", "labels": {"app.kubernetes.io/managed-by": "railshot-secrets",
           "railshot.io/environment": environment_id}}, "data": {"phase": phase, **(evidence or {})}}
    runner.kube("apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json", document=doc,
                sensitive=phase != "ready")


def state(runner):
    return runner.kube("-n", "railshot-secrets", "get", "configmap", "railshot-secrets-state", "--ignore-not-found", "-o", "json")


def validate_prerequisites(runner, profile, eso_manifest):
    digest = hashlib.sha256(Path(eso_manifest).read_bytes()).hexdigest()
    if digest != profile["external_secrets"]["manifest_sha256"]:
        raise RuntimeErrorCode("ESO_MANIFEST_MISMATCH", "External Secrets manifest digest differs")
    storage = runner.kube("get", "storageclass", profile["storage"]["class_name"], "-o", "json")
    if storage.get("reclaimPolicy") != "Retain":
        raise RuntimeErrorCode("PERSISTENT_STORAGE_UNSAFE", "Vault storage class must retain volumes")
    names = profile.get("_material_names", {})
    tls = runner.kube("-n", "railshot-secrets", "get", "secret", names.get("tls", profile["vault"]["tls_secret"]), "-o", "json")
    if not {"tls.crt", "tls.key", "ca.crt"} <= set(tls.get("data", {})):
        raise RuntimeErrorCode("VAULT_TLS_REQUIRED", "registered Vault TLS Secret is incomplete")
    runner.kube("-n", "railshot-secrets", "get", "secret", names.get("seal", profile["vault"]["seal_secret"]), "-o", "json")


def staged_file(path, *, private=False):
    path = Path(path)
    try:
        info = path.lstat(); raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeErrorCode("SECRETS_MATERIAL_REQUIRED", "staged secrets material is unavailable") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not raw
            or info.st_mode & (0o077 if private else 0o022)):
        raise RuntimeErrorCode("SECRETS_MATERIAL_UNSAFE", "staged secrets material ownership or mode is unsafe")
    return raw


def material_names(profile):
    vault = profile["vault"]
    tls = {"tls.crt": base64.b64encode(staged_file(vault["tls"]["cert_file"])).decode(),
           "tls.key": base64.b64encode(staged_file(vault["tls"]["key_file"], private=True)).decode(),
           "ca.crt": base64.b64encode(staged_file(vault["tls"]["ca_file"])).decode()}
    seal = {k: base64.b64encode(v.encode()).decode() for k, v in private_json(vault["seal_env_file"]).items()}
    values = [("tls", vault["tls_secret"], tls), ("seal", vault["seal_secret"], seal)]
    if vault["seal"]["type"] == "transit":
        values.append(("transit", "vault-transit-ca", {"ca.crt": base64.b64encode(staged_file(vault["seal"]["ca_file"])).decode()}))
    return {purpose: base[:40].rstrip("-") + "-" + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]
            for purpose, base, value in values}


def install_prerequisites(runner, profile, environment_id, storage_manifest):
    expected = profile["storage"]["manifest_sha256"]
    if hashlib.sha256(Path(storage_manifest).read_bytes()).hexdigest() != expected:
        raise RuntimeErrorCode("STORAGE_MANIFEST_MISMATCH", "storage provider manifest digest differs")
    runner.command(["/usr/local/bin/k3s", "kubectl", "apply", "--server-side", "--field-manager=railshot-storage-provider",
                    "-f", str(storage_manifest)], timeout=300)
    vault = profile["vault"]; tls = vault["tls"]
    tls_data = {"tls.crt": base64.b64encode(staged_file(tls["cert_file"])).decode(),
                "tls.key": base64.b64encode(staged_file(tls["key_file"], private=True)).decode(),
                "ca.crt": base64.b64encode(staged_file(tls["ca_file"])).decode()}
    try:
        seal_values = json.loads(staged_file(vault["seal_env_file"], private=True))
    except ValueError as exc:
        raise RuntimeErrorCode("AUTO_UNSEAL_PROFILE_INVALID", "seal environment file is invalid") from exc
    allowed = {
        "awskms": {"AWS_REGION", "VAULT_AWSKMS_SEAL_KEY_ID", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"},
        "gcpckms": {"GOOGLE_PROJECT", "GOOGLE_REGION", "GOOGLE_KMS_KEY_RING", "GOOGLE_KMS_CRYPTO_KEY", "GOOGLE_CREDENTIALS"},
        "transit": {"VAULT_TOKEN"}}
    if (not isinstance(seal_values, dict) or not seal_values or not set(seal_values) <= allowed[vault["seal"]["type"]]
            or any(not isinstance(value, str) or not value for value in seal_values.values())):
        raise RuntimeErrorCode("AUTO_UNSEAL_PROFILE_INVALID", "seal environment keys are not approved")
    labels = {"app.kubernetes.io/managed-by": "railshot-secrets", "railshot.io/environment": environment_id}
    materials = [("tls", vault["tls_secret"], "kubernetes.io/tls", tls_data),
                 ("seal", vault["seal_secret"], "Opaque", {key: base64.b64encode(value.encode()).decode()
                                                        for key, value in seal_values.items()})]
    if vault["seal"]["type"] == "transit":
        materials.append(("transit", "vault-transit-ca", "Opaque", {"ca.crt": base64.b64encode(
            staged_file(vault["seal"]["ca_file"])).decode()}))
    names = {}
    for purpose, base, kind, values in materials:
        name = base[:40].rstrip("-") + "-" + hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]
        names[purpose] = name
        document = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "railshot-secrets",
                    "labels": labels}, "type": kind, "immutable": True, "data": values}
        existing = runner.kube("-n", "railshot-secrets", "get", "secret", name, "--ignore-not-found", "-o", "json")
        if existing is not None and (existing.get("data") != values or existing.get("type") != kind
                                     or existing.get("immutable") is not True
                                     or any(existing.get("metadata", {}).get("labels", {}).get(k) != v for k, v in labels.items())):
            raise RuntimeErrorCode("SECRETS_MATERIAL_CONFLICT", "existing TLS or seal Secret differs")
        runner.kube("apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json",
                    document=document, sensitive=True)
    profile["_material_names"] = names


def apply_runtime(runner, profile, environment_id, eso_manifest):
    for document in documents(profile, environment_id):
        runner.kube("apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json", document=document)
    runner.command(["/usr/local/bin/k3s", "kubectl", "apply", "--server-side", "--field-manager=railshot-secrets-operator",
                    "-f", str(eso_manifest)], timeout=300)
    runner.command(["/usr/local/bin/k3s", "kubectl", "wait", "--for=condition=Established", "--timeout=300s",
                    "crd/secretstores.external-secrets.io", "crd/externalsecrets.external-secrets.io"], timeout=330)
    for deployment in ("external-secrets", "external-secrets-webhook", "external-secrets-cert-controller"):
        runner.command(["/usr/local/bin/k3s", "kubectl", "-n", "external-secrets", "rollout", "status",
                        "deployment/" + deployment, "--timeout=300s"], timeout=330)
    runner.command(["/usr/local/bin/k3s", "kubectl", "-n", "railshot-secrets", "rollout", "status", "statefulset/vault",
                    "--timeout=300s"], timeout=330)


def vault_status(runner):
    raw = runner.command(["/usr/local/bin/k3s", "kubectl", "-n", "railshot-secrets", "exec", "vault-0", "--",
                          "vault", "status", "-format=json"], timeout=30, accepted=(0, 2))
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeErrorCode("VAULT_STATUS_INVALID", "Vault status is invalid") from exc


def wait_for(runner, observe, ready, code, message, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        value = observe()
        if ready(value):
            return value
        if time.monotonic() >= deadline:
            raise RuntimeErrorCode(code, message)
        time.sleep(2)


def wait_vault_status(runner, ready, code, message, timeout=180):
    def observed():
        try:
            return vault_status(runner)
        except RuntimeErrorCode as exc:
            if exc.code not in ("SECRETS_COMMAND_FAILED", "SECRETS_COMMAND_UNKNOWN"):
                raise
            return None
    return wait_for(runner, observed, lambda value: value is not None and ready(value), code, message, timeout)


def wait_readiness(runner, nonce):
    namespace = "railshot-secrets"
    def observed():
        return runner.kube("-n", namespace, "get", "secret", "railshot-readiness", "--ignore-not-found", "-o", "json")
    expected = base64.b64encode(nonce.encode()).decode()
    wait_for(runner, observed, lambda value: value is not None and value.get("data", {}).get("value") == expected,
             "ESO_READBACK_FAILED", "Vault write did not synchronize to the Kubernetes Secret")


def readiness_test(runner, token, tls_secret):
    namespace = "railshot-secrets"
    nonce = secrets.token_urlsafe(24)
    policy = 'path "railshot/data/readiness" { capabilities = ["read"] }\n'
    runner.vault(token, ["policy", "write", "railshot-readiness", "-"], payload=policy, sensitive=True)
    runner.vault(token, ["write", "auth/kubernetes/role/railshot-readiness",
                 "bound_service_account_names=railshot-readiness", "bound_service_account_namespaces=railshot-secrets",
                 "audience=vault", "policies=railshot-readiness", "ttl=10m"], sensitive=True)
    store = {"apiVersion": "external-secrets.io/v1", "kind": "SecretStore",
             "metadata": {"name": "railshot-readiness", "namespace": namespace,
                          "labels": {"app.kubernetes.io/managed-by": "railshot-secrets"}},
             "spec": {"provider": {"vault": {"server": "https://vault.railshot-secrets.svc:8200", "path": "railshot",
                      "version": "v2", "caProvider": {"type": "Secret", "name": tls_secret, "key": "ca.crt"},
                      "auth": {"kubernetes": {"mountPath": "kubernetes", "role": "railshot-readiness",
                               "serviceAccountRef": {"name": "railshot-readiness", "audiences": ["vault"]}}}}}}}
    external = {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
                "metadata": {"name": "railshot-readiness", "namespace": namespace,
                             "labels": {"app.kubernetes.io/managed-by": "railshot-secrets"}},
                "spec": {"refreshInterval": "5s", "secretStoreRef": {"kind": "SecretStore", "name": "railshot-readiness"},
                         "target": {"name": "railshot-readiness", "creationPolicy": "Owner"},
                         "data": [{"secretKey": "value", "remoteRef": {"key": "readiness", "property": "value"}}]}}
    runner.kube("apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json", document=store)
    runner.kube("apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json", document=external)
    runner.vault(token, ["kv", "put", "-mount=railshot", "readiness", "value=" + nonce], sensitive=True)
    wait_readiness(runner, nonce)
    return nonce


def custody_operation(environment_id, kind, generation="initial"):
    return hashlib.sha256((environment_id + "\0" + kind + "\0" + generation).encode()).hexdigest()


def configure_vault(runner, environment_id, profile):
    current = state(runner)
    if current is not None and (current.get("metadata", {}).get("labels", {}).get("railshot.io/environment") != environment_id
            or current.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by") != "railshot-secrets"):
        raise RuntimeErrorCode("SECRETS_STATE_CONFLICT", "runtime checkpoint ownership differs")
    checkpoint = (current or {}).get("data", {})
    init_op = custody_operation(environment_id, "vault-initialization")
    delivery_op = custody_operation(environment_id, "vault-delivery-approle")
    status = wait_vault_status(runner, lambda value: isinstance(value.get("initialized"), bool),
                              "VAULT_LISTENER_NOT_READY", "Vault listener did not become available")
    if status.get("initialized"):
        saved = runner.custody(environment_id, "vault-initialization", init_op, action="recover")
        if saved is None:
            # The init response itself can be irretrievably lost; a second init never recovers it.
            raise RuntimeErrorCode("VAULT_INITIALIZATION_UNKNOWN", "initialized Vault has no recoverable custody receipt", unknown=True)
        init, init_receipt = saved["material"], saved["receipt_id"]
    else:
        if runner.custody(environment_id, "vault-initialization", init_op) is not None:
            raise RuntimeErrorCode("VAULT_STATE_MISMATCH", "custody records an initialized environment but its Vault is empty", unknown=True)
        init = runner.command(["/usr/local/bin/k3s", "kubectl", "-n", "railshot-secrets", "exec", "vault-0", "--",
                               "env", "VAULT_CLIENT_TIMEOUT=480s", "VAULT_MAX_RETRIES=0", "vault", "operator", "init", "-format=json",
                               "-recovery-shares=5", "-recovery-threshold=3"],
                              timeout=510, sensitive=True, json_output=True)
        if not isinstance(init, dict) or not isinstance(init.get("root_token"), str) or len(init.get("recovery_keys_b64", [])) != 5:
            raise RuntimeErrorCode("VAULT_INITIALIZATION_UNKNOWN", "Vault initialization response is incomplete", unknown=True)
        init_receipt = runner.custody(environment_id, "vault-initialization", init_op, action="store", material=init)["receipt_id"]
    if not isinstance(init, dict) or not isinstance(init.get("root_token"), str):
        raise RuntimeErrorCode("RECOVERY_MATERIAL_INVALID", "initialization envelope is incomplete", unknown=True)
    evidence = {"initialization_operation_id": init_op, "initialization_receipt_id": init_receipt,
                "delivery_operation_id": delivery_op}
    token = init["root_token"]
    delivery_saved = runner.custody(environment_id, "vault-delivery-approle", delivery_op)
    if not runner.token_valid(token):
        if (checkpoint.get("phase") in ("root-revocation-pending", "ready") and checkpoint.get("readiness_verified") == "true"
                and checkpoint.get("initialization_receipt_id") == init_receipt and delivery_saved is not None
                and checkpoint.get("delivery_receipt_id") == delivery_saved["receipt_id"]):
            marker(runner, environment_id, "ready", {**evidence, "delivery_receipt_id": delivery_saved["receipt_id"], "readiness_verified": "true"})
            return
        raise RuntimeErrorCode("ROOT_REVOCATION_UNPROVEN", "revoked root lacks matching completed stage evidence", unknown=True)
    marker(runner, environment_id, "initialization-stored", evidence)
    mounts = runner.vault(token, ["secrets", "list", "-format=json"], json_output=True)
    if "railshot/" not in mounts:
        runner.vault(token, ["secrets", "enable", "-path=railshot", "kv-v2"], sensitive=True)
    auth = runner.vault(token, ["auth", "list", "-format=json"], json_output=True)
    if "kubernetes/" not in auth:
        runner.vault(token, ["auth", "enable", "kubernetes"], sensitive=True)
    if "approle/" not in auth:
        runner.vault(token, ["auth", "enable", "approle"], sensitive=True)
    # Vault runs in the cluster with TokenReview RBAC. Omitting reviewer token
    # and CA makes the auth plugin reread the projected, rotating service-account
    # token and local CA instead of persisting a token that dies with this pod.
    runner.vault(token, ["write", "auth/kubernetes/config",
                 "kubernetes_host=https://kubernetes.default.svc:443"], sensitive=True)
    audits = runner.vault(token, ["audit", "list", "-format=json"], json_output=True)
    if "file/" not in audits:
        runner.vault(token, ["audit", "enable", "file", "file_path=/vault/data/audit.log", "log_raw=false"], sensitive=True)
    readiness_nonce = readiness_test(runner, token, profile.get("_material_names", {}).get("tls", profile["vault"]["tls_secret"]))
    # A pod replacement proves that the retained volume and external seal can
    # recover before any ready receipt or root-token revocation.
    runner.kube("-n", "railshot-secrets", "delete", "pod", "vault-0", "--wait=true", sensitive=True, timeout=180)
    runner.command(["/usr/local/bin/k3s", "kubectl", "-n", "railshot-secrets", "rollout", "status", "statefulset/vault",
                    "--timeout=300s"], timeout=330)
    wait_vault_status(runner, lambda value: value.get("initialized") is True and value.get("sealed") is False,
                      "VAULT_RESTART_RECOVERY_FAILED", "Vault did not auto-unseal after pod replacement", timeout=180)
    runner.kube("-n", "railshot-secrets", "delete", "secret", "railshot-readiness", "--ignore-not-found", "--wait=true",
                sensitive=True)
    # ESO must re-read the same persisted value after Vault's restart.
    wait_readiness(runner, readiness_nonce)
    runner.kube("-n", "railshot-secrets", "delete", "externalsecret", "railshot-readiness", "--ignore-not-found", "--wait=true",
                sensitive=True)
    runner.kube("-n", "railshot-secrets", "delete", "secretstore", "railshot-readiness", "--ignore-not-found", "--wait=true",
                sensitive=True)
    runner.kube("-n", "railshot-secrets", "delete", "secret", "railshot-readiness", "--ignore-not-found", "--wait=true",
                sensitive=True)
    runner.vault(token, ["kv", "metadata", "delete", "-mount=railshot", "readiness"], sensitive=True)
    policy = ('path "railshot/data/projects/*" { capabilities = ["create", "read", "update"] }\n'
              'path "sys/policies/acl/railshot-*" { capabilities = ["create", "read", "update"] }\n'
              'path "auth/kubernetes/role/railshot-*" { capabilities = ["create", "read", "update"] }\n'
              'path "auth/approle/role/railshot-delivery/secret-id" { capabilities = ["create", "update"] }\n'
              'path "auth/approle/role/railshot-delivery/secret-id-accessor/destroy" { capabilities = ["update"] }\n')
    runner.vault(token, ["policy", "write", "railshot-delivery", "-"], payload=policy, sensitive=True)
    issuer_policy = ('path "auth/approle/role/railshot-delivery/role-id" { capabilities = ["read"] }\n'
                     'path "auth/approle/role/railshot-delivery/secret-id" { capabilities = ["create", "update"] }\n'
                     'path "auth/approle/role/railshot-delivery/secret-id/destroy" { capabilities = ["update"] }\n')
    runner.vault(token, ["policy", "write", "railshot-delivery-issuer", "-"], payload=issuer_policy, sensitive=True)
    runner.vault(token, ["write", "auth/kubernetes/role/railshot-delivery-issuer",
                 "bound_service_account_names=vault", "bound_service_account_namespaces=railshot-secrets",
                 "audience=vault", "token_policies=railshot-delivery-issuer", "token_ttl=5m", "token_max_ttl=10m"], sensitive=True)
    runner.vault(token, ["write", "auth/approle/role/railshot-delivery", "token_policies=railshot-delivery",
                 "token_ttl=15m", "token_max_ttl=30m", "secret_id_ttl=24h", "secret_id_num_uses=1000"], sensitive=True)
    if delivery_saved is None:
        role_id = runner.vault(token, ["read", "-format=json", "auth/approle/role/railshot-delivery/role-id"],
                               json_output=True, sensitive=True).get("data", {}).get("role_id")
        secret_id = runner.vault(token, ["write", "-format=json", "-f", "auth/approle/role/railshot-delivery/secret-id"],
                                 json_output=True, sensitive=True).get("data", {}).get("secret_id")
        if not isinstance(role_id, str) or not isinstance(secret_id, str):
            raise RuntimeErrorCode("DELIVERY_CREDENTIAL_UNKNOWN", "Vault delivery AppRole was not created", unknown=True)
        delivery_saved = runner.custody(environment_id, "vault-delivery-approle", delivery_op, action="store",
                                       material={"role_id": role_id, "secret_id": secret_id})
    evidence.update(delivery_receipt_id=delivery_saved["receipt_id"], readiness_verified="true")
    marker(runner, environment_id, "root-revocation-pending", evidence)
    runner.vault(token, ["token", "revoke", "-self"], sensitive=True)
    if runner.token_valid(token):
        raise RuntimeErrorCode("ROOT_REVOCATION_UNPROVEN", "root token remains usable after revocation", unknown=True)
    marker(runner, environment_id, "ready", evidence)


def verify(runner, profile):
    pvc = runner.kube("-n", "railshot-secrets", "get", "pvc", "vault-data-vault-0", "-o", "json")
    if (pvc.get("status", {}).get("phase") != "Bound" or not pvc.get("spec", {}).get("volumeName")
            or pvc.get("spec", {}).get("storageClassName") != profile["storage"]["class_name"]):
        raise RuntimeErrorCode("VAULT_STORAGE_NOT_READY", "Vault persistent volume is not bound")
    sts = runner.kube("-n", "railshot-secrets", "get", "statefulset", "vault", "-o", "json")
    if sts.get("status", {}).get("readyReplicas") != 1 or sts.get("status", {}).get("currentRevision") != sts.get("status", {}).get("updateRevision"):
        raise RuntimeErrorCode("VAULT_WORKLOAD_NOT_READY", "Vault StatefulSet is not ready")
    status = wait_vault_status(runner, lambda value: value.get("initialized") is True and value.get("sealed") is False,
                               "VAULT_NOT_READY", "Vault did not become initialized and unsealed")
    if status.get("initialized") is not True or status.get("sealed") is not False:
        raise RuntimeErrorCode("VAULT_NOT_READY", "Vault is not initialized and unsealed")
    eso = runner.kube("-n", "external-secrets", "get", "deployment", "external-secrets", "-o", "json")
    if eso.get("status", {}).get("availableReplicas", 0) < 1:
        raise RuntimeErrorCode("ESO_NOT_READY", "External Secrets controller is not available")
    current = state(runner)
    if current is None or current.get("data", {}).get("phase") != "ready":
        raise RuntimeErrorCode("SECRETS_READINESS_UNPROVEN", "secrets readiness marker is absent")
    return {"persistent_volume_bound": True, "tls_configured": True, "vault_initialized": True,
            "vault_unsealed": True, "external_secrets_ready": True}


def reconcile(config, environment_id, provider, eso_manifest, storage_manifest, phase, runner=None):
    runner = runner or Runner(); profile = json.loads(json.dumps(validate(config, environment_id, provider)))
    previous = None
    if phase == "configure":
        # Namespace creation is the only prerequisite mutation; TLS/seal
        # material still has to be placed there by the trusted operator profile.
        runner.kube("apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json",
                    document=documents(profile, environment_id)[0])
        previous = runner.kube("-n", "railshot-secrets", "get", "statefulset", "vault", "--ignore-not-found", "-o", "json")
        install_prerequisites(runner, profile, environment_id, storage_manifest)
    else:
        # Resolve content-addressed material names without mutating resources.
        profile["_material_names"] = material_names(profile)
    validate_prerequisites(runner, profile, eso_manifest)
    try:
        if phase == "configure":
            apply_runtime(runner, profile, environment_id, eso_manifest)
            configure_vault(runner, environment_id, profile)
        return verify(runner, profile)
    except RuntimeErrorCode as original:
        # An indeterminate initialization or credential mutation must be observed
        # through its custody/checkpoint evidence before any further mutation.
        if (not original.unknown and phase == "configure" and previous is not None
                and previous.get("metadata", {}).get("labels", {}).get("railshot.io/environment") == environment_id
                and previous.get("status", {}).get("readyReplicas") == 1
                and previous["status"].get("currentRevision")
                and previous["status"]["currentRevision"] == previous["status"].get("updateRevision")
                and previous["status"].get("observedGeneration", 0) >= previous["metadata"].get("generation", 1)):
            # Retain every credential version; restore only the prior, owned desired workload.
            # A failed installation is not a viable rollback target during resume.
            restored = {"apiVersion": "apps/v1", "kind": "StatefulSet",
                        "metadata": {"name": "vault", "namespace": "railshot-secrets", "labels": previous["metadata"]["labels"]},
                        "spec": previous["spec"]}
            try:
                runner.kube("apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json", document=restored, sensitive=True)
                runner.command(["/usr/local/bin/k3s", "kubectl", "-n", "railshot-secrets", "rollout", "status", "statefulset/vault", "--timeout=300s"], timeout=330)
            except RuntimeErrorCode as rollback:
                # Keep the initiating failure; a failed recovery must not replace
                # it with a generic rollout error or discard uncertainty.
                raise RuntimeErrorCode(original.code, str(original), unknown=original.unknown or rollback.unknown) from rollback
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--eso-manifest", required=True, type=Path)
    parser.add_argument("--storage-manifest", required=True, type=Path)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--provider", required=True, choices=("aws", "gcp", "openstack", "onprem"))
    parser.add_argument("--phase", required=True, choices=("configure", "verify"))
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = {"request_id": args.request_id, "target_id": args.environment_id, "node_id": args.node_id,
               "stage": "secrets-" + args.phase, "nonce": args.nonce, "secrets_ready": False}
    try:
        receipt["checks"] = reconcile(private_json(args.config), args.environment_id, args.provider,
                                      args.eso_manifest, args.storage_manifest, args.phase)
        receipt["secrets_ready"] = True; receipt["status"] = "succeeded"; code = 0
    except RuntimeErrorCode as exc:
        receipt.update(status="unknown" if exc.unknown else "blocked", error={"code": exc.code,
                       "outcome_unknown": exc.unknown}); code = 4 if exc.unknown else 3
    except (OSError, ValueError, KeyError, TypeError):
        receipt.update(status="blocked", error={"code": "SECRETS_RUNTIME_INVALID", "outcome_unknown": False}); code = 3
    # This file is fetched through the private Ansible temp directory; it never contains material.
    args.receipt.write_text(json.dumps(receipt, separators=(",", ":"))); args.receipt.chmod(0o600)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
