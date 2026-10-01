"""Shared release order and application container execution contract."""
GATE_ORDER = ("L0", "L1", "Q", "L2", "L4", "L3")
APP_UID = 65532


def pod_security(uid=APP_UID):
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid,
            "seccompProfile": {"type": "RuntimeDefault"}}


def container_security():
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


def docker_security():
    return ["--user", f"{APP_UID}:{APP_UID}", "--read-only", "--tmpfs", "/tmp",
            "--cap-drop=ALL", "--security-opt=no-new-privileges"]


def docker_command(image, command=None):
    # Kubernetes command replaces ENTRYPOINT and clears image CMD. Match it exactly.
    return ["--entrypoint", command[0], image, *command[1:]] if command else [image]
