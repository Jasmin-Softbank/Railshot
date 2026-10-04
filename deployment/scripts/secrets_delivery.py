#!/usr/bin/env python3
"""Deliver one immutable project configuration to a registered runtime.

The request may contain plaintext secret material, so it is accepted only from a
private regular file.  Cluster and Vault addresses are resolved exclusively from
the operator-owned config file.  stdout contains metadata only.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import tempfile
from urllib.parse import quote

LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
RESERVED = {"PORT", "DATABASE_URL", "MIGRATION_DATABASE_URL", "PGHOST", "PGPORT",
            "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE", "PGSSLROOTCERT"}


class DeliveryError(RuntimeError):
    def __init__(self, code, message, *, unknown=False):
        super().__init__(message)
        self.code, self.unknown = code, unknown


def private_json(path: Path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeliveryError("PRIVATE_FILE_UNAVAILABLE", "private input is unavailable") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or info.st_size <= 0 or info.st_size > 1024 * 1024):
        raise DeliveryError("PRIVATE_FILE_UNSAFE", "private input must be an owned 0600 regular file")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError) as exc:
        raise DeliveryError("INVALID_JSON", "private input is not valid JSON") from exc


def exact(value, fields, message):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise DeliveryError("INVALID_REQUEST", message)


def validate_request(value):
    exact(value, ("version", "operation_id", "project_id", "binding_id", "revision_id",
                  "environment_id", "application_id", "app", "phase", "variables"),
          "request fields differ from the delivery v1 contract")
    if value["version"] != 1 or value["phase"] not in ("prepare", "apply", "verify"):
        raise DeliveryError("INVALID_REQUEST", "unsupported version or phase")
    for key in ("operation_id", "project_id", "binding_id", "revision_id", "environment_id", "application_id"):
        if not isinstance(value[key], str) or IDENTITY.fullmatch(value[key]) is None:
            raise DeliveryError("INVALID_REQUEST", f"invalid {key}")
    if not isinstance(value["app"], str) or LABEL.fullmatch(value["app"]) is None:
        raise DeliveryError("INVALID_REQUEST", "invalid app")
    if not isinstance(value["variables"], list) or len(value["variables"]) > 256:
        raise DeliveryError("INVALID_REQUEST", "variables must be a bounded list")
    names = set()
    for variable in value["variables"]:
        exact(variable, ("name", "kind", "value", "required"), "invalid variable fields")
        name = variable["name"]
        if (not isinstance(name, str) or ENV_NAME.fullmatch(name) is None or name in names
                or name in RESERVED or variable["kind"] not in ("plain", "secret")
                or not isinstance(variable["value"], str) or len(variable["value"].encode()) > 65536
                or type(variable["required"]) is not bool):
            raise DeliveryError("INVALID_VARIABLE", f"invalid or reserved variable: {name if isinstance(name, str) else '?'}")
        if variable["required"] and not variable["value"]:
            raise DeliveryError("REQUIRED_VARIABLE_EMPTY", f"required variable is empty: {name}")
        names.add(name)
    return value


def select_target(config, request):
    if not isinstance(config, dict) or config.get("version") != 1:
        raise DeliveryError("INVALID_CONFIGURATION", "trusted delivery configuration v1 required")
    target = (config.get("environments", {}).get(request["environment_id"])
              if isinstance(config.get("environments"), dict) else
              config if config.get("environment_id") == request["environment_id"] else None)
    if not isinstance(target, dict):
        raise DeliveryError("ENVIRONMENT_NOT_REGISTERED", "environment is not registered")
    target = dict(target); target["environment_id"] = request["environment_id"]
    app = target.get("applications", {}).get(request["application_id"])
    if app is None:
        app = registered_application(target, config, request)
    if not isinstance(app, dict) or app.get("app") != request["app"]:
        raise DeliveryError("APPLICATION_NOT_REGISTERED", "application is not registered in the environment")
    required_target = {"registry_file", "vault"}
    required_app = {"app", "namespace", "deployment", "service", "container", "service_account", "secret_store"}
    required_vault = {"namespace", "pod", "mount", "auth_mount", "token_file", "ca_file"}
    if (not required_target <= set(target) or set(app) != required_app or not required_vault <= set(target["vault"])
            or set(target["vault"]) - required_vault - {"custody_config_file", "credential_operation_id"}):
        raise DeliveryError("INVALID_CONFIGURATION", "registered environment configuration is incomplete")
    for field in ("namespace", "deployment", "service", "service_account"):
        if not isinstance(app[field], str) or LABEL.fullmatch(app[field]) is None:
            raise DeliveryError("INVALID_CONFIGURATION", "registered Kubernetes identity is invalid")
    if app["container"] is not None and (not isinstance(app["container"], str) or LABEL.fullmatch(app["container"]) is None):
        raise DeliveryError("INVALID_CONFIGURATION", "registered container identity is invalid")
    store = app["secret_store"]
    if not isinstance(store, dict) or set(store) != {"kind", "name"} or store["kind"] != "SecretStore" or LABEL.fullmatch(store["name"]) is None:
        raise DeliveryError("INVALID_CONFIGURATION", "registered SecretStore is invalid")
    vault = target["vault"]
    if vault["namespace"] != "railshot-secrets" or vault["pod"] != "vault-0" or vault["auth_mount"] != "kubernetes":
        raise DeliveryError("INVALID_CONFIGURATION", "registered Vault Kubernetes identity differs")
    for path in (target["registry_file"], target["vault"]["token_file"], target["vault"]["ca_file"]):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise DeliveryError("INVALID_CONFIGURATION", "trusted references must be absolute paths")
    return target, app


def atomic_private(path, value):
    path = Path(path); parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DeliveryError("CREDENTIAL_DIRECTORY_UNSAFE", "credential directory must be privately owned")
    if path.exists() or path.is_symlink(): private_json(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600); json.dump(value, handle, separators=(',', ':'))
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def credential_operation(environment_id, generation="initial"):
    return hashlib.sha256((environment_id + "\0vault-delivery-approle\0" + generation).encode()).hexdigest()


def custody_handoff(target, login, *, client=None, operation_id=None):
    from recovery_escrow import Client
    vault = target['vault']; environment_id = target['environment_id']
    active_file = Path(vault['token_file'] + '.active.json')
    if operation_id is None and active_file.exists():
        active = private_json(active_file)
        if set(active) != {'environment_id', 'operation_id'} or active['environment_id'] != environment_id:
            raise DeliveryError('CREDENTIAL_RECORD_INVALID', 'active credential identity differs')
        operation_id = active['operation_id']
    operation_id = operation_id or vault.get('credential_operation_id') or credential_operation(environment_id)
    client = client or Client(Path(vault['custody_config_file']))
    recovered = client.recover(environment_id, 'vault-delivery-approle', operation_id)
    if recovered is None:
        raise DeliveryError('DELIVERY_CREDENTIAL_UNAVAILABLE', 'custody has no delivery credential for this operation')
    credential = recovered['material']
    if (not isinstance(credential, dict) or set(credential) != {'role_id', 'secret_id'}
            or not all(isinstance(value, str) and value for value in credential.values())):
        raise DeliveryError('VAULT_CREDENTIAL_UNSAFE', 'custody delivery material is invalid')
    token = login(credential)  # The old management file remains untouched until new login succeeds.
    if not isinstance(token, str) or not token:
        raise DeliveryError('VAULT_LOGIN_FAILED', 'replacement credential did not authenticate')
    atomic_private(vault['token_file'], credential)
    raw_digest = hashlib.sha256(json.dumps(credential, separators=(',', ':')).encode()).hexdigest()
    atomic_private(vault['token_file'] + '.receipt.json', {'environment_id': environment_id, 'operation_id': operation_id,
        'receipt_id': recovered['receipt_id'], 'credential_sha256': raw_digest})
    return token


def rotate_delivery_credential(target, executor, generation, *, retire_previous=False, client=None):
    from recovery_escrow import Client
    if not isinstance(generation, str) or IDENTITY.fullmatch(generation) is None or generation == 'initial':
        raise DeliveryError('INVALID_GENERATION', 'an explicit new credential generation is required')
    client = client or Client(Path(target['vault']['custody_config_file']))
    environment = target['environment_id']; operation = credential_operation(environment, generation)
    prior_path = Path(target['vault']['token_file'])
    previous = private_json(prior_path) if prior_path.exists() else None
    ack = client.lookup(environment, 'vault-delivery-approle', operation)
    attempt = Path(str(prior_path) + '.rotation-' + operation + '.json')
    if ack is None and attempt.exists():
        raise DeliveryError('CREDENTIAL_ISSUANCE_UNKNOWN', 'prior issuance lacks a custody receipt; inspect it before another generation', unknown=True)
    # The fixed Vault SA TokenRequest is available only through the trusted
    # management transport and has a dedicated audience and a ten-minute TTL.
    issuer = executor.issuer_login()
    if ack is None:
        atomic_private(attempt, {'environment_id': environment, 'operation_id': operation, 'status': 'attempted'})
        role = executor._vault_exec(issuer, ['read', '-format=json', 'auth/approle/role/railshot-delivery/role-id'], json_output=True)['data']['role_id']
        secret = executor._vault_exec(issuer, ['write', '-format=json', '-f', 'auth/approle/role/railshot-delivery/secret-id'], json_output=True)['data']['secret_id']
        ack = client.store({'version': 1, 'environment_id': environment, 'kind': 'vault-delivery-approle',
                            'operation_id': operation, 'material': {'role_id': role, 'secret_id': secret}})
        atomic_private(attempt, {'environment_id': environment, 'operation_id': operation, 'status': 'stored', 'receipt_id': ack['receipt_id']})
    custody_handoff(target, executor.login, client=client, operation_id=operation)
    atomic_private(str(prior_path) + '.active.json', {'environment_id': environment, 'operation_id': operation})
    current = private_json(prior_path)
    if retire_previous and previous is not None and previous != current:
        executor._vault_exec(issuer, ['write', 'auth/approle/role/railshot-delivery/secret-id/destroy', '-'],
                             payload=json.dumps({'secret_id': previous['secret_id']}))
    return {'status': 'succeeded', 'environment_id': environment, 'operation_id': operation,
            'receipt_id': ack['receipt_id'], 'previous_retired': retire_previous and previous is not None and previous != current}


def registered_application(target, config, request):
    """Resolve the immutable application registration instead of a static app map."""
    if request["application_id"].startswith("envapp-"):
        return registered_environment_application(target, config, request)
    path = target.get("application_registry_config", config.get("application_registry_config"))
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise DeliveryError("APPLICATION_NOT_REGISTERED", "application registration authority is unavailable")
    registry = private_json(Path(path))
    state_dir = registry.get("state_dir") if isinstance(registry, dict) and registry.get("version") == 1 else None
    if not isinstance(state_dir, str) or not Path(state_dir).is_absolute() or ".." in Path(state_dir).parts:
        raise DeliveryError("APPLICATION_REGISTRY_INVALID", "application state directory is invalid")
    home = Path(state_dir) / request["application_id"]
    receipt_path, binding_path = home / "registration.json", home / "binding.json"
    receipt = private_json(receipt_path)
    raw = binding_path.read_bytes(); binding = private_json(binding_path)
    if (receipt.get("status") != "succeeded" or receipt.get("application_id") != request["application_id"]
            or receipt.get("environment_id") != request["environment_id"] or receipt.get("app") != request["app"]
            or receipt.get("binding_sha256") != hashlib.sha256(raw).hexdigest()
            or binding.get("version") != 1 or binding.get("application_id") != request["application_id"]
            or binding.get("environment_id") != request["environment_id"]):
        raise DeliveryError("APPLICATION_REGISTRATION_CHANGED", "application registration receipt or binding differs")
    registered = binding.get("registered", {}); target = registered.get("target", {})
    if registered.get("app") != request["app"] or target.get("namespace") != request["application_id"]:
        raise DeliveryError("APPLICATION_REGISTRATION_CHANGED", "registered application identity differs")
    return {"app": request["app"], "namespace": target["namespace"], "deployment": request["app"],
            "service": request["app"], "container": None, "service_account": "default",
            "secret_store": {"kind": "SecretStore", "name": "railshot-vault"}}


def registered_environment_application(target, config, request):
    expected = "envapp-" + hashlib.sha256(json.dumps(
        [request["environment_id"], request["app"]], separators=(",", ":")).encode()).hexdigest()[:24]
    if request["application_id"] != expected:
        raise DeliveryError("APPLICATION_ID_MISMATCH", "environment application identity differs")
    directory = target.get("environment_registry_dir", config.get("environment_registry_dir"))
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise DeliveryError("APPLICATION_NOT_REGISTERED", "environment registration ledger is unavailable")
    matches = []
    for ledger_path in Path(directory).glob("*/secrets-registration.json"):
        try:
            ledger = private_json(ledger_path)
        except DeliveryError:
            continue
        if ledger.get("status") == "succeeded" and ledger.get("environment_id") == request["environment_id"]:
            matches.append(ledger)
    if len(matches) != 1:
        raise DeliveryError("APPLICATION_NOT_REGISTERED", "one successful environment registration is required")
    ledger = matches[0]; registration_path = Path(ledger.get("registration_file", ""))
    if (not registration_path.is_absolute() or not isinstance(ledger.get("registration_sha256"), str)
            or hashlib.sha256(registration_path.read_bytes()).hexdigest() != ledger["registration_sha256"]):
        raise DeliveryError("APPLICATION_REGISTRATION_CHANGED", "environment registration digest differs")
    registration = private_json(registration_path)
    cd_path = registration_path.parent / "cd.json"
    try:
        cd_raw = cd_path.read_bytes()
    except OSError as exc:
        raise DeliveryError("APPLICATION_REGISTRATION_CHANGED", "environment CD registration is unavailable") from exc
    cd = private_json(cd_path)
    registered = cd.get("targets", {}).get(request["environment_id"])
    app_target = registered.get("target", {}) if isinstance(registered, dict) else {}
    if (registration.get("status") != "succeeded"
            or registration.get("cd_sha256") != hashlib.sha256(cd_raw).hexdigest()
            or not isinstance(registered, dict)
            or registered.get("app") != request["app"] or app_target.get("id") != request["environment_id"]
            or not isinstance(app_target.get("namespace"), str) or LABEL.fullmatch(app_target["namespace"]) is None):
        raise DeliveryError("APPLICATION_REGISTRATION_CHANGED", "environment CD registration differs")
    return {"app": request["app"], "namespace": app_target["namespace"], "deployment": request["app"],
            "service": request["app"], "container": None, "service_account": "default",
            "secret_store": {"kind": "SecretStore", "name": "railshot-vault"}}


def workload_container(app, deployment):
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    if app["container"] is None:
        if len(containers) != 1 or not isinstance(containers[0].get("name"), str):
            raise DeliveryError("WORKLOAD_CONTAINER_MISMATCH", "registered workload must have one container")
        return containers[0]["name"], 0
    indexes = [index for index, container in enumerate(containers) if container.get("name") == app["container"]]
    if len(indexes) != 1:
        raise DeliveryError("WORKLOAD_CONTAINER_MISMATCH", "registered workload container differs")
    return app["container"], indexes[0]


class Executor:
    def __init__(self, target, app):
        self.target, self.app = target, app
        self.stack = None; self.prefix = None; self.vault_token = None

    def __enter__(self):
        # Resolve only the operator-owned target registry and reuse its SSM/IAP/
        # OpenStack private SSH transport. No request field can select a host.
        root = Path(__file__).resolve().parents[2]
        sys.path[:0] = [str(root / "infrastructure/ansible"), str(root / "gitops")]
        import run as ansible
        import environment as runtime
        registry = private_json(Path(self.target["registry_file"]))
        selected = registry.get("targets", {}).get(self.target.get("environment_id"))
        if not isinstance(selected, dict) or selected.get("purpose") != "runtime":
            raise DeliveryError("ENVIRONMENT_NOT_REGISTERED", "runtime target is absent from the trusted registry")
        request, _ = runtime.registered_node(selected, self.target["environment_id"], "secrets.delivery")
        self.stack = ExitStack()
        forwarded = self.stack.enter_context(ansible.forwarded_port(
            request["inventory"]["control_plane"][0]["ssh"].get("transport_ref"), time.monotonic() + 540))
        host = next(iter(ansible.build_inventory(request, forwarded)["all"]["children"]["k3s_server"]["hosts"].values()))
        self.prefix = ["ssh", *shlex.split(host["ansible_ssh_common_args"]), "-i", host["ansible_ssh_private_key_file"],
                       "-p", str(host["ansible_port"]), "-o", "ConnectTimeout=15", host["ansible_user"] + "@" + host["ansible_host"]]
        return self

    def __exit__(self, *exc):
        if self.stack is not None:
            self.stack.close()

    def kube(self, *args, document=None):
        remote = shlex.join(["sudo", "-n", "k3s", "kubectl", "--request-timeout=15s", *args])
        argv = [*self.prefix, remote]
        try:
            result = subprocess.run(argv, input=None if document is None else json.dumps(document), text=True,
                                    capture_output=True, timeout=30, env={"PATH": os.environ.get("PATH", "")})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError("KUBERNETES_UNAVAILABLE", "registered Kubernetes transport failed", unknown=document is not None) from exc
        if result.returncode:
            raise DeliveryError("KUBERNETES_REJECTED", "Kubernetes rejected the registered operation", unknown=document is not None)
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise DeliveryError("KUBERNETES_RESPONSE_INVALID", "Kubernetes returned invalid JSON") from exc

    def kube_raw(self, path):
        remote = shlex.join(["sudo", "-n", "k3s", "kubectl", "--request-timeout=15s", "get", "--raw", path])
        argv = [*self.prefix, remote]
        try:
            result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                                    env={"PATH": os.environ.get("PATH", "")})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError("SERVICE_CHECK_FAILED", "in-cluster service check did not complete") from exc
        if result.returncode:
            raise DeliveryError("SERVICE_CHECK_FAILED", "in-cluster service did not return a successful response")
        return True

    def _vault_exec(self, token, args, payload=None, *, json_output=False, sensitive=True):
        vault = self.target["vault"]
        script = ('IFS= read -r token; export VAULT_TOKEN="$token"; IFS= read -r body || true; '
                  'if [ -n "$body" ]; then printf %s "$body" | base64 -d | exec vault "$@"; '
                  'else exec vault "$@"; fi')
        encoded = "" if payload is None else base64.b64encode(payload.encode()).decode()
        remote = shlex.join(["sudo", "-n", "k3s", "kubectl", "--request-timeout=15s", "-n", vault["namespace"],
                             "exec", "-i", vault["pod"], "--", "sh", "-ceu", script, "railshot-vault-delivery", *args])
        argv = [*self.prefix, remote]
        try:
            result = subprocess.run(argv, input=token + "\n" + encoded + "\n", text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=30, env={"PATH": os.environ.get("PATH", "")})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError("VAULT_OPERATION_UNKNOWN", "Vault operation did not complete", unknown=sensitive) from exc
        if result.returncode:
            raise DeliveryError("VAULT_OPERATION_REJECTED", "Vault rejected the registered operation", unknown=sensitive)
        if json_output:
            try:
                return json.loads(result.stdout)
            except ValueError as exc:
                raise DeliveryError("VAULT_READBACK_UNKNOWN", "Vault returned invalid readback", unknown=True) from exc
        return result.stdout

    def vault(self, args, payload=None, *, json_output=False, sensitive=True):
        if self.vault_token is None:
            if self.target['vault'].get('custody_config_file'):
                self.vault_token = custody_handoff(self.target, self.login)
                return self._vault_exec(self.vault_token, args, payload=payload, json_output=json_output, sensitive=sensitive)
            credential = private_json(Path(self.target["vault"]["token_file"]))
            if not isinstance(credential, dict) or set(credential) != {"role_id", "secret_id"} or not all(
                    isinstance(credential[key], str) and credential[key] for key in credential):
                raise DeliveryError("VAULT_CREDENTIAL_UNSAFE", "registered Vault AppRole credential is invalid")
            self.vault_token = self.login(credential)
            if not isinstance(self.vault_token, str) or not self.vault_token:
                raise DeliveryError("VAULT_LOGIN_FAILED", "Vault AppRole login did not return a token")
        return self._vault_exec(self.vault_token, args, payload=payload, json_output=json_output, sensitive=sensitive)

    def login(self, credential):
        result = self._vault_exec("", ["write", "-format=json", "auth/approle/login", "-"],
                                  payload=json.dumps(credential, separators=(",", ":")), json_output=True)
        return result.get('auth', {}).get('client_token')

    def issuer_login(self):
        remote = shlex.join(['sudo', '-n', 'k3s', 'kubectl', '--request-timeout=15s', '-n', 'railshot-secrets',
                             'create', 'token', 'vault', '--audience=vault', '--duration=10m'])
        try:
            result = subprocess.run([*self.prefix, remote], text=True, capture_output=True, timeout=30,
                                    env={'PATH': os.environ.get('PATH', '')})
            if result.returncode or not result.stdout.strip():
                raise DeliveryError('ISSUER_IDENTITY_UNAVAILABLE', 'registered issuer identity is unavailable')
            login = self._vault_exec('', ['write', '-format=json', 'auth/kubernetes/login', '-'],
                payload=json.dumps({'role': 'railshot-delivery-issuer', 'jwt': result.stdout.strip()}), json_output=True)
            token = login.get('auth', {}).get('client_token')
            if not isinstance(token, str) or not token:
                raise DeliveryError('ISSUER_LOGIN_FAILED', 'issuer authentication did not return a token')
            return token
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError('ISSUER_IDENTITY_UNAVAILABLE', 'registered issuer identity is unavailable') from exc

    def vault_write(self, path, values):
        mount = self.target["vault"]["mount"]
        payload = json.dumps({"data": values, "options": {"cas": 0}}, separators=(",", ":"))
        try:
            self.vault(["write", f"{mount}/data/{path}", "-"], payload=payload)
            return
        except DeliveryError as exc:
            if exc.code != "VAULT_OPERATION_REJECTED":
                raise
        observed = self.vault(["read", "-format=json", f"{mount}/data/{path}"], json_output=True).get("data", {}).get("data")
        if observed != values:
            raise DeliveryError("REVISION_CONFLICT", "Vault revision already contains different values")


def public_text(path):
    try:
        info = path.lstat(); value = path.read_text()
    except OSError as exc:
        raise DeliveryError("VAULT_CA_UNAVAILABLE", "registered Vault CA is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or not value:
        raise DeliveryError("VAULT_CA_UNSAFE", "registered Vault CA ownership or mode is unsafe")
    return value


def revision_names(request):
    suffix = hashlib.sha256((request["project_id"] + "\0" + request["revision_id"]).encode()).hexdigest()[:20]
    return "railshot-env-" + suffix, "railshot-secret-" + suffix, "railshot-external-" + suffix


def refs(request, app):
    configmap, secret, external = revision_names(request)
    plain = sorted(v["name"] for v in request["variables"] if v["kind"] == "plain")
    protected = sorted(v["name"] for v in request["variables"] if v["kind"] == "secret")
    return {"project_id": request["project_id"], "binding_id": request["binding_id"],
            "revision_id": request["revision_id"], "namespace": app["namespace"],
            "configmap_name": configmap, "secret_name": secret, "external_secret_name": external,
            "plain_names": plain, "secret_names": protected}


def labels(request):
    compact = lambda value: hashlib.sha256(value.encode()).hexdigest()[:32]
    return {"app.kubernetes.io/managed-by": "railshot-secrets", "railshot.io/project": compact(request["project_id"]),
            "railshot.io/binding": compact(request["binding_id"]), "railshot.io/revision": compact(request["revision_id"])}


def prepare(request, target, app, executor):
    reference = refs(request, app); namespace = app["namespace"]
    observed_namespace = executor.kube("get", "namespace", namespace, "-o", "json")
    if observed_namespace.get("metadata", {}).get("name") != namespace:
        raise DeliveryError("NAMESPACE_MISMATCH", "registered application namespace differs")
    plain = {v["name"]: v["value"] for v in request["variables"] if v["kind"] == "plain"}
    protected = {v["name"]: v["value"] for v in request["variables"] if v["kind"] == "secret"}
    configure_access(request, target, app, executor)
    expected_labels = labels(request)
    for kind, name in (("configmap", reference["configmap_name"]),
                       ("externalsecret", reference["external_secret_name"])):
        if kind == "externalsecret" and not protected:
            continue
        existing = executor.kube("-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json")
        if existing is not None and any(existing.get("metadata", {}).get("labels", {}).get(key) != value
                                        for key, value in expected_labels.items()):
            raise DeliveryError("RESOURCE_OWNERSHIP_CONFLICT", f"existing {kind} is not owned by this revision")
    if protected:
        path = "projects/" + quote(request["project_id"], safe="") + "/revisions/" + quote(request["revision_id"], safe="")
        executor.vault_write(path, protected)
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": reference["configmap_name"],
                 "namespace": namespace, "labels": expected_labels}, "immutable": True, "data": plain}
    executor.kube("-n", namespace, "apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json", document=configmap)
    if protected:
        store = app["secret_store"]
        external = {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
                    "metadata": {"name": reference["external_secret_name"], "namespace": namespace, "labels": labels(request)},
                    "spec": {"refreshInterval": "1h", "secretStoreRef": store,
                             "target": {"name": reference["secret_name"], "creationPolicy": "Owner",
                                        "immutable": True, "template": {"metadata": {"labels": labels(request)}}},
                             "data": [{"secretKey": name, "remoteRef": {"key": "projects/" + request["project_id"] + "/revisions/" + request["revision_id"], "property": name}}
                                      for name in reference["secret_names"]]}}
        executor.kube("-n", namespace, "apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json", document=external)
    verify_resources(request, app, executor, reference)
    return reference


def configure_access(request, target, app, executor):
    """Bind this namespace/service account to only this project's Vault path."""
    namespace = app["namespace"]; vault = target["vault"]
    suffix = hashlib.sha256((request["project_id"] + "\0" + request["application_id"]).encode()).hexdigest()[:20]
    policy_name, role_name, ca_name = "railshot-project-" + suffix, "railshot-app-" + suffix, "railshot-vault-ca"
    policy = 'path "{}/data/projects/{}/*" {{ capabilities = ["read"] }}\n'.format(vault["mount"], request["project_id"])
    executor.vault(["write", "sys/policies/acl/" + policy_name, "-"],
                   payload=json.dumps({"policy": policy}, separators=(",", ":")))
    role = {"bound_service_account_names": [app["service_account"]], "bound_service_account_namespaces": [namespace],
            "audience": "vault", "token_policies": [policy_name], "token_ttl": "10m"}
    executor.vault(["write", "auth/" + vault["auth_mount"] + "/role/" + role_name, "-"],
                   payload=json.dumps(role, separators=(",", ":")))
    owner = {"app.kubernetes.io/managed-by": "railshot-secrets", "railshot.io/project": labels(request)["railshot.io/project"]}
    ca = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": ca_name, "namespace": namespace, "labels": owner},
          "immutable": True, "data": {"ca.crt": public_text(Path(vault["ca_file"]))}}
    store = {"apiVersion": "external-secrets.io/v1", "kind": app["secret_store"]["kind"],
             "metadata": {"name": app["secret_store"]["name"], "namespace": namespace, "labels": owner},
             "spec": {"provider": {"vault": {"server": "https://vault.railshot-secrets.svc:8200",
                      "path": vault["mount"], "version": "v2",
                      "caProvider": {"type": "ConfigMap", "name": ca_name, "key": "ca.crt"},
                      "auth": {"kubernetes": {"mountPath": vault["auth_mount"], "role": role_name,
                               "serviceAccountRef": {"name": app["service_account"], "audiences": ["vault"]}}}}}}}
    for kind, name, document in (("configmap", ca_name, ca), ("secretstore", app["secret_store"]["name"], store)):
        existing = executor.kube("-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json")
        if existing is not None and any(existing.get("metadata", {}).get("labels", {}).get(key) != value for key, value in owner.items()):
            raise DeliveryError("RESOURCE_OWNERSHIP_CONFLICT", f"existing {kind} is not owned by this project")
        executor.kube("-n", namespace, "apply", "--server-side", "--field-manager=railshot-secrets", "-f", "-", "-o", "json",
                      document=document)
    deadline = time.monotonic() + 120
    while True:
        observed = executor.kube("-n", namespace, "get", "secretstore", app["secret_store"]["name"], "-o", "json")
        conditions = {row.get("type"): row.get("status") for row in observed.get("status", {}).get("conditions", [])}
        if conditions.get("Ready") == "True":
            break
        if time.monotonic() >= deadline:
            raise DeliveryError("SECRET_STORE_NOT_READY", "project SecretStore is not ready")
        time.sleep(2)


def verify_resources(request, app, executor, reference):
    namespace = app["namespace"]
    cm = executor.kube("-n", namespace, "get", "configmap", reference["configmap_name"], "-o", "json")
    expected_revision = labels(request)["railshot.io/revision"]
    expected_plain = {v["name"]: v["value"] for v in request["variables"] if v["kind"] == "plain"}
    expected_labels = labels(request)
    if (cm.get("immutable") is not True or cm.get("data", {}) != expected_plain
            or any(cm.get("metadata", {}).get("labels", {}).get(key) != value for key, value in expected_labels.items())):
        raise DeliveryError("CONFIGMAP_READBACK_FAILED", "immutable ConfigMap revision differs")
    if reference["secret_names"]:
        deadline = time.monotonic() + 120
        while True:
            external = executor.kube("-n", namespace, "get", "externalsecret", reference["external_secret_name"], "-o", "json")
            ready = {row.get("type"): row.get("status") for row in external.get("status", {}).get("conditions", [])}
            if ready.get("Ready") == "True":
                break
            if time.monotonic() >= deadline:
                raise DeliveryError("SECRET_NOT_SYNCHRONIZED", "ExternalSecret has not synchronized")
            time.sleep(2)
        expected_key = "projects/" + request["project_id"] + "/revisions/" + request["revision_id"]
        external_data = external.get("spec", {}).get("data", [])
        external_data_ok = (isinstance(external_data, list) and len(external_data) == len(reference["secret_names"])
            and all(isinstance(row, dict) and row.get("secretKey") == name
                    and isinstance(row.get("remoteRef"), dict) and row["remoteRef"].get("key") == expected_key
                    and row["remoteRef"].get("property") == name
                    for row, name in zip(external_data, reference["secret_names"])))
        external_metadata = external.get("metadata", {})
        external_target = external.get("spec", {}).get("target", {})
        template_labels = external_target.get("template", {}).get("metadata", {}).get("labels", {})
        if (external_target.get("name") != reference["secret_name"] or external_target.get("creationPolicy") != "Owner"
                or external_target.get("immutable") is not True or not external_data_ok
                or any(template_labels.get(key) != value for key, value in expected_labels.items())
                or any(external_metadata.get("labels", {}).get(key) != value for key, value in expected_labels.items())
                or not isinstance(external_metadata.get("uid"), str) or not external_metadata["uid"]):
            raise DeliveryError("EXTERNAL_SECRET_READBACK_FAILED", "ExternalSecret revision or ownership differs")
        secret = executor.kube("-n", namespace, "get", "secret", reference["secret_name"], "-o", "json")
        expected_secret = {v["name"]: base64.b64encode(v["value"].encode()).decode()
                           for v in request["variables"] if v["kind"] == "secret"}
        owners = secret.get("metadata", {}).get("ownerReferences", [])
        owner_matches = [row for row in owners if row.get("apiVersion") == "external-secrets.io/v1"
                         and row.get("kind") == "ExternalSecret" and row.get("name") == reference["external_secret_name"]
                         and row.get("uid") == external_metadata["uid"] and row.get("controller") is True]
        if (secret.get("immutable") is not True or secret.get("data", {}) != expected_secret or len(owner_matches) != 1
                or any(secret.get("metadata", {}).get("labels", {}).get(key) != value for key, value in expected_labels.items())):
            raise DeliveryError("SECRET_READBACK_FAILED", "Secret revision differs")


def apply_workload(request, app, executor, reference):
    namespace = app["namespace"]
    env_from = [{"configMapRef": {"name": reference["configmap_name"]}}]
    if reference["secret_names"]:
        env_from.append({"secretRef": {"name": reference["secret_name"]}})
    deployment = executor.kube("-n", namespace, "get", "deployment", app["deployment"], "-o", "json")
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    _, index = workload_container(app, deployment)
    configured = set(reference["plain_names"]) | set(reference["secret_names"])
    env = [entry for entry in containers[index].get("env", []) if entry.get("name") not in configured]
    annotations = deployment["spec"]["template"].get("metadata", {}).get("annotations", {})
    annotations["railshot.io/configuration-revision"] = request["revision_id"]
    patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
             {"op": "add", "path": "/spec/template/metadata/annotations", "value": annotations},
             {"op": "add", "path": f"/spec/template/spec/containers/{index}/env", "value": env},
             {"op": "add", "path": f"/spec/template/spec/containers/{index}/envFrom", "value": env_from}]
    executor.kube("-n", namespace, "patch", "deployment", app["deployment"], "--type=json", "-p", json.dumps(patch), "-o", "json")


def verify_workload(request, app, executor, reference):
    deadline = time.monotonic() + 180
    while True:
        deployment = executor.kube("-n", app["namespace"], "get", "deployment", app["deployment"], "-o", "json")
        status = deployment.get("status", {}); desired = deployment.get("spec", {}).get("replicas", 1)
        ready = (status.get("observedGeneration", 0) >= deployment.get("metadata", {}).get("generation", 1)
                 and status.get("replicas", 0) == desired and status.get("updatedReplicas", 0) == desired
                 and status.get("readyReplicas", 0) == desired and status.get("availableReplicas", 0) == desired
                 and status.get("unavailableReplicas", 0) == 0)
        if ready or time.monotonic() >= deadline:
            break
        time.sleep(2)
    template = deployment.get("spec", {}).get("template", {})
    if template.get("metadata", {}).get("annotations", {}).get("railshot.io/configuration-revision") != request["revision_id"]:
        raise DeliveryError("WORKLOAD_REVISION_MISMATCH", "workload does not pin the requested revision")
    _, index = workload_container(app, deployment)
    container = template.get("spec", {}).get("containers", [])[index]
    expected = [{"configMapRef": {"name": reference["configmap_name"]}}] + ([{"secretRef": {"name": reference["secret_name"]}}] if reference["secret_names"] else [])
    if container.get("envFrom") != expected:
        raise DeliveryError("WORKLOAD_REFERENCE_MISMATCH", "workload configuration references differ")
    configured = set(reference["plain_names"]) | set(reference["secret_names"])
    if any(entry.get("name") in configured for entry in container.get("env", [])):
        raise DeliveryError("WORKLOAD_ENV_PRECEDENCE_CONFLICT", "explicit workload environment overrides a configuration reference")
    if not ready:
        raise DeliveryError("WORKLOAD_NOT_READY", "workload revision is not available")
    endpoints = executor.kube("-n", app["namespace"], "get", "endpoints", app["service"], "-o", "json")
    if not any(subset.get("addresses") for subset in endpoints.get("subsets", [])):
        raise DeliveryError("SERVICE_NOT_READY", "service has no ready endpoints")
    service = executor.kube("-n", app["namespace"], "get", "service", app["service"], "-o", "json")
    probe = container.get("readinessProbe", {}).get("httpGet", {})
    path, port = probe.get("path"), probe.get("port")
    ports = service.get("spec", {}).get("ports", [])
    selected = next((row for row in ports if row.get("targetPort") == port or row.get("name") == port or row.get("port") == port), None)
    if not isinstance(path, str) or not path.startswith("/") or selected is None:
        raise DeliveryError("SERVICE_CHECK_UNAVAILABLE", "registered HTTP readiness endpoint is unavailable")
    proxy_path = "/api/v1/namespaces/{}/services/http:{}:{}/proxy{}".format(
        app["namespace"], app["service"], selected["port"], path)
    executor.kube_raw(proxy_path)


def deliver(config, request, executor_factory=Executor):
    request = validate_request(request); target, app = select_target(config, request)
    context = executor_factory(target, app)
    manager = context if hasattr(context, "__enter__") else _PlainContext(context)
    with manager as executor:
        reference = refs(request, app)
        if request["phase"] == "prepare":
            reference = prepare(request, target, app, executor)
            checks = {"synchronized": True, "workload_ready": False, "service_ready": False}
        else:
            if request["phase"] == "apply":
                reference = prepare(request, target, app, executor)
                apply_workload(request, app, executor, reference)
            else:
                verify_resources(request, app, executor, reference)
            verify_workload(request, app, executor, reference)
            checks = {"synchronized": True, "workload_ready": True, "service_ready": True}
    return {"status": "succeeded", "operation_id": request["operation_id"], "project_id": request["project_id"],
            "binding_id": request["binding_id"], "revision_id": request["revision_id"],
            "observed_revision_id": request["revision_id"], "configuration": reference, "checks": checks}


class _PlainContext:
    def __init__(self, value): self.value = value
    def __enter__(self): return self.value
    def __exit__(self, *_): return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--rotate-credentials", metavar="GENERATION", help="Operator-only bounded AppRole rotation or expired-credential recovery")
    parser.add_argument("--retire-previous", action="store_true", help="Explicitly retire prior AppRole secret only after successful replacement")
    args = parser.parse_args(argv)
    operation = None
    try:
        request = private_json(args.request); operation = request.get("operation_id") if isinstance(request, dict) else None
        config = private_json(args.config)
        if args.rotate_credentials:
            request = validate_request(request); target, app = select_target(config, request)
            with Executor(target, app) as executor:
                result = rotate_delivery_credential(target, executor, args.rotate_credentials, retire_previous=args.retire_previous)
        else:
            if args.retire_previous: raise DeliveryError('INVALID_REQUEST', 'retirement requires explicit credential rotation')
            result = deliver(config, request)
        code = 0
    except DeliveryError as exc:
        result = {"status": "unknown" if exc.unknown else "blocked", "operation_id": operation,
                  "error": {"code": exc.code}}
        code = 4 if exc.unknown else 3
    except (OSError, ValueError, KeyError, TypeError):
        result = {"status": "blocked", "operation_id": operation, "error": {"code": "DELIVERY_INPUT_INVALID"}}
        code = 3
    print(json.dumps(result, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
