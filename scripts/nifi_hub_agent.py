#!/usr/bin/env python3
"""
Hub agent: discover NiFi deployments, maintain kubectl port-forwards for metrics,
write Prometheus file_sd, and supervise kubectl log streams to OTel.

Metrics → local Prometheus (nifi_federate + file_sd).
Logs → OTLP (not Prometheus).
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

from nifi_discovery import NiFiDeployment, discover_all, prometheus_file_sd

PROMETHEUS_FILE_SD = Path("/etc/prometheus/file_sd/nifi_federate.json")
STATE_DIR = Path(os.environ.get("NIFI_HUB_STATE_DIR", "/var/lib/nifi-hub-agent"))
LOG_FORWARDER_DIR = Path("/opt/uob-k8s-log-forwarder")
LOG_SCRIPT = LOG_FORWARDER_DIR / "kubectl_logs_to_otel.py"
LOG_VENV_PYTHON = Path("/opt/uob-k8s-log-forwarder/venv/bin/python")
KCFG_DIR = Path(os.environ.get("NIFI_KUBECONFIG_DIR", "/etc/prometheus/kubeconfig"))
INTERVAL_SEC = int(os.environ.get("NIFI_DISCOVERY_INTERVAL", "60"))
OTEL_LOGS_ENDPOINT = os.environ.get(
    "OTEL_LOGS_ENDPOINT", "http://127.0.0.1:4318/v1/logs"
)
NIFI_LOG_CONTAINER = os.environ.get("NIFI_LOG_CONTAINER", "nifi")
# Cloudera NiFi pods expose nifi-app.log via the app-log sidecar (FIFO in nifi container).
NIFI_APP_LOG_CONTAINER = os.environ.get("NIFI_APP_LOG_CONTAINER", "app-log")
NIFI_LOG_MODE = os.environ.get("NIFI_LOG_MODE", "app").lower()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nifi_hub_agent")

_port_forwards: dict[str, subprocess.Popen[bytes]] = {}
_log_streams: dict[str, subprocess.Popen[bytes]] = {}
_shutdown = False


def _key(dep: NiFiDeployment, suffix: str) -> str:
    return f"{dep.cluster}/{dep.namespace}/{suffix}"


def _pf_key(dep: NiFiDeployment) -> str:
    return _key(dep, "port-forward")


def _log_key(dep: NiFiDeployment, pod: str) -> str:
    return _key(dep, f"logs/{pod}")


def _is_alive(proc: subprocess.Popen[bytes] | None) -> bool:
    return proc is not None and proc.poll() is None


def _write_file_sd(deployments: list[NiFiDeployment]) -> None:
    payload = prometheus_file_sd(deployments)
    PROMETHEUS_FILE_SD.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROMETHEUS_FILE_SD.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(PROMETHEUS_FILE_SD)
    log.info("Wrote %s (%d federate targets)", PROMETHEUS_FILE_SD, len(payload))


def _start_port_forward(dep: NiFiDeployment) -> None:
    if dep.local_port is None:
        return
    cmd = [
        "kubectl",
        f"--kubeconfig={dep.kubeconfig}",
        "port-forward",
        "-n",
        dep.namespace,
        f"svc/{os.environ.get('NIFI_PROMETHEUS_SVC', 'prometheus')}",
        f"{dep.local_port}:{os.environ.get('NIFI_PROMETHEUS_SVC_PORT', '19090')}",
        "--address=127.0.0.1",
    ]
    log.info("Starting port-forward %s -> 127.0.0.1:%s", dep.namespace, dep.local_port)
    _port_forwards[_pf_key(dep)] = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _stop_port_forward(key: str) -> None:
    proc = _port_forwards.pop(key, None)
    if proc and _is_alive(proc):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _log_script_path() -> Path | None:
    if NIFI_LOG_MODE not in ("app", "nifi-app", "console", "kubectl", "stdout"):
        log.warning("Unknown NIFI_LOG_MODE=%s — use app or console", NIFI_LOG_MODE)
        return None
    if not LOG_SCRIPT.is_file():
        log.warning("Log script missing %s — run 10_setup-k8s-console-logs-to-otel.sh", LOG_SCRIPT)
        return None
    return LOG_SCRIPT


def _log_container_name() -> str:
    if NIFI_LOG_MODE in ("app", "nifi-app"):
        return NIFI_APP_LOG_CONTAINER
    if NIFI_LOG_MODE in ("console", "kubectl", "stdout"):
        return NIFI_LOG_CONTAINER
    return NIFI_LOG_CONTAINER


def _start_log_stream(dep: NiFiDeployment, pod: str) -> None:
    log_script = _log_script_path()
    if log_script is None:
        return
    python = LOG_VENV_PYTHON if LOG_VENV_PYTHON.is_file() else Path(sys.executable)
    cmd = [
        str(python),
        str(log_script),
        "--kubeconfig",
        dep.kubeconfig,
        "--namespace",
        dep.namespace,
        "--pod",
        pod,
        "--container",
        _log_container_name(),
        "--endpoint",
        OTEL_LOGS_ENDPOINT,
    ]
    log.info(
        "Starting log stream (%s, container=%s) %s/%s",
        NIFI_LOG_MODE,
        _log_container_name(),
        dep.namespace,
        pod,
    )
    _log_streams[_log_key(dep, pod)] = subprocess.Popen(
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


def sync(deployments: list[NiFiDeployment]) -> None:
    _write_file_sd(deployments)

    desired_pf = {_pf_key(d) for d in deployments if d.has_prometheus and d.local_port}
    for key in list(_port_forwards):
        if key not in desired_pf:
            _stop_port_forward(key)

    for dep in deployments:
        if not dep.has_prometheus or dep.local_port is None:
            continue
        key = _pf_key(dep)
        if not _is_alive(_port_forwards.get(key)):
            if key in _port_forwards:
                _stop_port_forward(key)
            _start_port_forward(dep)

    desired_logs: set[str] = set()
    for dep in deployments:
        for pod in dep.pods:
            desired_logs.add(_log_key(dep, pod))

    for key in list(_log_streams):
        if key not in desired_logs:
            _stop_log_stream(key)

    for dep in deployments:
        for pod in dep.pods:
            key = _log_key(dep, pod)
            if not _is_alive(_log_streams.get(key)):
                if key in _log_streams:
                    _stop_log_stream(key)
                _start_log_stream(dep, pod)

    for key, proc in list(_port_forwards.items()):
        if proc.poll() is not None:
            err = (proc.stderr.read().decode() if proc.stderr else "")[:200]
            log.warning("Port-forward died %s: %s", key, err)

    for key, proc in list(_log_streams.items()):
        if proc.poll() is not None:
            err = (proc.stderr.read().decode() if proc.stderr else "")[:200]
            log.warning("Log stream died %s: %s", key, err)


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown
    log.info("Signal %s — shutting down", signum)
    _shutdown = True


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    once = "--once" in sys.argv
    log.info("NiFi hub agent started (interval=%ss, kubeconfigs=%s)", INTERVAL_SEC, KCFG_DIR)

    while not _shutdown:
        try:
            deps = discover_all(KCFG_DIR)
            metrics = sum(1 for d in deps if d.has_prometheus)
            log.info(
                "Discovered %d NiFi namespace(s), %d with Prometheus",
                len(deps),
                metrics,
            )
            sync(deps)
        except Exception:
            log.exception("Discovery cycle failed")
        if once:
            break
        time.sleep(INTERVAL_SEC)

    for key in list(_port_forwards):
        _stop_port_forward(key)
    for key in list(_log_streams):
        _stop_log_stream(key)


if __name__ == "__main__":
    main()
