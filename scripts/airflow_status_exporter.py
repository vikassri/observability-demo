#!/usr/bin/env python3
"""
Prometheus exporter for Airflow on CWO: /api/v2/monitor/health + scheduler log job outcomes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from airflow_discovery import AirflowPodTarget, discover_all

LISTEN_HOST = os.environ.get("AIRFLOW_EXPORTER_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("AIRFLOW_EXPORTER_LISTEN_PORT", "19650"))
KCFG_DIR = Path(os.environ.get("AIRFLOW_KUBECONFIG_DIR", "/etc/prometheus/kubeconfig"))
SCRAPE_INTERVAL = int(os.environ.get("AIRFLOW_STATUS_INTERVAL", "15"))
LOG_WINDOW = os.environ.get("AIRFLOW_LOG_WINDOW", "15m")

DAGRUN_STATE_RE = re.compile(r"DagRun Finished:.*state=(?P<state>[a-z]+)")
TASK_STATE_RE = re.compile(r"TaskInstance Finished:.*state=(?P<state>[a-z]+)")

log = logging.getLogger("airflow_status_exporter")
_cache_lock = threading.Lock()
_metrics_text = "# airflow status exporter starting\n"


def _kubectl_exec_json(target: AirflowPodTarget, url: str) -> dict[str, Any] | None:
    cmd = [
        "kubectl",
        f"--kubeconfig={target.kubeconfig}",
        "exec",
        "-n",
        target.namespace,
        target.pod,
        "-c",
        target.container,
        "--",
        "curl",
        "-sS",
        "-m",
        "5",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45, check=False)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _health_from_api(targets: list[AirflowPodTarget]) -> tuple[dict[str, float], str]:
    """Pick api-server pod per release and scrape /api/v2/monitor/health."""
    metrics: dict[str, float] = {}
    version = ""
    seen: set[tuple[str, str, str]] = set()
    for t in targets:
        if t.component != "api-server":
            continue
        key = (t.cluster, t.namespace, t.release)
        if key in seen:
            continue
        seen.add(key)
        health = _kubectl_exec_json(t, "http://127.0.0.1:8080/api/v2/monitor/health")
        ver = _kubectl_exec_json(t, "http://127.0.0.1:8080/api/v2/version")
        if ver and ver.get("version"):
            version = str(ver["version"])
        base = f'cluster="{t.cluster}",airflow_namespace="{t.namespace}",airflow_release="{t.release}"'
        if health is None:
            metrics[f"airflow_api_reachable{{{base}}}"] = 0.0
            continue
        metrics[f"airflow_api_reachable{{{base}}}"] = 1.0
        for part in ("metadatabase", "scheduler", "triggerer", "dag_processor"):
            block = health.get(part) or {}
            status = str(block.get("status") or "unknown").lower()
            metrics[f'airflow_component_healthy{{{base},component="{part}"}}'] = (
                1.0 if status == "healthy" else 0.0
            )
    return metrics, version


def _scheduler_log_counts(targets: list[AirflowPodTarget]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for t in targets:
        if t.component != "scheduler":
            continue
        cmd = [
            "kubectl",
            f"--kubeconfig={t.kubeconfig}",
            "logs",
            "-n",
            t.namespace,
            t.pod,
            "-c",
            t.container,
            f"--since={LOG_WINDOW}",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode != 0:
            continue
        base = f'cluster="{t.cluster}",airflow_namespace="{t.namespace}",airflow_release="{t.release}"'
        dag_counts: dict[str, int] = {}
        task_counts: dict[str, int] = {}
        for line in proc.stdout.splitlines():
            dm = DAGRUN_STATE_RE.search(line)
            if dm:
                st = dm.group("state")
                dag_counts[st] = dag_counts.get(st, 0) + 1
            tm = TASK_STATE_RE.search(line)
            if tm:
                st = tm.group("state")
                task_counts[st] = task_counts.get(st, 0) + 1
        for st, count in dag_counts.items():
            metrics[f'airflow_dagrun_finished_total{{{base},state="{st}"}}'] = float(count)
        for st, count in task_counts.items():
            metrics[f'airflow_taskinstance_finished_total{{{base},state="{st}"}}'] = float(count)
    return metrics


def _collect() -> str:
    targets = discover_all(KCFG_DIR)
    lines: list[str] = []
    health_metrics, version = _health_from_api(targets)
    lines.extend(f"{k} {v}" for k, v in sorted(health_metrics.items()))
    lines.extend(f"{k} {v}" for k, v in sorted(_scheduler_log_counts(targets).items()))

    schedulers = sum(1 for t in targets if t.component == "scheduler")
    if schedulers:
        for t in targets:
            if t.component == "scheduler":
                base = (
                    f'cluster="{t.cluster}",airflow_namespace="{t.namespace}",'
                    f'airflow_release="{t.release}"'
                )
                lines.append(f"airflow_scheduler_pods_up{{{base}}} {float(schedulers)}")
                if version:
                    lines.append(
                        f'airflow_version_info{{{base},version="{version}"}} 1'
                    )
                break

    lines.append(f"airflow_status_exporter_last_scrape_timestamp {int(time.time())}")
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
    log.info("Airflow status exporter on http://%s:%s/metrics", LISTEN_HOST, LISTEN_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
