#!/usr/bin/env python3
"""Root-only local certificate enrollment. The central CA key never leaves here."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from recovery_service import (audit, canonical, exclusive_write, fsync_directory,
                              identifier, load_registry, private_directory, private_read)


@contextmanager
def registry_lock(registry):
    private_directory(Path(registry).parent)
    fd = os.open(str(registry) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_registry(registry, clients):
    # Caller holds the companion file lock. Readers see old or new complete JSON.
    fd, temporary = tempfile.mkstemp(prefix=".registry-", dir=Path(registry).parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical({"version": 1, "clients": clients}) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, registry)
        fsync_directory(Path(registry).parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def issue(environment, role, ca_file, ca_key_file, registry, output_dir, days=30):
    identifier(environment)
    if role not in ("runtime", "manager", "both") or type(days) is not int or not 1 <= days <= 90:
        raise ValueError("invalid certificate request")
    private_directory(output_dir)
    # Public CA is also an integrity-critical root-owned enrollment input.
    ca_bytes = private_read(ca_file)
    ca = x509.load_pem_x509_certificate(ca_bytes)
    key = serialization.load_pem_private_key(private_read(ca_key_file), password=None)
    if key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != ca.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise ValueError("CA key mismatch")
    if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("not a CA")
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=days)
    if ca.not_valid_before_utc > now or ca.not_valid_after_utc < expiry:
        raise ValueError("CA lifetime insufficient")
    roles = ("runtime", "manager") if role == "both" else (role,)
    outputs = {}
    certificates = {}
    for current_role in roles:
        client_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "railshot-" + current_role)]))
                .issuer_name(ca.subject).public_key(client_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expiry)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(client_key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                                            data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = Path(output_dir) / (current_role + ".crt"), Path(output_dir) / (current_role + ".key")
        outputs[cert_path] = cert.public_bytes(serialization.Encoding.PEM)
        outputs[key_path] = client_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
        certificates[current_role] = {"fingerprint": fingerprint, "cert_file": str(cert_path), "key_file": str(key_path)}
    outputs[Path(output_dir) / "ca.crt"] = ca_bytes
    # Refuse accidental regeneration in the same destination. Prior credentials
    # stay valid until the operator explicitly revokes their fingerprint.
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise ValueError("destination already contains credentials")
    written = []
    try:
        for path, data in outputs.items():
            exclusive_write(path, data)
            written.append(path)
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    # Keep files if registry update has an uncertain outcome. A caller can
    # reconcile by fingerprint; automatic deletion could destroy issued keys.
    with registry_lock(registry):
        clients = load_registry(registry) if Path(registry).exists() else {}
        for current_role, certificate in certificates.items():
            fingerprint = certificate["fingerprint"]
            if fingerprint in clients:
                raise ValueError("certificate collision")
            clients[fingerprint] = {"environment_id": environment, "role": current_role}
        write_registry(registry, clients)
    audit("enroll", "succeeded", environment_id=environment)
    return {"environment_id": environment, "clients": certificates, "ca_file": str(Path(output_dir) / "ca.crt")}


def revoke(environment, role, fingerprint, registry):
    identifier(environment)
    if role not in ("runtime", "manager") or not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError("invalid revocation")
    with registry_lock(registry):
        clients = load_registry(registry)
        identity = clients.get(fingerprint)
        if identity is not None:
            if identity != {"environment_id": environment, "role": role}:
                raise ValueError("revocation identity mismatch")
            del clients[fingerprint]
            write_registry(registry, clients)
    audit("revoke", "succeeded", environment_id=environment, client_fingerprint=fingerprint)
    return {"revoked": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("issue")
    enroll.add_argument("--environment-id", required=True)
    enroll.add_argument("--role", choices=("runtime", "manager", "both"), default="both")
    for name in ("ca-file", "ca-key-file", "registry", "output-dir"):
        enroll.add_argument("--" + name, required=True)
    enroll.add_argument("--days", type=int, default=30)
    remove = commands.add_parser("revoke")
    for name in ("environment-id", "role", "fingerprint", "registry"):
        remove.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise ValueError("root required")
        os.umask(0o077)
        if args.command == "issue":
            result = issue(args.environment_id, args.role, args.ca_file, args.ca_key_file, args.registry, args.output_dir, args.days)
        else:
            result = revoke(args.environment_id, args.role, args.fingerprint, args.registry)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception:
        audit("enrollment", "failed")
        print('{"error":"CUSTODY_ENROLLMENT_FAILED"}', file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
