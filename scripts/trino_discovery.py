#!/usr/bin/env python3
"""Discover Cloudera Trino engines across hub kubeconfig files (CLE and similar)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

TRINO_CRD_GROUP = "trino.cloudera.com"
DEFAULT_KCFG_DIR = Path("/etc/prometheus/kubeconfig")
BASE_LOCAL_METRICS_PORT = 19500
NS_PATTERN = re.compile(r".*-trino-.*", re.I)


@dataclass(frozen=True)
class TrinoPodTarget:
    cluster: str
    kubeconfig: str
    namespace: str
    trino_name: str
    trino_ready: bool
    role: str  # coordinator | worker
    pod: str
    container: str
    metrics_port: int
    local_port: int | None = None

    def to_json(self) -> dict:
        return asdict(self)


def _kubectl(kubeconfig: Path, *args: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", f"--kubeconfig={kubeconfig}", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _cluster_has_trino_crd(kubeconfig: Path) -> bool:
    proc = _kubectl(kubeconfig, "api-resources", "--api-group", TRINO_CRD_GROUP)
    return proc.returncode == 0 and "trino" in (proc.stdout or "").lower()


def list_kubeconfigs(kcfg_dir: Path) -> list[tuple[str, Path]]:
    if not kcfg_dir.is_dir():
        return []
    return [(p.stem, p) for p in sorted(kcfg_dir.glob("*.yaml"))]


def _trino_ready(item: dict) -> bool:
    for cond in (item.get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def discover_in_cluster(cluster: str, kubeconfig: Path) -> list[TrinoPodTarget]:
    if not _cluster_has_trino_crd(kubeconfig):
        return []

    proc = _kubectl(kubeconfig, "get", "trino", "-A", "-o", "json")
    if proc.returncode != 0:
        return []

    try:
        items = json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return []

    targets: list[TrinoPodTarget] = []
    for item in items:
        namespace = item["metadata"]["namespace"]
        trino_name = item["metadata"]["name"]
        if not NS_PATTERN.match(namespace):
            continue
        ready = _trino_ready(item)

        coord = _kubectl(
            kubeconfig,
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app=trino-coordinator",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        )
        for pod in (coord.stdout or "").split():
            if not pod:
                continue
            targets.append(
                TrinoPodTarget(
                    cluster=cluster,
                    kubeconfig=str(kubeconfig),
                    namespace=namespace,
                    trino_name=trino_name,
                    trino_ready=ready,
                    role="coordinator",
                    pod=pod,
                    container="trino-coordinator",
                    metrics_port=35000,
                )
            )

        workers = _kubectl(
            kubeconfig,
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app=trino-worker",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        )
        for pod in sorted(p for p in (workers.stdout or "").split() if p):
            targets.append(
                TrinoPodTarget(
                    cluster=cluster,
                    kubeconfig=str(kubeconfig),
                    namespace=namespace,
                    trino_name=trino_name,
                    trino_ready=ready,
                    role="worker",
                    pod=pod,
                    container="trino-worker",
                    metrics_port=35000,
                )
            )

    return targets


def assign_local_ports(targets: list[TrinoPodTarget]) -> list[TrinoPodTarget]:
    ordered = sorted(
        targets,
        key=lambda t: (t.cluster, t.namespace, t.trino_name, t.role, t.pod),
    )
    out: list[TrinoPodTarget] = []
    for idx, t in enumerate(ordered):
        out.append(
            TrinoPodTarget(
                cluster=t.cluster,
                kubeconfig=t.kubeconfig,
                namespace=t.namespace,
                trino_name=t.trino_name,
                trino_ready=t.trino_ready,
                role=t.role,
                pod=t.pod,
                container=t.container,
                metrics_port=t.metrics_port,
                local_port=BASE_LOCAL_METRICS_PORT + idx,
            )
        )
    return out


def discover_all(kcfg_dir: Path = DEFAULT_KCFG_DIR) -> list[TrinoPodTarget]:
    all_targets: list[TrinoPodTarget] = []
    for cluster, kc_path in list_kubeconfigs(kcfg_dir):
        all_targets.extend(discover_in_cluster(cluster, kc_path))
    return assign_local_ports(all_targets)


def prometheus_file_sd(targets: list[TrinoPodTarget]) -> list[dict]:
    groups: list[dict] = []
    for t in targets:
        if t.local_port is None:
            continue
        groups.append(
            {
                "targets": [f"127.0.0.1:{t.local_port}"],
                "labels": {
                    "cluster": t.cluster,
                    "trino_namespace": t.namespace,
                    "trino_instance": t.trino_name,
                    "trino_role": t.role,
                    "trino_pod": t.pod,
                    "trino_ready": str(t.trino_ready).lower(),
                },
            }
        )
    return groups


def main() -> None:
    kcfg_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_KCFG_DIR
    targets = discover_all(kcfg_dir)
    print(json.dumps([t.to_json() for t in targets], indent=2))


if __name__ == "__main__":
    main()
