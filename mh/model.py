"""Structured result types returned by the deployment functions.

These are the machine-readable contract that automation callers (e.g. the MSRS
control plane) consume via `--json`, and that the manifest persists.
"""

from dataclasses import asdict, dataclass, field


@dataclass
class NodeInfo:
    """One deployed MariaDB instance."""

    id: str
    role: str          # 'single' | 'master' | 'slave' | 'galera'
    port: int
    socket: str
    datadir: str
    path: str
    wsrep_port: int | None = None
    sst_port: int | None = None


@dataclass
class DeploymentResult:
    """The outcome of a deploy command."""

    topology: str      # 'single' | 'replication' | 'galera'
    cluster_id: str    # base/first instance id — identifies the deployment
    tarball: str
    nodes: list[NodeInfo] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
