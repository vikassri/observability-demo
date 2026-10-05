#!/usr/bin/env python3
"""Prometheus file_sd for Flink status exporter."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from flink_discovery import discover_all

OUT = Path("/etc/prometheus/file_sd/flink_status.json")
KCFG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/etc/prometheus/kubeconfig")
HOST = "127.0.0.1"
PORT = int(os.environ.get("FLINK_EXPORTER_LISTEN_PORT", "19670"))


def write_sd(kcfg_dir: Path = KCFG) -> list[dict]:
    _, deps, jobs = discover_all(kcfg_dir)
    workspaces: set[tuple[str, str, str]] = set()
    for d in deps:
        workspaces.add((d.cluster, d.namespace, d.workspace))
    for j in jobs:
        workspaces.add((j.cluster, j.namespace, j.workspace))
    if not workspaces:
        workspaces.add(("csa", "csa01-csa-0ecfd9", "csa01-csa-0ecfd9"))

    groups: list[dict] = []
    for cluster, namespace, workspace in sorted(workspaces):
        groups.append(
            {
                "targets": [f"{HOST}:{PORT}"],
                "labels": {
                    "cluster": cluster,
                    "flink_namespace": namespace,
                    "flink_workspace": workspace,
                },
            }
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(groups, indent=2) + "\n")
    return groups


def main() -> None:
    kcfg = Path(sys.argv[1]) if len(sys.argv) > 1 else KCFG
    print(json.dumps(write_sd(kcfg), indent=2))


if __name__ == "__main__":
    main()
