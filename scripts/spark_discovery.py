#!/usr/bin/env python3
"""Discover Spark Operator workloads on hub kubeconfigs (CDE / dex-app-*)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SPARK_CRD_GROUP = "spark.apache.org"
DEFAULT_KCFG_DIR = Path("/etc/prometheus/kubeconfig")
NS_PATTERN = re.compile(r"^dex-app-", re.I)
SPARK_CLUSTER_FILTER = re.compile(r"^cde$|^dex$", re.I)


@dataclass(frozen=True)
class SparkPodTarget:
    cluster: str
    kubeconfig: str
    namespace: str
    workspace: str
    role: str
    pod: str
    container: str
    spark_app: str

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SparkApplicationSummary:
    cluster: str
    kubeconfig: str
    namespace: str
    workspace: str
    name: str
    state: str

    def to_json(self) -> dict:
        return asdict(self)


def _kubectl(kubeconfig: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
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
    for p in sorted(kcfg_dir.glob("*.yaml")):
        if SPARK_CLUSTER_FILTER.match(p.stem):
            out.append((p.stem, p))
    return out


def _has_spark_crd(kubeconfig: Path) -> bool:
    proc = _kubectl(kubeconfig, "api-resources", "--api-group", SPARK_CRD_GROUP)
    return proc.returncode == 0 and "sparkapplications" in (proc.stdout or "").lower()


def _workspace_from_ns(namespace: str) -> str:
    return namespace


def discover_applications(cluster: str, kubeconfig: Path) -> list[SparkApplicationSummary]:
    if not _has_spark_crd(kubeconfig):
        return []
    proc = _kubectl(kubeconfig, "get", "sparkapplications", "-A", "-o", "json")
    if proc.returncode != 0:
        return []
    try:
        items = json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return []
    apps: list[SparkApplicationSummary] = []
    for item in items:
        ns = item["metadata"]["namespace"]
        if not NS_PATTERN.match(ns):
            continue
        name = item["metadata"]["name"]
        state = (
            (item.get("status") or {})
            .get("currentState", {})
            .get("currentStateSummary", "Unknown")
        )
        apps.append(
            SparkApplicationSummary(
                cluster=cluster,
                kubeconfig=str(kubeconfig),
                namespace=ns,
                workspace=_workspace_from_ns(ns),
                name=name,
                state=str(state),
            )
        )
    return apps


def discover_log_pods(cluster: str, kubeconfig: Path) -> list[SparkPodTarget]:
    if not _has_spark_crd(kubeconfig):
        return []
    targets: list[SparkPodTarget] = []

    for label_sel, role in (
        ("app.kubernetes.io/name=spark-operator", "operator"),
        ("spark-role=driver", "driver"),
        ("spark-role=executor", "executor"),
    ):
        proc = _kubectl(
            kubeconfig,
            "get",
            "pods",
            "-A",
            "-l",
            label_sel,
            "-o",
            "json",
        )
        if proc.returncode != 0:
            continue
        try:
            items = json.loads(proc.stdout).get("items", [])
        except json.JSONDecodeError:
            continue
        for item in items:
            ns = item["metadata"]["namespace"]
            if not NS_PATTERN.match(ns):
                continue
            pod = item["metadata"]["name"]
            labels = item["metadata"].get("labels") or {}
            spark_app = labels.get("sparkoperator.k8s.io/app-name") or labels.get(
                "spark-app-name", ""
            )
            containers = [c["name"] for c in item["spec"].get("containers") or []]
            container = containers[0] if containers else "spark-kubernetes-operator"
            if role == "operator":
                container = next(
                    (c for c in containers if c in ("spark-operator", "spark-kubernetes-operator")),
                    next((c for c in containers if "operator" in c), containers[0] if containers else "spark-operator"),
                )
            targets.append(
                SparkPodTarget(
                    cluster=cluster,
                    kubeconfig=str(kubeconfig),
                    namespace=ns,
                    workspace=_workspace_from_ns(ns),
                    role=role,
                    pod=pod,
                    container=container,
                    spark_app=spark_app or role,
                )
            )
    return targets


def discover_all(kcfg_dir: Path = DEFAULT_KCFG_DIR) -> tuple[list[SparkPodTarget], list[SparkApplicationSummary]]:
    pods: list[SparkPodTarget] = []
    apps: list[SparkApplicationSummary] = []
    for cluster, kc in list_kubeconfigs(kcfg_dir):
        apps.extend(discover_applications(cluster, kc))
        pods.extend(discover_log_pods(cluster, kc))
    pods = sorted(pods, key=lambda t: (t.cluster, t.namespace, t.role, t.pod))
    apps = sorted(apps, key=lambda a: (a.cluster, a.namespace, a.name))
    return pods, apps


def main() -> None:
    kcfg_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_KCFG_DIR
    pods, apps = discover_all(kcfg_dir)
    print(json.dumps({"pods": [p.to_json() for p in pods], "applications": [a.to_json() for a in apps]}, indent=2))


if __name__ == "__main__":
    main()
