#!/usr/bin/env python3
"""Run one Jasmin agent role through an agent API (Claude Agent SDK or Codex SDK).

usage:
  run_agent.py ROLE --provider claude|codex --workspace DIR --run DIR --task FILE
  run_agent.py --self-test

Agents only get read access. Files come back in the JSON output ("files": [{path, content}]);
this script validates every path and writes it. Output and metadata go to RUN/<role>.json.
Needs: pip packages pyyaml, jsonschema, claude-agent-sdk (claude provider); `openai-codex==0.159.3` (codex provider).
"""
import argparse
import asyncio
import copy
from fnmatch import fnmatchcase
from functools import lru_cache
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
import uuid
from itertools import chain
from pathlib import Path, PurePosixPath

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM))
from observability import OperationError, event_record
from runner.runtime_boundary import effective_auth_route, private_directory


def validate_read_roots(roots, deny):
    """Directory searches must not bypass file denies. Reject unsafe read trees before SDK launch."""
    for root in roots:
        root = Path(root)
        if not root.exists():
            raise OperationError("SDK_CONFIG_INVALID", component="runner", phase="config", retry_policy="after_configuration")
        for path in chain((root,), root.rglob("*")):
            rel = path.relative_to(root).as_posix() if path != root else root.name
            if path.is_symlink() or not path_ok(rel, ["**"], deny) or len(Path(rel).parts) > 32:
                raise OperationError("SDK_POLICY_DENIED", component="runner", phase="policy")


def lifecycle(run, role, provider, model):
    """A private, content-free SDK receipt; loop checkpoint owns scheduling and recovery."""
    run_id = os.environ.get("RAILSHOT_RUN_ID") or str(uuid.uuid4())
    attempt_id = os.environ.get("RAILSHOT_ATTEMPT_ID") or run_id + ":1"
    state = {"schema_version": 1, "run_id": run_id, "attempt_id": attempt_id,
             "role": role, "provider": provider, "model": model, "status": "pending",
             "sdk_status": "not_started", "session_id": None, "thread_id": None, "turn_id": None,
             "conversation_resume": "unsupported",
             "resume_reason": "ephemeral_thread" if provider == "codex" else "policy_rebinding_not_implemented"}
    events, snapshot = run / f"{role}-events.jsonl", run / f"{role}-session.json"
    if snapshot.exists() or snapshot.is_symlink():
        raise OperationError("SDK_POLICY_DENIED", component="runner", phase="policy", retry_policy="after_reconcile")
    try:
        fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)  # Exclusive creation also rejects a concurrent/replayed invocation.
    except FileExistsError as exc:
        raise OperationError("SDK_POLICY_DENIED", component="runner", phase="policy", retry_policy="after_reconcile", cause=exc) from exc
    except OSError as exc:
        raise OperationError("OBSERVATION_WRITE_FAILED", component="runner", phase="observation", cause=exc) from exc
    seq = 0

    def emit(kind, *, error=None, **fields):
        nonlocal seq
        allowed = {"status", "sdk_status", "session_id", "thread_id", "turn_id", "sdk_failure"}
        if set(fields) - allowed:
            raise OperationError("INTERNAL_ERROR", component="runner", phase="observation")
        state.update(fields)
        seq += 1
        attributes = {key: state[key] for key in ("role", "provider", "model", "status", "sdk_status",
                      "session_id", "thread_id", "turn_id", "conversation_resume", "resume_reason")}
        if state.get("sdk_failure"): attributes["sdk_failure"] = state["sdk_failure"]
        sdk_finished = kind == "session.finished"
        outcome = error.outcome if error else "PASS" if state["status"] == "completed" or sdk_finished else "RUNNING"
        phase = error.phase if error else "invoke" if kind.startswith(("session.", "turn.")) else "agent"
        common = event_record(kind, component="runner", phase=phase, outcome=outcome,
                              run_id=run_id, attempt_id=attempt_id, error=error, attributes=attributes)
        # Existing receipt consumers retain top-level IDs/status; canonical event fields come from the common module.
        event = {**state, **common, "event": kind, "sequence": seq, "timestamp": time.time()}
        temporary = None
        try:
            with events.open("a") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
                stream.flush(); os.fsync(stream.fileno())
            fd, temporary = tempfile.mkstemp(prefix=f".{role}-session-", dir=run)
            with os.fdopen(fd, "w") as stream:
                json.dump(event, stream, sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, snapshot)
            fd = os.open(run, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            effect = "unknown" if state["sdk_status"] == "running" else "none" if state["sdk_status"] == "not_started" else "completed"
            raise OperationError("OBSERVATION_WRITE_FAILED", component="runner", phase="observation",
                                 outcome="UNKNOWN" if effect == "unknown" else "BLOCKED",
                                 retry_policy="after_reconcile", side_effect=effect, cause=exc) from exc
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
    return state, emit


def load_yaml(path):
    import yaml
    return yaml.safe_load(Path(path).read_text())


def writable_rules(spec, scope="packaging"):
    if scope not in {"packaging", "source"}:
        raise ValueError("unsupported repair scope")
    if isinstance(spec, list):
        return spec, []
    paths = load_yaml(PLATFORM / spec)
    return (paths["writable"] + (paths.get("source_writable", []) if scope == "source" else []),
            paths["protected"] + paths.get("source_protected", []))


def path_ok(rel, allow, deny):
    """True if rel is a clean relative path matching an allow glob and no deny glob."""
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not rel or rel.startswith("~"):
        return False
    def matches(pattern):
        parts = PurePosixPath(pattern).parts
        @lru_cache(None)
        def match(i, j):
            if j == len(parts):
                return i == len(p.parts)
            if parts[j] == "**":
                return match(i, j + 1) or (i < len(p.parts) and match(i + 1, j))
            return i < len(p.parts) and fnmatchcase(p.parts[i], parts[j]) and match(i + 1, j + 1)
        return match(0, 0)
    hit = lambda globs: any(matches(g) for g in globs)
    return hit(allow) and not hit(deny)


def instructions(profile, role_cfg):
    text = (PLATFORM / profile["instructions_prefix"]).read_text() + "\n\n" + (PLATFORM / role_cfg["instructions"]).read_text()
    text += ("\n\n## How to return files\nYou cannot edit files. Return the full content of every file you create or "
             "change in the `files` array of your JSON output, with paths relative to the workspace root. "
             "Files you do not list stay unchanged.\n")
    return text


def with_files(schema, allow):
    """Add the files array to the role schema (draft-07)."""
    s = copy.deepcopy(schema)
    s["properties"]["files"] = {
        "type": "array", "maxItems": 8,
        "items": {"type": "object", "additionalProperties": False, "required": ["path", "content"],
                  "properties": {"path": {"type": "string"}, "content": {"type": "string", "maxLength": 20000}}}}
    if not allow:
        s["properties"]["files"]["maxItems"] = 0
    return s


def strict_variant(schema):
    """Codex structured output wants every property required; optional ones become nullable."""
    s = copy.deepcopy(schema)
    s.pop("$schema", None)
    s.pop("allOf", None)

    def walk(node):
        if isinstance(node, dict):
            # The canonical schema is still validated after generation. This wire
            # projection omits constraints outside Structured Outputs' supported subset.
            for keyword in ("$schema", "contains", "uniqueItems", "allOf"):
                node.pop(keyword, None)
            if "const" in node:
                node["enum"] = [node.pop("const")]
            if "enum" in node and "type" not in node:
                values = node["enum"]
                if all(type(value) is str for value in values): node["type"] = "string"
                elif all(type(value) is int for value in values): node["type"] = "integer"
            if node.get("type") == "object" and "properties" in node:
                req = set(node.get("required", []))
                for k, v in node["properties"].items():
                    walk(v)
                    if k not in req:
                        node["properties"][k] = {"anyOf": [v, {"type": "null"}]}
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    return s


def drop_nulls(obj):
    if isinstance(obj, dict):
        return {k: drop_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [drop_nulls(v) for v in obj]
    return obj


async def run_claude(cfg, system, task, schema, workspace, read_roots, read_deny, emit=None):
    from importlib.metadata import version
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, SystemMessage, query

    if version("claude-agent-sdk") != "0.2.158":
        raise OperationError("SDK_CONFIG_INVALID", component="runner", phase="config", retry_policy="after_configuration")
    validate_read_roots(read_roots, read_deny)
    emit = emit or (lambda *args, **kwargs: None)

    async def guard(inp, tool_use_id, ctx):
        args = inp.get("tool_input", {})
        pattern = args.get("pattern", "")
        target = args.get("file_path") or args.get("path") or (pattern if pattern.startswith(("/", "~", "..")) else str(workspace))
        lexical = Path(os.path.expanduser(target) if Path(os.path.expanduser(target)).is_absolute() else workspace / target)
        if any(p.is_symlink() for p in (lexical, *lexical.parents)):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "symlink read denied"}}
        full = lexical.resolve()
        inside = any(full == r or r in full.parents for r in read_roots)
        rel = full.relative_to(workspace).as_posix() if workspace in full.parents else full.name
        if not inside or (rel and not path_ok(rel, ["**"], read_deny)):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": f"read denied: {target}"}}
        try:
            # Grep/Glob on a directory otherwise see denied descendants, unlike Read(file).
            scan = full
            while not scan.exists() and scan != scan.parent:
                scan = scan.parent
            validate_read_roots([scan], read_deny)
        except OperationError:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "unsafe read tree"}}
        return {}

    async def prompt():
        yield {"type": "user", "message": {"role": "user", "content": task}}

    opts = ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code", "append": system},
        cwd=str(workspace), add_dirs=[str(r) for r in read_roots if r != workspace],
        tools=["Read", "Glob", "Grep"], allowed_tools=["Read", "Glob", "Grep"],
        setting_sources=[],              # never load CLAUDE.md, settings or hooks from disk
        strict_mcp_config=True, mcp_servers={},
        hooks={"PreToolUse": [HookMatcher(matcher="Read|Glob|Grep", hooks=[guard])]},
        max_turns=cfg["max_turns"], max_budget_usd=cfg["max_budget_usd"], model=cfg["model"],
        output_format={"type": "json_schema", "schema": schema},
    )
    result = None
    emit("session.starting", sdk_status="running")
    async for msg in query(prompt=prompt(), options=opts):
        if isinstance(msg, SystemMessage) and msg.subtype == "init" and msg.data.get("session_id"):
            emit("session.started", session_id=msg.data["session_id"])
        if isinstance(msg, ResultMessage):
            result = msg
            failure = OperationError("SDK_EXECUTION_FAILED", component="runner", phase="invoke",
                                     outcome="FAIL", side_effect="completed") if msg.is_error else None
            emit("session.finished", session_id=msg.session_id,
                 sdk_status="failed" if msg.is_error else "completed", error=failure)
    if result is None:
        raise OperationError("SDK_OUTCOME_UNKNOWN", component="runner", phase="invoke",
                             outcome="UNKNOWN", retry_policy="after_reconcile", side_effect="unknown")
    if result.is_error:
        raise failure
    if result.structured_output is None:
        raise OperationError("SDK_OUTPUT_INVALID", component="runner", phase="output", outcome="FAIL", side_effect="completed")
    meta = {"turns": result.num_turns, "cost_usd": result.total_cost_usd, "duration_ms": result.duration_ms,
            "denials": len(result.permission_denials or []), "runtime": "claude-agent-sdk", "version": "0.2.158",
            "session_id": result.session_id, "thread_id": None, "turn_id": None,
            "status": "completed", "conversation_resume": "unsupported"}
    return result.structured_output, meta


def codex_permissions(workspace, run, credential_home, read_deny):
    """Restrict command reads as well as writes; legacy read-only permits broad reads."""
    from codex_cli_bin import bundled_codex_path, bundled_path_dir
    profile = "permissions.railshot_read"
    values = ['default_permissions="railshot_read"', f'{profile}.network.enabled=false']
    rules = {":root": "deny", ":minimal": "read"}
    for path in (workspace, PLATFORM / "contract", PLATFORM / "schemas", run):
        rules[str(Path(path).resolve())] = "read"
        for pattern in read_deny:
            rules[str(Path(path).resolve() / pattern)] = "deny"
    rules["glob_scan_max_depth"] = 32
    rules[str(bundled_codex_path().resolve())] = "read"
    rules[str((bundled_path_dir() / "rg").resolve())] = "read"
    for name in ("cat", "ls", "sed"):
        executable = shutil.which(name)
        if executable:
            rules[str(Path(executable).resolve())] = "read"
    rules[str(Path(credential_home).resolve())] = "deny"
    # CLI dotted overrides split keys literally; path quoting belongs in a TOML value.
    values.append(profile + '.filesystem={' + ','.join(json.dumps(path) + '=' + json.dumps(access) for path, access in rules.items()) + '}')
    return tuple(values)


def codex_failure_diagnostic(error):
    """Keep typed codes and bounded safe explanation; never emit provider free text."""
    message = getattr(error, "message", "") or ""
    raw_info = getattr(error, "codex_error_info", None)
    info = raw_info.model_dump(mode="json") if raw_info is not None else None
    # The SDK union's field names/codes are trusted types; string values other
    # than enum codes are not copied. Raw message remains represented by a hash.
    codes = []
    def collect(value):
        if isinstance(value, str) and value.replace("_", "").isalnum() and len(value) <= 64:
            codes.append(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in ("http_status_code", "httpStatusCode") and type(child) is int:
                    codes.append("http_" + str(child))
                elif child is None and key.replace("_", "").isalnum(): codes.append(key)
                elif key in ("root", "type", "code"): collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    collect(info)
    category = "provider_terminal_failure"
    safe_message = "The SDK reported a terminal failure; raw provider text is not exposed."
    if "invalid" in message.lower() and "schema" in message.lower():
        category = "invalid_output_schema"
        safe_message = "The provider rejected the structured output schema."
    return {"category": category, "codes": sorted(set(codes))[:8], "message": safe_message,
            "message_sha256": hashlib.sha256(message.encode()).hexdigest()}


def collect_codex_turn(turn):
    """Use public typed stream notifications: run() raises before returning failed turns."""
    from openai_codex import TurnResult
    from openai_codex.models import ItemCompletedNotification, ThreadTokenUsageUpdatedNotification, TurnCompletedNotification
    from openai_codex.generated.v2_all import AgentMessageThreadItem, MessagePhase
    completed = None; items = []; usage = None
    stream = turn.stream()
    try:
        for event in stream:
            payload = event.payload
            if isinstance(payload, ItemCompletedNotification) and payload.turn_id == turn.id:
                items.append(payload.item)
            elif isinstance(payload, ThreadTokenUsageUpdatedNotification) and payload.turn_id == turn.id:
                usage = payload.token_usage
            elif isinstance(payload, TurnCompletedNotification) and payload.turn.id == turn.id:
                completed = payload.turn
    finally:
        stream.close()
    if completed is None:
        raise OperationError("SDK_OUTCOME_UNKNOWN", component="runner", phase="invoke",
                             outcome="UNKNOWN", side_effect="unknown", retry_policy="after_reconcile")
    final = fallback = None
    for wrapped in reversed(items):
        item = wrapped.root if hasattr(wrapped, "root") else wrapped
        if isinstance(item, AgentMessageThreadItem):
            if item.phase == MessagePhase.final_answer:
                final = item.text; break
            if item.phase is None and fallback is None: fallback = item.text
    return TurnResult(id=completed.id, status=completed.status, error=completed.error,
                      started_at=completed.started_at, completed_at=completed.completed_at,
                      duration_ms=completed.duration_ms, final_response=final if final is not None else fallback,
                      items=items, usage=usage)


def run_codex(cfg, system, task, schema, workspace, run, read_deny=None, emit=None):
    from importlib.metadata import version
    from openai_codex import ApprovalMode, Codex, CodexConfig

    if version("openai-codex") != "0.159.3":
        raise OperationError("SDK_CONFIG_INVALID", component="runner", phase="config", retry_policy="after_configuration")
    if read_deny is None:
        read_deny = load_yaml(PLATFORM / "runner/profiles.yaml")["read_deny"]
    validate_read_roots([workspace, PLATFORM / "contract", PLATFORM / "schemas", run], read_deny)
    emit = emit or (lambda *args, **kwargs: None)
    # The operator provisions an auth-only home on the dedicated agent VM.
    # Never select an account or copy credentials from an uploaded repository.
    route = effective_auth_route('codex')
    mode, home = route['mode'], route['credential_home']
    if mode not in {"subscription", "api-key"}:
        raise OperationError("SDK_CONFIG_INVALID", component="runner", phase="config", retry_policy="after_configuration")
    key = os.environ.get("CODEX_API_KEY")
    if mode == "api-key" and not key:
        raise OperationError("SDK_CONFIG_INVALID", component="runner", phase="config", retry_policy="after_configuration")
    if mode == "subscription" and (key or os.environ.get("OPENAI_API_KEY") or not home):
        raise OperationError("SDK_CONFIG_INVALID", component="runner", phase="config", retry_policy="after_configuration")
    if home and not Path(home).is_absolute():
        raise OperationError("SDK_CONFIG_INVALID", component="runner", phase="config", retry_policy="after_configuration")
    t = time.time()
    with tempfile.TemporaryDirectory(prefix="railshot-codex-auth-") as temporary:
        auth_home = temporary if mode == "api-key" else home
        config = CodexConfig(cwd=str(workspace),
            env={"CODEX_HOME": auth_home,
                 "OPENAI_API_KEY": "", "CODEX_API_KEY": ""},
            config_overrides=('project_doc_max_bytes=0', 'web_search="disabled"',
                              'shell_environment_policy.inherit="none"',
                              *codex_permissions(workspace, run, auth_home, read_deny)))
        with Codex(config) as codex:
            if mode == "api-key":
                codex.login_api_key(key)
            emit("session.starting", sdk_status="running")
            thread = codex.thread_start(
                cwd=str(workspace),
                approval_mode=ApprovalMode.deny_all, ephemeral=True,
                developer_instructions=system, model=cfg.get("model"),
            )
            emit("session.started", session_id=thread.id, thread_id=thread.id)
            turn = thread.turn(task, effort=cfg["reasoning_effort"], output_schema=strict_variant(schema))
            emit("turn.started", turn_id=turn.id)
            result = collect_codex_turn(turn)
            status = getattr(result.status, "value", result.status)
            failure = OperationError("SDK_EXECUTION_FAILED", component="runner", phase="invoke",
                                     outcome="FAIL", side_effect="completed") if status != "completed" else None
            diagnostic = codex_failure_diagnostic(result.error) if failure else None
            emit("session.finished", turn_id=result.id, sdk_status=status, error=failure,
                 **({"sdk_failure": diagnostic} if diagnostic else {}))
            if failure:
                raise failure
            try:
                output = drop_nulls(json.loads(result.final_response))
            except (TypeError, ValueError) as exc:
                raise OperationError("SDK_OUTPUT_INVALID", component="runner", phase="output",
                                     outcome="FAIL", side_effect="completed", cause=exc) from exc
            meta = {"runtime": "openai-codex", "version": "0.159.3", "auth_mode": mode, "requested_model": cfg.get("model"),
                    "requested_reasoning_effort": cfg["reasoning_effort"],
                    "permission_profile": "railshot_read",
                    "session_id": thread.id, "thread_id": thread.id, "turn_id": result.id, "status": status,
                    "conversation_resume": "unsupported",
                    "duration_ms": int((time.time() - t) * 1000)}
            # Subscription usage is not a USD price; absent price remains unknown.
            try:
                (run / "codex-meta.json").write_text(json.dumps(meta))
            except OSError as exc:
                raise OperationError("OBSERVATION_WRITE_FAILED", component="runner", phase="observation",
                                     side_effect="completed", retry_policy="after_reconcile", cause=exc) from exc
            return output, meta


def apply_files(workspace, files, allow, protect, *, applied=None):
    """Validate the whole proposal before writing any file, including symlink parents."""
    workspace = workspace.resolve()
    if len(files) > 8 or sum(len(f["content"].encode()) for f in files) > 20000:
        raise ValueError("proposal exceeds file/byte limit")
    targets = []
    for f in files:
        rel = f["path"]
        dest = workspace / rel
        if (not path_ok(rel, allow, protect) or dest.resolve().is_relative_to(workspace) is False
                or any(p.is_symlink() for p in (dest, *dest.parents) if p.is_relative_to(workspace))):
            raise ValueError(f"rejected patch path: {rel}")
        if rel in [p for p, _ in targets]:
            raise ValueError(f"duplicate patch path: {rel}")
        targets.append((rel, f["content"]))
    applied = [] if applied is None else applied
    if not targets:
        return applied
    # Stage every byte first. Each replace is atomic, but the proposal is not a
    # filesystem transaction: preserve the exact completed subset on any failure.
    with tempfile.TemporaryDirectory(prefix='.railshot-patch-', dir=workspace) as staging:
        for index, (rel, content) in enumerate(targets):
            dest, staged = workspace / rel, Path(staging) / str(index)
            with staged.open('x') as stream:
                os.fchmod(stream.fileno(), dest.stat().st_mode & 0o777 if dest.exists() else 0o644)
                stream.write(content)
                stream.flush(); os.fsync(stream.fileno())
        for index, (rel, _) in enumerate(targets):
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(Path(staging) / str(index), dest)
            applied.append(rel)
            fd = os.open(dest.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return applied


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role", nargs="?", choices=["adapter", "fixer"])
    ap.add_argument("--provider", choices=["claude", "codex"], default="codex")
    ap.add_argument("--repair-scope", choices=["packaging", "source"], default="packaging")
    ap.add_argument("--workspace")
    ap.add_argument("--run")
    ap.add_argument("--task")
    ap.add_argument("--resume-session-id", help="Reserved: native conversation resume is unsupported")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    try:
        return execute(a)
    except Exception as exc:
        error = exc if isinstance(exc, OperationError) else OperationError(
            "INTERNAL_ERROR", component="runner", phase="agent", cause=exc)
        # When required evidence itself cannot be written, report safely to the parent and stop.
        print(json.dumps(event_record("agent.unknown" if error.outcome == "UNKNOWN" else "agent.blocked",
              component="runner", phase=error.phase, outcome=error.outcome,
              run_id=os.environ.get("RAILSHOT_RUN_ID"), attempt_id=os.environ.get("RAILSHOT_ATTEMPT_ID"),
              error=error, attributes={"role": a.role, "provider": a.provider})))
        return 1


def execute(a):
    try:
        profile = load_yaml(PLATFORM / "runner/profiles.yaml")
        role_cfg = profile["roles"][a.role]
        provider = a.provider
        workspace, run = Path(a.workspace).resolve(), private_directory(a.run)
        allow, protect = writable_rules(role_cfg["writable"], scope=a.repair_scope)
        schema = with_files(json.loads((PLATFORM / role_cfg["schema"]).read_text()), allow)
        system = instructions(profile, role_cfg)
        system += (f"\n\n## Authority for this run\nRepair scope: {a.repair_scope}.\n"
                   f"Writable paths: {json.dumps(allow)}\nProtected paths: {json.dumps(protect)}\n"
                   "These concrete bounds replace packaging-only restrictions when source scope is explicitly selected. "
                   "Never weaken tests, lint/type rules, CI gates or approval policy. Return a proposal only.\n")
        task = Path(a.task).read_text()
    except Exception as exc:
        raise OperationError("SDK_CONFIG_INVALID", component="runner", phase="config",
                             retry_policy="after_configuration", cause=exc) from exc
    read_roots = [workspace, PLATFORM / "contract", PLATFORM / "schemas", run]

    state, emit = lifecycle(run, a.role, provider, profile["providers"][provider].get("model"))
    emit("agent.started", status="running")
    written, rejected, out, meta, error, phase = [], [], {}, {}, None, "config"
    try:
        if a.resume_session_id:
            raise OperationError("SDK_RESUME_UNSUPPORTED", component="runner", phase="config")
        phase = "invoke"
        if provider == "claude":
            # Drop parent session variables; retain only the operator's authentication variable.
            for k in [k for k in os.environ if k.startswith("CLAUDE_CODE_") and k != "CLAUDE_CODE_OAUTH_TOKEN"] + ["CLAUDECODE"]:
                os.environ.pop(k, None)
            out, meta = asyncio.run(run_claude(profile["providers"]["claude"], system, task, schema, workspace,
                                               read_roots, profile["read_deny"], emit))
        else:
            out, meta = run_codex(profile["providers"]["codex"], system, task, schema, workspace, run,
                                 profile["read_deny"], emit)
        import jsonschema
        phase = "output"
        jsonschema.validate(out, schema)
        phase = "patch"
        written = apply_files(workspace, out.get("files", []), allow, protect, applied=written)
    except Exception as exc:
        if isinstance(exc, OperationError):
            error = exc
        elif phase == "patch" and isinstance(exc, OSError):
            error = OperationError("INTERNAL_ERROR", component="runner", phase="patch", outcome="FAIL",
                                   retry_policy="after_reconcile", side_effect="possible", cause=exc)
        elif phase in ("output", "patch"):
            error = OperationError("SDK_OUTPUT_INVALID" if phase == "output" else "SDK_PATCH_REJECTED",
                                   component="runner", phase=phase, outcome="FAIL", side_effect="completed", cause=exc)
        elif state["sdk_status"] == "running":
            error = OperationError("SDK_OUTCOME_UNKNOWN", component="runner", phase="invoke", outcome="UNKNOWN",
                                   retry_policy="after_reconcile", side_effect="unknown", cause=exc)
        elif state["sdk_status"] == "not_started" and isinstance(exc, (ImportError, LookupError, ValueError, OSError)):
            error = OperationError("SDK_CONFIG_INVALID", component="runner", phase="config",
                                   retry_policy="after_configuration", cause=exc)
        else:
            error = OperationError("INTERNAL_ERROR", component="runner", phase=phase, cause=exc,
                                   side_effect="completed" if state["sdk_status"] != "not_started" else "none")
        rejected = [error.code]
    status = "unknown" if error and error.outcome == "UNKNOWN" else "failed" if error else "completed"
    meta.update({key: state[key] for key in ("run_id", "attempt_id", "session_id", "thread_id", "turn_id",
                                           "conversation_resume", "resume_reason")})
    if state.get("sdk_failure"): meta["sdk_failure"] = state["sdk_failure"]
    meta.update(status=status, sdk_status=state["sdk_status"], events_file=f"{a.role}-events.jsonl",
                session_file=f"{a.role}-session.json")
    record = {"role": a.role, "repair_scope": a.repair_scope, "provider": provider, "model": profile["providers"][provider].get("model"),
              "instructions_sha256": hashlib.sha256(system.encode()).hexdigest()[:12],
              "written": written, "rejected": rejected, "meta": meta,
              "output": {k: v for k, v in out.items() if k != "files"} if isinstance(out, dict) else {}}
    if error:
        record["error"] = error.as_dict()
    record_path = run / f"{a.role}.json"
    try:
        with record_path.open("w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, indent=2, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
    except OSError as exc:
        effect = "unknown" if state["sdk_status"] == "running" else "none" if state["sdk_status"] == "not_started" else "completed"
        raise OperationError("OBSERVATION_WRITE_FAILED", component="runner", phase="observation",
                             retry_policy="after_reconcile", side_effect=effect,
                             outcome="UNKNOWN" if effect == "unknown" else "BLOCKED", cause=exc) from exc
    emit("agent." + status, status=status, error=error)
    print(json.dumps({k: record[k] for k in ("role", "provider", "written", "rejected", "meta", "error") if k in record}, ensure_ascii=False))
    return 1 if rejected else 0


def self_test():
    allow, protect = writable_rules("contract/paths.yaml")
    ok = ["Dockerfile", "api.Dockerfile", "backend/Dockerfile", ".dockerignore", ".jasmin/jasmin.yaml"]
    bad = ["../x", "/etc/passwd", "app.py", ".github/workflows/x.yml", "tests/Dockerfile", "AGENTS.md", "~/x", ""]
    assert all(path_ok(p, allow, protect) for p in ok), [p for p in ok if not path_ok(p, allow, protect)]
    assert not any(path_ok(p, allow, protect) for p in bad), [p for p in bad if path_ok(p, allow, protect)]
    s = strict_variant(with_files(json.loads((PLATFORM / "schemas/report.schema.json").read_text()), allow))
    assert set(s["required"]) == set(s["properties"]) and "allOf" not in s
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
