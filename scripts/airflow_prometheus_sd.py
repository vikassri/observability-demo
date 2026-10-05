#!/usr/bin/env python3
"""Prometheus file_sd for Airflow status exporter."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from airflow_discovery import discover_all

OUT = Path("/etc/prometheus/file_sd/airflow_status.json")
KCFG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/etc/prometheus/kubeconfig")
HOST = "127.0.0.1"
PORT = int(os.environ.get("AIRFLOW_EXPORTER_LISTEN_PORT", "19650"))
UI_URL = os.environ.get("AIRFLOW_UI_URL", "")


def write_sd(kcfg_dir: Path = KCFG) -> list[dict]:
    targets = discover_all(kcfg_dir)
    engines: dict[tuple[str, str, str], None] = {}
    for t in targets:
        engines[(t.cluster, t.namespace, t.release)] = None

    groups: list[dict] = []
    for cluster, namespace, release in sorted(engines):
        groups.append(
            {
                "targets": [f"{HOST}:{PORT}"],
                "labels": {
                    "cluster": cluster,
                    "airflow_namespace": namespace,
                    "airflow_release": release,
                    "airflow_ui_url": UI_URL,
                },
            }
        )
    if not groups:
        groups = [{"targets": [f"{HOST}:{PORT}"], "labels": {"airflow_release": "none"}}]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(groups, indent=2) + "\n")
    return groups


def main() -> None:
    kcfg = Path(sys.argv[1]) if len(sys.argv) > 1 else KCFG
    print(json.dumps(write_sd(kcfg), indent=2))


if __name__ == "__main__":
    main()
