#!/usr/bin/env python3
"""Discover Cloudera CWO Airflow (Helm) on hub kubeconfigs."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_KCFG_DIR = Path("/etc/prometheus/kubeconfig")
NS_PATTERN = re.compile(r".*-cwo-.*", re.I)


@dataclass(frozen=True)
class AirflowPodTarget:
    cluster: str
    kubeconfig: str
    namespace: str
    release: str
    component: str
    pod: str
    container: str
    api_service: str

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


def list_kubeconfigs(kcfg_dir: Path) -> list[tuple[str, Path]]:
    if not kcfg_dir.is_dir():
        return []
    return [(p.stem, p) for p in sorted(kcfg_dir.glob("*.yaml"))]


def discover_in_cluster(cluster: str, kubeconfig: Path) -> list[AirflowPodTarget]:
    proc = _kubectl(
        kubeconfig,
        "get",
        "pods",
        "-A",
        "-l",
        "tier=airflow,app=airflow",
        "-o",
        "json",
    )
    if proc.returncode != 0:
        return []

    try:
        items = json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return []

    releases: set[tuple[str, str]] = set()
    targets: list[AirflowPodTarget] = []

    for item in items:
        namespace = item["metadata"]["namespace"]
        if not NS_PATTERN.match(namespace):
            continue
        labels = item["metadata"].get("labels") or {}
        release = labels.get("release") or labels.get("helm.sh/chart", "airflow")
        component = labels.get("component") or "unknown"
        pod = item["metadata"]["name"]
        releases.add((namespace, release))

        containers = [c["name"] for c in item["spec"].get("containers") or []]
        main_container = component if component in containers else (containers[0] if containers else component)

        api_svc = f"{release}-airflow-api-server"
        targets.append(
            AirflowPodTarget(
                cluster=cluster,
                kubeconfig=str(kubeconfig),
                namespace=namespace,
                release=release,
                component=component,
                pod=pod,
                container=main_container,
                api_service=api_svc,
            )
        )

    return targets


def discover_all(kcfg_dir: Path = DEFAULT_KCFG_DIR) -> list[AirflowPodTarget]:
    all_targets: list[AirflowPodTarget] = []
    for cluster, kc_path in list_kubeconfigs(kcfg_dir):
        all_targets.extend(discover_in_cluster(cluster, kc_path))
    return sorted(all_targets, key=lambda t: (t.cluster, t.namespace, t.release, t.component, t.pod))


def main() -> None:
    kcfg_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_KCFG_DIR
    targets = discover_all(kcfg_dir)
    print(json.dumps([t.to_json() for t in targets], indent=2))


if __name__ == "__main__":
    main()
