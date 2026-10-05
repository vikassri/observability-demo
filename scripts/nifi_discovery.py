#!/usr/bin/env python3
"""Discover CDF NiFi deployments across hub kubeconfig files."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


NS_LABEL_SELECTOR = "app.kubernetes.io/name=nifi,app.kubernetes.io/instance=cloudera-data-flow"
POD_LABEL_SELECTOR = "app.kubernetes.io/part-of=nifi,app.kubernetes.io/component=nifi"
PROMETHEUS_SVC = "prometheus"
PROMETHEUS_PORT = 19090
DEFAULT_KCFG_DIR = Path("/etc/prometheus/kubeconfig")
BASE_LOCAL_PORT = 19090


@dataclass(frozen=True)
class NiFiDeployment:
    cluster: str
    kubeconfig: str
    namespace: str
    has_prometheus: bool
    pods: tuple[str, ...]
    local_port: int | None = None

    def to_json(self) -> dict:
        return asdict(self)


def _kubectl(kubeconfig: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", f"--kubeconfig={kubeconfig}", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def list_kubeconfigs(kcfg_dir: Path) -> list[tuple[str, Path]]:
    if not kcfg_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(kcfg_dir.glob("*.yaml")):
        out.append((path.stem, path))
    return out


def discover_in_cluster(cluster: str, kubeconfig: Path) -> list[NiFiDeployment]:
    proc = _kubectl(
        kubeconfig,
        "get",
        "ns",
        "-l",
        NS_LABEL_SELECTOR,
        "-o",
        "json",
    )
    if proc.returncode != 0:
        return []

    try:
        ns_items = json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return []

    deployments: list[NiFiDeployment] = []
    for item in ns_items:
        namespace = item["metadata"]["name"]
        prom = _kubectl(kubeconfig, "get", "svc", PROMETHEUS_SVC, "-n", namespace)
        has_prometheus = prom.returncode == 0

        pod_proc = _kubectl(
            kubeconfig,
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            POD_LABEL_SELECTOR,
            "-o",
            "jsonpath={.items[*].metadata.name}",
        )
        pods = tuple(p for p in (pod_proc.stdout or "").split() if p)

        deployments.append(
            NiFiDeployment(
                cluster=cluster,
                kubeconfig=str(kubeconfig),
                namespace=namespace,
                has_prometheus=has_prometheus,
                pods=pods,
            )
        )
    return deployments


def assign_ports(deployments: list[NiFiDeployment]) -> list[NiFiDeployment]:
    """Stable local port per metrics-ready deployment."""
    metrics_ready = sorted(
        [d for d in deployments if d.has_prometheus],
        key=lambda d: (d.cluster, d.namespace),
    )
    port_by_key: dict[tuple[str, str], int] = {}
    for idx, dep in enumerate(metrics_ready):
        port_by_key[(dep.cluster, dep.namespace)] = BASE_LOCAL_PORT + idx

    result: list[NiFiDeployment] = []
    for dep in deployments:
        port = port_by_key.get((dep.cluster, dep.namespace))
        result.append(
            NiFiDeployment(
                cluster=dep.cluster,
                kubeconfig=dep.kubeconfig,
                namespace=dep.namespace,
                has_prometheus=dep.has_prometheus,
                pods=dep.pods,
                local_port=port,
            )
        )
    return result


def discover_all(kcfg_dir: Path = DEFAULT_KCFG_DIR) -> list[NiFiDeployment]:
    all_deps: list[NiFiDeployment] = []
    for cluster, kc_path in list_kubeconfigs(kcfg_dir):
        all_deps.extend(discover_in_cluster(cluster, kc_path))
    return assign_ports(all_deps)


def prometheus_file_sd(deployments: list[NiFiDeployment]) -> list[dict]:
    groups: list[dict] = []
    for dep in deployments:
        if not dep.has_prometheus or dep.local_port is None:
            continue
        groups.append(
            {
                "targets": [f"127.0.0.1:{dep.local_port}"],
                "labels": {
                    "cluster": dep.cluster,
                    "nifi_namespace": dep.namespace,
                },
            }
        )
    return groups


def main() -> None:
    kcfg_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_KCFG_DIR
    deps = discover_all(kcfg_dir)
    print(json.dumps([d.to_json() for d in deps], indent=2))


if __name__ == "__main__":
    main()
