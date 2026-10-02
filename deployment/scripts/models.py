"""Provider-neutral, normalized deployment contracts."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RuntimeSpec:
    k3s_version: str = 'v1.34.11+k3s1'
    cilium_version: str = '1.20.2'
    cilium_cli_version: str = 'v0.20.1'
    timeout_seconds: int = 180
    node_ip: Optional[str] = None


@dataclass(frozen=True)
class WorkloadSpec:
    image: str
    namespace: str
    replicas: int = 1
    container_port: int = 80
    health_path: str = '/'
    sample_content: bool = False


@dataclass(frozen=True)
class ExposureSpec:
    node_port: int = 30080
    verification_url: Optional[str] = None


@dataclass(frozen=True)
class DeploymentSpec:
    environment_id: str
    workload: WorkloadSpec
    runtime: RuntimeSpec
    exposure: ExposureSpec


@dataclass(frozen=True)
class RequestContext:
    provider: str
    node_host: str
    ssh_user: Optional[str] = None


@dataclass(frozen=True)
class AdaptedRequest:
    spec: DeploymentSpec
    context: RequestContext


@dataclass
class DeploymentResult:
    status: str = 'failed'
    cluster_status: str = 'unknown'
    cilium_status: str = 'unknown'
    workload_status: str = 'unknown'
    endpoint: Optional[str] = None
    endpoint_scope: Optional[str] = None
    error: Optional[dict] = None
    states: list = field(default_factory=list)
