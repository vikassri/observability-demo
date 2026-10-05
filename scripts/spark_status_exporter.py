#!/usr/bin/env python3
"""Prometheus exporter: SparkApplication CR state + driver/operator pods (CDE)."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from spark_discovery import discover_all, discover_applications, list_kubeconfigs

LISTEN_HOST = os.environ.get("SPARK_EXPORTER_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SPARK_EXPORTER_LISTEN_PORT", "19660"))
KCFG_DIR = Path(os.environ.get("SPARK_KUBECONFIG_DIR", "/etc/prometheus/kubeconfig"))
SCRAPE_INTERVAL = int(os.environ.get("SPARK_STATUS_INTERVAL", "30"))
LOG_WINDOW = os.environ.get("SPARK_LOG_WINDOW", "15m")

FAIL_RE = __import__("re").compile(r"(?i)(ERROR|Exception|FAILED|Application failed)")

log = logging.getLogger("spark_status_exporter")
_cache_lock = threading.Lock()
_metrics_text = "# spark status exporter starting\n"


def _operator_up(cluster: str, kubeconfig: Path, namespace: str) -> float:
    proc = subprocess.run(
        [
            "kubectl",
            f"--kubeconfig={kubeconfig}",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name=spark-operator",
            "-o",
            "jsonpath={.items[?(@.status.phase=='Running')].metadata.name}",
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if proc.returncode != 0:
        return 0.0
    return 1.0 if (proc.stdout or "").strip() else 0.0


def _operator_error_lines(kubeconfig: Path, namespace: str, pod: str) -> int:
    proc = subprocess.run(
        [
            "kubectl",
            f"--kubeconfig={kubeconfig}",
            "logs",
            "-n",
            namespace,
            pod,
            f"--since={LOG_WINDOW}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return 0
    return sum(1 for line in proc.stdout.splitlines() if FAIL_RE.search(line))


def _collect() -> str:
    pods, apps = discover_all(KCFG_DIR)
    lines: list[str] = []

    state_counts: Counter[tuple[str, str, str, str]] = Counter()
    for app in apps:
        state_counts[(app.cluster, app.namespace, app.workspace, app.state)] += 1
        base = (
            f'cluster="{app.cluster}",spark_namespace="{app.namespace}",'
            f'spark_workspace="{app.workspace}",spark_application="{app.name}"'
        )
        failed = 1.0 if "fail" in app.state.lower() else 0.0
        lines.append(f"spark_application_info{{{base},state=\"{app.state}\"}} 1")
        lines.append(f"spark_application_failed{{{base}}} {failed}")

    for (cluster, namespace, workspace, state), count in sorted(state_counts.items()):
        base = (
            f'cluster="{cluster}",spark_namespace="{namespace}",'
            f'spark_workspace="{workspace}",state="{state}"'
        )
        lines.append(f"spark_applications_total{{{base}}} {float(count)}")

    drivers = sum(1 for p in pods if p.role == "driver")
    default_ns = os.environ.get("SPARK_NAMESPACE", "dex-app-jjqrq8z9")
    for cluster, kc in list_kubeconfigs(KCFG_DIR):
        ns = default_ns
        app_list = discover_applications(cluster, kc)
        if app_list:
            ns = app_list[0].namespace
        op_up = _operator_up(cluster, kc, ns)
        base = f'cluster="{cluster}",spark_namespace="{ns}",spark_workspace="{ns}"'
        lines.append(f"spark_operator_up{{{base}}} {op_up}")
        lines.append(f"spark_driver_pods_up{{{base}}} {float(drivers)}")

        proc = subprocess.run(
            [
                "kubectl",
                f"--kubeconfig={kc}",
                "get",
                "pods",
                "-n",
                ns,
                "-l",
                "app.kubernetes.io/name=spark-operator",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        op_pod = (proc.stdout or "").strip()
        if op_pod:
            err_n = _operator_error_lines(kc, ns, op_pod)
            lines.append(f"spark_operator_error_lines{{{base}}} {float(err_n)}")

    lines.append(f"spark_status_exporter_last_scrape_timestamp {int(time.time())}")
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
    log.info("Spark status exporter on http://%s:%s/metrics", LISTEN_HOST, LISTEN_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
