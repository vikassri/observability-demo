#!/usr/bin/env python3
"""
Hub agent: discover Trino engines, refresh Prometheus file_sd for status exporter,
stream coordinator/worker logs to OTel.

Metrics: trino-status-exporter polls /v1/status on :8080 (same API family as Trino UI).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from trino_discovery import TrinoPodTarget, discover_all
from trino_prometheus_sd import write_sd

PROMETHEUS_FILE_SD = Path("/etc/prometheus/file_sd/trino_status.json")
KCFG_DIR = Path(os.environ.get("TRINO_KUBECONFIG_DIR", "/etc/prometheus/kubeconfig"))
INTERVAL_SEC = int(os.environ.get("TRINO_DISCOVERY_INTERVAL", "60"))
OTEL_LOGS_ENDPOINT = os.environ.get(
    "OTEL_LOGS_ENDPOINT", "http://127.0.0.1:4318/v1/logs"
)
LOG_FORWARDER_DIR = Path("/opt/uob-k8s-log-forwarder")
LOG_SCRIPT = LOG_FORWARDER_DIR / "kubectl_logs_to_otel.py"
LOG_VENV_PYTHON = Path("/opt/uob-k8s-log-forwarder/venv/bin/python")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trino_hub_agent")

_log_streams: dict[str, subprocess.Popen[bytes]] = {}
_shutdown = False


def _log_key(t: TrinoPodTarget) -> str:
    return f"{t.cluster}/{t.namespace}/logs/{t.pod}"


def _is_alive(proc: subprocess.Popen[bytes] | None) -> bool:
    return proc is not None and proc.poll() is None


def _write_file_sd() -> None:
    groups = write_sd(KCFG_DIR)
    log.info("Wrote %s (%d file_sd groups)", PROMETHEUS_FILE_SD, len(groups))


def _start_log_stream(t: TrinoPodTarget) -> None:
    if not LOG_SCRIPT.is_file():
        log.warning("Log script missing %s — run 10_setup-k8s-console-logs-to-otel.sh", LOG_SCRIPT)
        return
    python = LOG_VENV_PYTHON if LOG_VENV_PYTHON.is_file() else Path(sys.executable)
    service_name = f"trino-{t.role}-log"
    cmd = [
        str(python),
        str(LOG_SCRIPT),
        "--kubeconfig",
        t.kubeconfig,
        "--namespace",
        t.namespace,
        "--pod",
        t.pod,
        "--container",
        t.container,
        "--endpoint",
        OTEL_LOGS_ENDPOINT,
        "--service-name",
        service_name,
        "--product",
        "trino",
        "--cluster",
        t.cluster,
        "--trino-instance",
        t.trino_name,
    ]
    log.info("Log stream %s/%s container=%s", t.namespace, t.pod, t.container)
    _log_streams[_log_key(t)] = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _stop_log_stream(key: str) -> None:
    proc = _log_streams.pop(key, None)
    if proc and _is_alive(proc):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def sync(targets: list[TrinoPodTarget]) -> None:
    _write_file_sd()

    desired_logs = {_log_key(t) for t in targets}
    for key in list(_log_streams):
        if key not in desired_logs:
            _stop_log_stream(key)

    for t in targets:
        key = _log_key(t)
        if not _is_alive(_log_streams.get(key)):
            if key in _log_streams:
                _stop_log_stream(key)
            _start_log_stream(t)

    for key, proc in list(_log_streams.items()):
        if proc.poll() is not None:
            err = (proc.stderr.read().decode() if proc.stderr else "")[:200]
            log.warning("Log stream died %s: %s", key, err)


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown
    log.info("Signal %s — shutting down", signum)
    _shutdown = True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    once = "--once" in sys.argv
    log.info("Trino hub agent started (interval=%ss, kubeconfigs=%s)", INTERVAL_SEC, KCFG_DIR)

    while not _shutdown:
        try:
            targets = discover_all(KCFG_DIR)
            engines = len({(t.cluster, t.namespace, t.trino_name) for t in targets})
            log.info(
                "Discovered %d Trino engine(s), %d pod target(s)",
                engines,
                len(targets),
            )
            sync(targets)
        except Exception:
            log.exception("Discovery cycle failed")
        if once:
            break
        time.sleep(INTERVAL_SEC)

    for key in list(_log_streams):
        _stop_log_stream(key)


if __name__ == "__main__":
    main()
