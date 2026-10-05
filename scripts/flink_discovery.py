#!/usr/bin/env python3
"""Discover Flink operator / SSB workloads on hub kubeconfigs (CSA)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

FLINK_CRD_GROUP = "flink.apache.org"
DEFAULT_KCFG_DIR = Path("/etc/prometheus/kubeconfig")
NS_PATTERN = re.compile(r".*-csa-.*", re.I)
FLINK_CLUSTER_FILTER = re.compile(r"^csa$", re.I)


@dataclass(frozen=True)
class FlinkPodTarget:
    cluster: str
    kubeconfig: str
    namespace: str
    workspace: str
    role: str
    pod: str
    container: str
    flink_app: str

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FlinkDeploymentSummary:
    cluster: str
    kubeconfig: str
    namespace: str
    workspace: str
    name: str
    job_state: str
    lifecycle_state: str

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FlinkSessionJobSummary:
    cluster: str
    kubeconfig: str
    namespace: str
    workspace: str
    name: str
    deployment_name: str
    job_state: str
    lifecycle_state: str

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
    return [(p.stem, p) for p in sorted(kcfg_dir.glob("*.yaml")) if FLINK_CLUSTER_FILTER.match(p.stem)]


def _has_flink_crd(kubeconfig: Path) -> bool:
    proc = _kubectl(kubeconfig, "api-resources", "--api-group", FLINK_CRD_GROUP)
    return proc.returncode == 0 and "flinkdeployments" in (proc.stdout or "").lower()


def discover_deployments(cluster: str, kubeconfig: Path) -> list[FlinkDeploymentSummary]:
    if not _has_flink_crd(kubeconfig):
        return []
    proc = _kubectl(kubeconfig, "get", "flinkdeployments", "-A", "-o", "json")
    if proc.returncode != 0:
        return []
    try:
        items = json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return []
    out: list[FlinkDeploymentSummary] = []
    for item in items:
        ns = item["metadata"]["namespace"]
        if not NS_PATTERN.match(ns):
            continue
        st = item.get("status") or {}
        job = st.get("jobStatus") or {}
        out.append(
            FlinkDeploymentSummary(
                cluster=cluster,
                kubeconfig=str(kubeconfig),
                namespace=ns,
                workspace=ns,
                name=item["metadata"]["name"],
                job_state=str(job.get("state") or "UNKNOWN"),
                lifecycle_state=str(st.get("lifecycleState") or "UNKNOWN"),
            )
        )
    return out


def discover_session_jobs(cluster: str, kubeconfig: Path) -> list[FlinkSessionJobSummary]:
    if not _has_flink_crd(kubeconfig):
        return []
    proc = _kubectl(kubeconfig, "get", "flinksessionjobs", "-A", "-o", "json")
    if proc.returncode != 0:
        return []
    try:
        items = json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return []
    out: list[FlinkSessionJobSummary] = []
    for item in items:
        ns = item["metadata"]["namespace"]
        if not NS_PATTERN.match(ns):
            continue
        st = item.get("status") or {}
        job = st.get("jobStatus") or {}
        out.append(
            FlinkSessionJobSummary(
                cluster=cluster,
                kubeconfig=str(kubeconfig),
                namespace=ns,
                workspace=ns,
                name=item["metadata"]["name"],
                deployment_name=str((item.get("spec") or {}).get("deploymentName") or ""),
                job_state=str(job.get("state") or "UNKNOWN"),
                lifecycle_state=str(st.get("lifecycleState") or "UNKNOWN"),
            )
        )
    return out


def discover_log_pods(cluster: str, kubeconfig: Path) -> list[FlinkPodTarget]:
    targets: list[FlinkPodTarget] = []

    selectors = [
        ("app.kubernetes.io/name=flink-kubernetes-operator", "operator", "flink-kubernetes-operator"),
        ("type=flink-native-kubernetes", "flink", ""),
        ("ssb.service=ssb", "ssb-sse", "ssb"),
    ]

    for label_sel, role, default_app in selectors:
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
            if role != "operator" and not NS_PATTERN.match(ns):
                continue
            phase = item.get("status", {}).get("phase", "")
            if phase in ("Failed", "Succeeded"):
                continue
            pod = item["metadata"]["name"]
            labels = item["metadata"].get("labels") or {}
            flink_app = labels.get("app") or labels.get("app.kubernetes.io/name") or default_app
            component = labels.get("component", "")
            if component:
                role = f"{role}-{component}"
            containers = [c["name"] for c in item["spec"].get("containers") or []]
            container = containers[0] if containers else "flink-main-container"
            if role == "ssb-sse":
                container = next((c for c in containers if c == "ssb"), container)
            elif "jobmanager" in labels.get("component", ""):
                container = next((c for c in containers if c == "flink-main-container"), container)
            targets.append(
                FlinkPodTarget(
                    cluster=cluster,
                    kubeconfig=str(kubeconfig),
                    namespace=ns,
                    workspace=ns if NS_PATTERN.match(ns) else "flink-operator",
                    role=role,
                    pod=pod,
                    container=container,
                    flink_app=str(flink_app),
                )
            )
    return targets


def discover_all(
    kcfg_dir: Path = DEFAULT_KCFG_DIR,
) -> tuple[list[FlinkPodTarget], list[FlinkDeploymentSummary], list[FlinkSessionJobSummary]]:
    pods: list[FlinkPodTarget] = []
    deps: list[FlinkDeploymentSummary] = []
    jobs: list[FlinkSessionJobSummary] = []
    for cluster, kc in list_kubeconfigs(kcfg_dir):
        deps.extend(discover_deployments(cluster, kc))
        jobs.extend(discover_session_jobs(cluster, kc))
        pods.extend(discover_log_pods(cluster, kc))
    pods = sorted(pods, key=lambda t: (t.cluster, t.namespace, t.role, t.pod))
    return pods, deps, jobs


def main() -> None:
    kcfg_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_KCFG_DIR
    pods, deps, jobs = discover_all(kcfg_dir)
    print(
        json.dumps(
            {
                "pods": [p.to_json() for p in pods],
                "deployments": [d.to_json() for d in deps],
                "session_jobs": [j.to_json() for j in jobs],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
