#!/usr/bin/env python3
"""Private mTLS custody service. No HTTP route ever returns secret material."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import socketserver
import sqlite3
import ssl
import stat
import sys
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_BODY = 65536
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
KINDS = {"vault-initialization", "vault-delivery-approle"}


class Rejected(Exception):
    def __init__(self, status=422, code="INVALID_INPUT"):
        self.status, self.code = status, code


def audit(action, status, **metadata):
    # Caller supplies fixed action/status and validated identifiers only.
    allowed = {"environment_id", "receipt_id", "client_fingerprint", "request_id"}
    record = {"event": "recovery_custody", "action": action, "status": status}
    record.update({key: value for key, value in metadata.items() if key in allowed and value is not None})
    print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Rejected()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(Rejected()))
    except (ValueError, UnicodeError, RecursionError):
        raise Rejected(400) from None


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise Rejected()
    return value


def private_read(path, owner=0):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077:
            raise ValueError("unsafe private file")
        data = stream.read(MAX_BODY + 1)
        if len(data) > MAX_BODY:
            raise ValueError("private file too large")
        return data


def private_directory(path, owner=0):
    info = Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077:
        raise ValueError("unsafe private directory")


def exclusive_write(path, data):
    # Operator selects an already-private directory; never overwrite or follow links.
    private_directory(Path(path).parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(Path(path).parent)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_payload(value, environment):
    if not isinstance(value, dict) or set(value) not in (
            {"version", "environment_id", "kind", "material"},
            {"version", "environment_id", "kind", "material", "operation_id"}):
        raise Rejected()
    if type(value["version"]) is not int or value["version"] != 1 or not isinstance(value["kind"], str) or value["kind"] not in KINDS:
        raise Rejected()
    identifier(value["environment_id"])
    if value["environment_id"] != environment:
        raise Rejected(403, "FORBIDDEN")
    operation = identifier(value.get("operation_id", "initial"))
    material = value["material"]
    if not isinstance(material, dict):
        raise Rejected()
    if value["kind"] == "vault-delivery-approle":
        if set(material) != {"role_id", "secret_id"}:
            raise Rejected()
        for item in material.values():
            if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,1024}", item):
                raise Rejected()
    else:
        required = {"root_token", "recovery_keys_b64"}
        optional = {"recovery_keys_hex", "recovery_keys_shares", "recovery_keys_threshold",
                    "unseal_keys_b64", "unseal_keys_hex", "unseal_shares", "unseal_threshold"}
        if not required <= set(material) or set(material) - required - optional:
            raise Rejected()
        token = material["root_token"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,4096}", token):
            raise Rejected()
        shares = material["recovery_keys_b64"]
        if not isinstance(shares, list) or len(shares) != 5:
            raise Rejected()
        try:
            decoded = [base64.b64decode(s, validate=True) for s in shares if isinstance(s, str)]
        except ValueError:
            raise Rejected() from None
        if len(decoded) != 5 or len(set(decoded)) != 5 or any(len(s) != 33 for s in decoded):
            raise Rejected()
        if "recovery_keys_hex" in material and material["recovery_keys_hex"] != [s.hex() for s in decoded]:
            raise Rejected()
        for key, expected in {"recovery_keys_shares": 5, "recovery_keys_threshold": 3}.items():
            if key in material and (type(material[key]) is not int or material[key] != expected):
                raise Rejected()
        unseal_counts = (material.get("unseal_shares", 0), material.get("unseal_threshold", 0))
        if any(type(count) is not int for count in unseal_counts) or unseal_counts not in ((0, 0), (1, 1)):
            raise Rejected()
        # Vault CLI newMachineInit reports 1/1 for stored (auto-unseal) keys,
        # although both unseal key arrays are empty. Keep legacy 0/0 clients.
        if unseal_counts == (1, 1) and (
                material.get("unseal_keys_b64") != [] or material.get("unseal_keys_hex") != [] or
                material.get("recovery_keys_shares") != 5 or material.get("recovery_keys_threshold") != 3):
            raise Rejected()
        for key in ("unseal_keys_b64", "unseal_keys_hex"):
            if key in material and material[key] != []:
                raise Rejected()
    return dict(value, operation_id=operation)


def load_keyring(path):
    value = strict_json(private_read(path))
    if not isinstance(value, dict) or set(value) != {"version", "active_key_id", "keys"} or value["version"] != 1:
        raise ValueError("invalid keyring")
    if not isinstance(value["keys"], dict) or not value["keys"]:
        raise ValueError("invalid keyring")
    keys = {}
    for key_id, encoded in value["keys"].items():
        identifier(key_id)
        key = base64.b64decode(encoded, validate=True)
        if len(key) != 32:
            raise ValueError("invalid custody key")
        keys[key_id] = key
    if value["active_key_id"] not in keys:
        raise ValueError("missing active custody key")
    return value["active_key_id"], keys


def load_registry(path):
    value = strict_json(private_read(path))
    if not isinstance(value, dict) or set(value) != {"version", "clients"} or value["version"] != 1 or not isinstance(value["clients"], dict):
        raise ValueError("invalid registry")
    for fingerprint, client in value["clients"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("invalid certificate fingerprint")
        if not isinstance(client, dict) or set(client) != {"environment_id", "role"} or client["role"] not in ("runtime", "manager"):
            raise ValueError("invalid client identity")
        identifier(client["environment_id"])
    return value["clients"]


class Custody:
    """SQLite FULL transactions persist ciphertext and receipt together before ACK.

    Process lock serializes local threads; SQLite also serializes independent
    processes (rotation/backup). No plaintext digest is stored or exposed.
    """
    def __init__(self, directory, active_key_id, keys, owner=0):
        private_directory(directory, owner)
        self.directory = Path(directory)
        self.path = self.directory / "custody.sqlite3"
        self.active_key_id, self.keys = active_key_id, keys
        self.lock = threading.RLock()
        # O_EXCL avoids symlink races on creation; directory is owner-only.
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            self._check_file(owner)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS escrows (receipt_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, kind TEXT NOT NULL, operation_id TEXT NOT NULL, key_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, UNIQUE(environment_id,kind,operation_id))")
        fsync_directory(directory)

    def _check_file(self, owner):
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077:
            raise ValueError("unsafe database")

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("PRAGMA journal_mode=DELETE")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def aad(row):
        return canonical({"version": 1, **{key: row[key] for key in ("receipt_id", "environment_id", "kind", "operation_id", "key_id")}})

    def decrypt(self, row):
        raw = AESGCM(self.keys[row["key_id"]]).decrypt(row["nonce"], row["ciphertext"], self.aad(row))
        payload = validate_payload(strict_json(raw), row["environment_id"])
        if payload["kind"] != row["kind"] or payload["operation_id"] != row["operation_id"]:
            raise ValueError("record binding failed")
        return payload

    def store(self, payload):
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM escrows WHERE environment_id=? AND kind=? AND operation_id=?", (payload["environment_id"], payload["kind"], payload["operation_id"])).fetchone()
            if row:
                if canonical(self.decrypt(row)) != canonical(payload):
                    raise Rejected(409, "IDEMPOTENCY_CONFLICT")
                receipt, created = row["receipt_id"], False
            else:
                row = {"receipt_id": str(uuid.uuid4()), "environment_id": payload["environment_id"], "kind": payload["kind"], "operation_id": payload["operation_id"], "key_id": self.active_key_id}
                nonce = os.urandom(12)
                ciphertext = AESGCM(self.keys[self.active_key_id]).encrypt(nonce, canonical(payload), self.aad(row))
                db.execute("INSERT INTO escrows VALUES (?,?,?,?,?,?,?)", (*row.values(), nonce, ciphertext))
                receipt, created = row["receipt_id"], True
        # Returning outside the transaction is critical: failed commit is not ACKed.
        return {"stored": True, "receipt_id": receipt}, created

    def lookup(self, environment, receipt=None, kind=None, operation=None):
        with self.lock, self.connection() as db:
            if receipt is not None:
                row = db.execute("SELECT * FROM escrows WHERE environment_id=? AND receipt_id=?", (environment, receipt)).fetchone()
            else:
                row = db.execute("SELECT * FROM escrows WHERE environment_id=? AND kind=? AND operation_id=?", (environment, kind, operation)).fetchone()
            if row is None:
                raise Rejected(404, "NOT_FOUND")
            self.decrypt(row)  # A receipt never attests unreadable/tampered custody.
            return {"stored": True, "receipt_id": row["receipt_id"]}

    def selected(self, environment, receipt):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT * FROM escrows WHERE environment_id=? AND receipt_id=?", (environment, receipt)).fetchone()
            if row is None:
                raise Rejected(404, "NOT_FOUND")
            return self.decrypt(row)

    def wrapped_export(self, environment, receipt, purpose, role, certificate):
        expected = {"initialization-resume": ("runtime", "vault-initialization"),
                    "delivery-handoff": ("manager", "vault-delivery-approle")}
        if purpose not in expected or role != expected[purpose][0]:
            raise Rejected(403, "FORBIDDEN")
        payload = self.selected(environment, receipt)
        if payload["kind"] != expected[purpose][1]:
            raise Rejected(403, "FORBIDDEN")
        public_key = x509.load_der_x509_certificate(certificate).public_key()
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
            raise Rejected(422, "INVALID_RECIPIENT_KEY")
        metadata = {"version": 1, "environment_id": environment, "kind": payload["kind"],
                    "operation_id": payload["operation_id"], "receipt_id": receipt, "purpose": purpose,
                    "recipient_fingerprint": hashlib.sha256(certificate).hexdigest()}
        key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
        exported = ({"root_token": payload["material"]["root_token"]}
                    if purpose == "initialization-resume" else payload["material"])
        ciphertext = AESGCM(key).encrypt(nonce, canonical(exported), canonical(metadata))
        wrapped = public_key.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        return {**metadata, "wrapped_key": base64.b64encode(wrapped).decode(),
                "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode()}

    def readiness(self):
        # Bound the hot-path work. Full authentication happens at startup/verify.
        with self.lock, self.connection() as db:
            row = db.execute("SELECT * FROM escrows ORDER BY rowid DESC LIMIT 1").fetchone()
            if row is not None:
                self.decrypt(row)

    def verify(self):
        with self.lock, self.connection() as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("database integrity failed")
            count = 0
            for row in db.execute("SELECT * FROM escrows"):
                self.decrypt(row)
                count += 1
            return count

    def rotate(self):
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM escrows").fetchall()
            for record in rows:
                payload = self.decrypt(record)
                row = dict(record, key_id=self.active_key_id)
                nonce = os.urandom(12)
                ciphertext = AESGCM(self.keys[self.active_key_id]).encrypt(nonce, canonical(payload), self.aad(row))
                db.execute("UPDATE escrows SET key_id=?,nonce=?,ciphertext=? WHERE receipt_id=?", (self.active_key_id, nonce, ciphertext, row["receipt_id"]))
        return len(rows)

    def backup(self, destination):
        private_directory(Path(destination).parent)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            with self.lock, self.connection() as source:
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                finally:
                    target.close()
            fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            fsync_directory(Path(destination).parent)
        except BaseException:
            Path(destination).unlink(missing_ok=True)
            raise


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Custody"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass  # Never log paths, certificate identities, request bodies or exceptions.

    def send_error(self, code, message=None, explain=None):
        self.request_id = getattr(self, "request_id", str(uuid.uuid4()))
        self.error(405 if code == 501 else code, "METHOD_NOT_ALLOWED" if code == 501 else "INVALID_INPUT")

    def reply(self, status, body, location=None):
        encoded = canonical(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self.request_id)
        self.send_header("Connection", "close")
        if location:
            self.send_header("Location", location)
        if status == 405:
            self.send_header("Allow", "GET, POST")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True
        audit(getattr(self, "action", "request"), status,
              environment_id=getattr(self, "environment", None),
              client_fingerprint=getattr(self, "fingerprint", None),
              receipt_id=body.get("receipt_id"), request_id=self.request_id)

    def error(self, status, code):
        self.reply(status, {"error": {"code": code, "message": "Request could not be completed.", "request_id": self.request_id, "retryable": False, "outcome_unknown": status == 503}})

    def dispatch(self):
        self.request_id = str(uuid.uuid4())
        try:
            certificate = self.connection.getpeercert(binary_form=True)
            self.fingerprint = hashlib.sha256(certificate or b"").hexdigest()
            if self.server.registry_path is not None:
                try:
                    registry = load_registry(self.server.registry_path)
                except Exception:
                    raise Rejected(503, "CUSTODY_UNAVAILABLE") from None
            else:
                registry = self.server.registry
            client = registry.get(self.fingerprint)
            if client is None:
                raise Rejected(403, "FORBIDDEN")
            environment, role = client["environment_id"], client["role"]
            self.environment = environment
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise Rejected()
            path = parsed.path
            if self.command == "GET":
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                    raise Rejected()
                if path == "/api/v1/healths" and not parsed.query:
                    self.action = "health"
                    self.server.custody.readiness()
                    self.reply(200, {"status": "ready"})
                    return
                self.action = "lookup"
                if path == "/api/v1/escrows":
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                    if set(query) != {"kind", "operation_id"} or any(len(v) != 1 for v in query.values()) or query["kind"][0] not in KINDS:
                        raise Rejected()
                    result = self.server.custody.lookup(environment, kind=query["kind"][0], operation=identifier(query["operation_id"][0]))
                elif path.startswith("/api/v1/escrows/") and not parsed.query:
                    result = self.server.custody.lookup(environment, receipt=identifier(path.removeprefix("/api/v1/escrows/")))
                else:
                    raise Rejected(404, "NOT_FOUND")
                self.reply(200, result)
                return
            if self.command != "POST":
                raise Rejected(405, "METHOD_NOT_ALLOWED")
            export_path = re.fullmatch(r"/api/v1/escrows/([A-Za-z0-9_.-]{1,128})/exports", path)
            if path != "/api/v1/escrows" and export_path is None:
                raise Rejected(404, "NOT_FOUND")
            if parsed.query or self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise Rejected()
            if self.headers.get_content_type() != "application/json":
                raise Rejected(415, "UNSUPPORTED_MEDIA_TYPE")
            size = int(self.headers["Content-Length"])
            if size <= 0:
                raise Rejected(400)
            if size > MAX_BODY:
                raise Rejected(413, "PAYLOAD_TOO_LARGE")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise Rejected(400)
            value = strict_json(raw)
            if export_path:
                self.action = "wrappedexport"
                if not isinstance(value, dict) or set(value) != {"purpose"} or not isinstance(value["purpose"], str):
                    raise Rejected()
                result = self.server.custody.wrapped_export(environment, export_path[1], value["purpose"], role, certificate)
                self.reply(200, result)
                return
            self.action = "store"
            payload = validate_payload(value, environment)
            if role != "runtime" and not (role == "manager" and payload["kind"] == "vault-delivery-approle"):
                raise Rejected(403, "FORBIDDEN")
            result, created = self.server.custody.store(payload)
            self.reply(201 if created else 200, result, "/api/v1/escrows/" + result["receipt_id"])
        except Rejected as exc:
            self.error(exc.status, exc.code)
        except (ValueError, TypeError):
            self.error(422, "INVALID_INPUT")
        except Exception:
            self.error(503, "CUSTODY_UNAVAILABLE")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = dispatch


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, custody, registry, tls_context, max_workers=16, registry_path=None):
        self.custody, self.registry, self.tls_context = custody, registry, tls_context
        self.registry_path = registry_path
        self.slots = threading.BoundedSemaphore(max_workers)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        wrapped = None
        try:
            request.settimeout(10)
            wrapped = self.tls_context.wrap_socket(request, server_side=True)
            self.finish_request(wrapped, address)
        except Exception:
            pass
        finally:
            self.shutdown_request(wrapped or request)
            self.slots.release()


def tls_context(cert, key, ca):
    private_read(key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert, key)
    context.load_verify_locations(cafile=ca)
    return context


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--keyring", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--listen", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9443)
    for name in ("registry", "cert", "key", "ca"):
        serve.add_argument("--" + name, required=True)
    export = commands.add_parser("export")
    export.add_argument("--environment-id", required=True)
    export.add_argument("--receipt-id", required=True)
    export.add_argument("--material", choices=("root-token", "recovery-share", "delivery-approle"), required=True)
    export.add_argument("--share-index", type=int)
    export.add_argument("--output", required=True)
    commands.add_parser("verify")
    commands.add_parser("ready")
    commands.add_parser("rotate")
    backup = commands.add_parser("backup")
    backup.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise ValueError("root required")
        os.umask(0o077)
        active, keys = load_keyring(args.keyring)
        if args.command != "serve" and not (Path(args.data_dir) / "custody.sqlite3").is_file():
            raise ValueError("missing custody database")
        # Hold for process lifetime; rotation must not race a server with an old keyring.
        if args.command in ("serve", "rotate"):
            private_directory(args.data_dir)
            lock_fd = os.open(Path(args.data_dir) / "service.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        custody = Custody(args.data_dir, active, keys)
        if args.command == "serve":
            custody.verify()
            with Server((args.listen, args.port), custody, load_registry(args.registry), tls_context(args.cert, args.key, args.ca), registry_path=args.registry) as server:
                server.serve_forever()
        elif args.command == "ready":
            custody.readiness()
            print('{"status":"ready"}')
        elif args.command == "verify":
            print(json.dumps({"verified_records": custody.verify()}))
        elif args.command == "rotate":
            print(json.dumps({"rotated_records": custody.rotate()}))
        elif args.command == "backup":
            custody.verify()
            custody.backup(args.output)
            print('{"backup_written":true}')
        else:
            payload = custody.selected(identifier(args.environment_id), identifier(args.receipt_id))
            material = payload["material"]
            if args.material == "delivery-approle" and payload["kind"] == "vault-delivery-approle" and args.share_index is None:
                output = canonical(material) + b"\n"
            elif args.material == "root-token" and payload["kind"] == "vault-initialization" and args.share_index is None:
                output = material["root_token"].encode() + b"\n"
            elif args.material == "recovery-share" and payload["kind"] == "vault-initialization" and args.share_index in range(1, 6):
                output = material["recovery_keys_b64"][args.share_index - 1].encode() + b"\n"
            else:
                raise ValueError("invalid material selection")
            exclusive_write(args.output, output)
            print('{"export_written":true}')
        audit("local" + args.command, "succeeded", environment_id=getattr(args, "environment_id", None), receipt_id=getattr(args, "receipt_id", None))
        return 0
    except Exception:
        audit("local" + args.command, "failed")
        print('{"error":"CUSTODY_OPERATION_FAILED"}', file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
