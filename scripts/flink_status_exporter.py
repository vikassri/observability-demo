#!/usr/bin/env python3
"""Prometheus exporter: FlinkDeployment / FlinkSessionJob CR status (CSA / SSB)."""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from flink_discovery import discover_all

LISTEN_HOST = os.environ.get("FLINK_EXPORTER_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("FLINK_EXPORTER_LISTEN_PORT", "19670"))
KCFG_DIR = Path(os.environ.get("FLINK_KUBECONFIG_DIR", "/etc/prometheus/kubeconfig"))
SCRAPE_INTERVAL = int(os.environ.get("FLINK_STATUS_INTERVAL", "30"))

log = logging.getLogger("flink_status_exporter")
_cache_lock = threading.Lock()
_metrics_text = "# flink status exporter starting\n"


def _collect() -> str:
    pods, deps, session_jobs = discover_all(KCFG_DIR)
    lines: list[str] = []

    jm_pods = sum(1 for p in pods if "jobmanager" in p.role or p.role == "flink-jobmanager")
    tm_hint = sum(1 for p in pods if "taskmanager" in p.role)

    dep_job_states: Counter[tuple[str, str, str, str]] = Counter()
    for d in deps:
        base = (
            f'cluster="{d.cluster}",flink_namespace="{d.namespace}",'
            f'flink_workspace="{d.workspace}",flink_deployment="{d.name}"'
        )
        lines.append(
            f'flink_deployment_info{{{base},job_state="{d.job_state}",lifecycle_state="{d.lifecycle_state}"}} 1'
        )
        failed = 1.0 if d.job_state.upper() == "FAILED" else 0.0
        lines.append(f"flink_deployment_failed{{{base}}} {failed}")
        dep_job_states[(d.cluster, d.namespace, d.workspace, d.job_state)] += 1

    for (cluster, namespace, workspace, job_state), count in sorted(dep_job_states.items()):
        base = (
            f'cluster="{cluster}",flink_namespace="{namespace}",'
            f'flink_workspace="{workspace}",job_state="{job_state}"'
        )
        lines.append(f"flink_deployments_total{{{base}}} {float(count)}")

    sess_states: Counter[tuple[str, str, str, str]] = Counter()
    for j in session_jobs:
        base = (
            f'cluster="{j.cluster}",flink_namespace="{j.namespace}",'
            f'flink_workspace="{j.workspace}",flink_sessionjob="{j.name}"'
        )
        lines.append(
            f'flink_sessionjob_info{{{base},deployment="{j.deployment_name}",'
            f'job_state="{j.job_state}",lifecycle_state="{j.lifecycle_state}"}} 1'
        )
        running = 1.0 if j.job_state.upper() == "RUNNING" else 0.0
        failed = 1.0 if j.job_state.upper() == "FAILED" else 0.0
        lines.append(f"flink_sessionjob_running{{{base}}} {running}")
        lines.append(f"flink_sessionjob_failed{{{base}}} {failed}")
        sess_states[(j.cluster, j.namespace, j.workspace, j.job_state)] += 1

    for (cluster, namespace, workspace, job_state), count in sorted(sess_states.items()):
        base = (
            f'cluster="{cluster}",flink_namespace="{namespace}",'
            f'flink_workspace="{workspace}",job_state="{job_state}"'
        )
        lines.append(f"flink_sessionjobs_total{{{base}}} {float(count)}")

    workspaces = {(p.cluster, p.namespace, p.workspace) for p in pods} | {
        (d.cluster, d.namespace, d.workspace) for d in deps
    }
    for cluster, namespace, workspace in sorted(workspaces):
        base = f'cluster="{cluster}",flink_namespace="{namespace}",flink_workspace="{workspace}"'
        op = sum(
            1
            for p in pods
            if p.cluster == cluster and p.role == "operator" and p.pod
        )
        lines.append(f"flink_operator_pods_up{{{base}}} {float(op)}")
        lines.append(f"flink_jobmanager_pods_up{{{base}}} {float(jm_pods)}")
        lines.append(f"flink_taskmanager_pods_up{{{base}}} {float(tm_hint)}")

    lines.append(f"flink_status_exporter_last_scrape_timestamp {int(time.time())}")
    return "\n".join(lines) + "\n"


def _scrape_loop() -> None:
    global _metrics_text
    while True:
        try:
            text = _collect()
            with _cache_lock:
                _metrics_text = text
        except Exception:
            log.exception("Scrape failed")
        time.sleep(SCRAPE_INTERVAL)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/metrics"):
            self.send_response(404)
            self.end_headers()
            return
        with _cache_lock:
            body = _metrics_text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug(fmt, *args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=_scrape_loop, daemon=True).start()
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler)
    log.info("Flink status exporter on http://%s:%s/metrics", LISTEN_HOST, LISTEN_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
