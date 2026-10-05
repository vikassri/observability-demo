#!/usr/bin/env python3
"""Write Prometheus file_sd for the Trino /v1/status exporter (not :35000)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import subprocess

from trino_discovery import discover_all

OUT = Path("/etc/prometheus/file_sd/trino_status.json")
KCFG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/etc/prometheus/kubeconfig")
HOST = "127.0.0.1"
PORT = 19640


def _ingress_host(kubeconfig: Path, namespace: str, trino_name: str) -> str:
    proc = subprocess.run(
        [
            "kubectl",
            f"--kubeconfig={kubeconfig}",
            "get",
            "trino",
            trino_name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.coordinatorIngressHostname}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    host = (proc.stdout or "").strip()
    return host


def write_sd(kcfg_dir: Path = KCFG) -> list[dict]:
    targets = discover_all(kcfg_dir)
    engines: dict[tuple[str, str, str], tuple[bool, str]] = {}
    for t in targets:
        key = (t.cluster, t.namespace, t.trino_name)
        if key not in engines:
            engines[key] = (
                t.trino_ready,
                _ingress_host(Path(t.kubeconfig), t.namespace, t.trino_name),
            )

    groups: list[dict] = []
    for (cluster, namespace, trino_name), (ready, ingress) in sorted(engines.items()):
        ui = f"https://{ingress}/ui/" if ingress else ""
        groups.append(
            {
                "targets": [f"{HOST}:{PORT}"],
                "labels": {
                    "cluster": cluster,
                    "trino_namespace": namespace,
                    "trino_instance": trino_name,
                    "trino_ready": str(ready).lower(),
                    "trino_ingress": ingress,
                    "trino_ui_url": ui,
                },
            }
        )
    if not groups:
        groups = [{"targets": [f"{HOST}:{PORT}"], "labels": {"trino_instance": "none"}}]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(groups, indent=2) + "\n")
    return groups


def main() -> None:
    kcfg = Path(sys.argv[1]) if len(sys.argv) > 1 else KCFG
    groups = write_sd(kcfg)
    print(json.dumps(groups, indent=2))


if __name__ == "__main__":
    main()
