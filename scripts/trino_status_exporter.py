#!/usr/bin/env python3
"""
Prometheus exporter for Trino: polls GET /v1/status (port 8080) via kubectl exec.
Matches the coordinator/worker HTTP API used by the Trino UI backend (not :35000 JMX, which is off in this build).
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

from trino_discovery import TrinoPodTarget, discover_all

LISTEN_HOST = os.environ.get("TRINO_EXPORTER_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("TRINO_EXPORTER_LISTEN_PORT", "19640"))
KCFG_DIR = Path(os.environ.get("TRINO_KUBECONFIG_DIR", "/etc/prometheus/kubeconfig"))
SCRAPE_INTERVAL = int(os.environ.get("TRINO_STATUS_INTERVAL", "15"))
QUERY_LOG_WINDOW = os.environ.get("TRINO_QUERY_LOG_WINDOW", "15m")

QUERY_STATE_RE = re.compile(
    r"Query\s+(?P<query_id>[0-9_]+_[a-z0-9]+)\s+::\s+(?P<state>[A-Z_]+)\s+::"
)

log = logging.getLogger("trino_status_exporter")
_cache_lock = threading.Lock()
_metrics_text = "# trino status exporter starting\n"
_targets: list[TrinoPodTarget] = []


def _kubectl_exec_json(target: TrinoPodTarget, url: str) -> dict[str, Any] | None:
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _parse_uptime_seconds(uptime: str) -> float | None:
    if not uptime:
        return None
    uptime = uptime.strip()
    if uptime.endswith("h"):
        try:
            return float(uptime[:-1]) * 3600.0
        except ValueError:
            return None
    if uptime.endswith("m"):
        try:
            return float(uptime[:-1]) * 60.0
        except ValueError:
            return None
    if uptime.endswith("s"):
        try:
            return float(uptime[:-1])
        except ValueError:
            return None
    return None


def _query_states_from_logs(target: TrinoPodTarget) -> dict[str, int]:
    if target.role != "coordinator":
        return {}
    cmd = [
        "kubectl",
        f"--kubeconfig={target.kubeconfig}",
        "logs",
        "-n",
        target.namespace,
        target.pod,
        "-c",
        target.container,
        f"--since={QUERY_LOG_WINDOW}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45, check=False)
    except subprocess.TimeoutExpired:
        return {}
    if proc.returncode != 0:
        return {}
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if "QueryMonitor" not in line or "TIMELINE:" not in line:
            continue
        match = QUERY_STATE_RE.search(line)
        if not match:
            continue
        state = match.group("state")
        counts[state] = counts.get(state, 0) + 1
    return counts


def _label_set(target: TrinoPodTarget, extra: dict[str, str] | None = None) -> str:
    labels = {
        "cluster": target.cluster,
        "trino_instance": target.trino_name,
        "trino_namespace": target.namespace,
        "trino_role": target.role,
        "trino_pod": target.pod,
    }
    if extra:
        labels.update(extra)
    return "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"


def _collect() -> str:
    targets = discover_all(KCFG_DIR)
    lines: list[str] = []

    engines = {(t.cluster, t.namespace, t.trino_name) for t in targets}
    for cluster, namespace, trino_name in sorted(engines):
        ready_val = "1"
        for t in targets:
            if t.cluster == cluster and t.namespace == namespace and t.trino_name == trino_name:
                ready_val = "1" if t.trino_ready else "0"
                break
        base = (
            f'cluster="{cluster}",trino_instance="{trino_name}",trino_namespace="{namespace}"'
        )
        lines.append(f"trino_engine_ready{{{base}}} {ready_val}")
        workers = sum(
            1
            for t in targets
            if t.cluster == cluster
            and t.namespace == namespace
            and t.trino_name == trino_name
            and t.role == "worker"
        )
        lines.append(f"trino_workers_expected{{{base}}} {workers}")

    for target in targets:
        lbl = _label_set(target)
        status = _kubectl_exec_json(target, "http://127.0.0.1:8080/v1/status")
        if status is None:
            lines.append(f"trino_node_up{lbl} 0")
            continue
        lines.append(f"trino_node_up{lbl} 1")
        heap_used = status.get("heapUsed")
        heap_avail = status.get("heapAvailable")
        if heap_used is not None:
            lines.append(f"trino_node_heap_used_bytes{lbl} {heap_used}")
        if heap_avail is not None:
            lines.append(f"trino_node_heap_available_bytes{lbl} {heap_avail}")
        proc_cpu = status.get("processCpuLoad")
        sys_cpu = status.get("systemCpuLoad")
        if proc_cpu is not None:
            lines.append(f"trino_node_process_cpu_load{lbl} {proc_cpu}")
        if sys_cpu is not None:
            lines.append(f"trino_node_system_cpu_load{lbl} {sys_cpu}")
        mem = status.get("memoryInfo") or {}
        pool = mem.get("pool") or {}
        free_bytes = pool.get("freeBytes")
        if free_bytes is not None:
            lines.append(f"trino_node_free_memory_bytes{lbl} {free_bytes}")
        uptime = _parse_uptime_seconds(str(status.get("uptime") or ""))
        if uptime is not None:
            lines.append(f"trino_node_uptime_seconds{lbl} {uptime}")

        for state, count in _query_states_from_logs(target).items():
            state_lbl = _label_set(target, {"query_state": state})
            lines.append(f"trino_query_timeline_total{state_lbl} {count}")

    lines.append(f"trino_status_exporter_last_scrape_timestamp {int(time.time())}")
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
    thread = threading.Thread(target=_scrape_loop, daemon=True)
    thread.start()
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler)
    log.info("Trino status exporter on http://%s:%s/metrics", LISTEN_HOST, LISTEN_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
