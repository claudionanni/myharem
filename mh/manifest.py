"""Persisted registry of deployments.

State was previously derived only from directory names + `my.cnf` scraping. The
manifest records each deployment's topology, node membership, ports, and roles
in `<basedir>/manifest.json` so cluster-level lifecycle and automation callers
have an authoritative source of truth. Best-effort: a missing/corrupt manifest
degrades to "no known deployments" rather than failing.
"""

import json
import os
from pathlib import Path

from . import config
from .model import DeploymentResult


def _manifest_path() -> Path:
    return config.get_basedir() / "manifest.json"


def _load() -> dict:
    path = _manifest_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def record(result: DeploymentResult) -> None:
    data = _load()
    data[str(result.cluster_id)] = result.to_dict()
    _save(data)


def get(cluster_id) -> dict | None:
    return _load().get(str(cluster_id))


def all_deployments() -> dict:
    return _load()


def remove(cluster_id) -> None:
    data = _load()
    if str(cluster_id) in data:
        del data[str(cluster_id)]
        _save(data)
