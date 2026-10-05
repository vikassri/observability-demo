#!/usr/bin/env python3
"""Flink hub agent: file_sd + JM/SSB/operator logs → OTel."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from flink_discovery import FlinkPodTarget, discover_all
from flink_prometheus_sd import write_sd

KCFG_DIR = Path(os.environ.get("FLINK_KUBECONFIG_DIR", "/etc/prometheus/kubeconfig"))
INTERVAL_SEC = int(os.environ.get("FLINK_DISCOVERY_INTERVAL", "60"))
OTEL_LOGS_ENDPOINT = os.environ.get("OTEL_LOGS_ENDPOINT", "http://127.0.0.1:4318/v1/logs")
LOG_SCRIPT = Path("/opt/uob-k8s-log-forwarder/kubectl_logs_to_otel.py")
LOG_VENV_PYTHON = Path("/opt/uob-k8s-log-forwarder/venv/bin/python")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("flink_hub_agent")
_log_streams: dict[str, subprocess.Popen[bytes]] = {}
_shutdown = False


def _log_key(t: FlinkPodTarget) -> str:
    return f"{t.cluster}/{t.namespace}/logs/{t.pod}"


def _is_alive(proc: subprocess.Popen[bytes] | None) -> bool:
    return proc is not None and proc.poll() is None


def _start_log_stream(t: FlinkPodTarget) -> None:
    if not LOG_SCRIPT.is_file():
        return
    python = LOG_VENV_PYTHON if LOG_VENV_PYTHON.is_file() else Path(sys.executable)
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
        f"flink-{t.role}-log",
        "--product",
        "flink",
        "--cluster",
        t.cluster,
        "--flink-app",
        t.flink_app,
    ]
    _log_streams[_log_key(t)] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _stop_log_stream(key: str) -> None:
    proc = _log_streams.pop(key, None)
    if proc and _is_alive(proc):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def sync(pods: list[FlinkPodTarget]) -> None:
    groups = write_sd(KCFG_DIR)
    log.info("Wrote flink_status.json (%d groups)", len(groups))
    desired = {_log_key(t) for t in pods}
    for key in list(_log_streams):
        if key not in desired:
            _stop_log_stream(key)
    for t in pods:
        key = _log_key(t)
        if not _is_alive(_log_streams.get(key)):
            if key in _log_streams:
                _stop_log_stream(key)
            _start_log_stream(t)


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown
    log.info("Signal %s — shutting down", signum)
    _shutdown = True


def main() -> None:
    global _shutdown
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    while not _shutdown:
        try:
            pods, deps, jobs = discover_all(KCFG_DIR)
            log.info(
                "Discovered %d Flink pod(s), %d deployment(s), %d session job(s)",
                len(pods),
                len(deps),
                len(jobs),
            )
            sync(pods)
        except Exception:
            log.exception("cycle failed")
        if "--once" in sys.argv:
            break
        time.sleep(INTERVAL_SEC)
    for key in list(_log_streams):
        _stop_log_stream(key)


if __name__ == "__main__":
    main()
