#!/usr/bin/env python3
"""Prometheus file_sd for Spark status exporter."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from spark_discovery import discover_applications, list_kubeconfigs

OUT = Path("/etc/prometheus/file_sd/spark_status.json")
KCFG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/etc/prometheus/kubeconfig")
HOST = "127.0.0.1"
PORT = int(os.environ.get("SPARK_EXPORTER_LISTEN_PORT", "19660"))


def write_sd(kcfg_dir: Path = KCFG) -> list[dict]:
    workspaces: set[tuple[str, str, str]] = set()
    for cluster, kc in list_kubeconfigs(kcfg_dir):
        for app in discover_applications(cluster, kc):
            workspaces.add((app.cluster, app.namespace, app.workspace))
    if not workspaces:
        for cluster, kc in list_kubeconfigs(kcfg_dir):
            workspaces.add((cluster, "dex-app-jjqrq8z9", "dex-app-jjqrq8z9"))

    groups: list[dict] = []
    for cluster, namespace, workspace in sorted(workspaces):
        groups.append(
            {
                "targets": [f"{HOST}:{PORT}"],
                "labels": {
                    "cluster": cluster,
                    "spark_namespace": namespace,
                    "spark_workspace": workspace,
                },
            }
        )
    if not groups:
        groups = [{"targets": [f"{HOST}:{PORT}"], "labels": {"spark_workspace": "none"}}]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(groups, indent=2) + "\n")
    return groups


def main() -> None:
    kcfg = Path(sys.argv[1]) if len(sys.argv) > 1 else KCFG
    print(json.dumps(write_sd(kcfg), indent=2))


if __name__ == "__main__":
    main()
