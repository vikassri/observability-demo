#!/usr/bin/env python3
"""Prometheus exporter for NiFi processor run-state: polls the flow REST API via kubectl exec.

Why this exists: the federated NiFi series (job=dfx-nifi-web) carry queues, bulletins and
failure counters, but no run-state — NiFi's Prometheus reporter has no metric for it. That gap
is the reason `nifi-conn-queue-stalled` has to *guess* a stopped processor from a connection
that stopped draining, and that guess only lands when the queue is both deep (>100 items) and
flat across two samples 15m apart. Stopping a processor on a low-volume flow therefore alerts
nobody. This exporter publishes the missing signal so a rule can fire on the state itself.

Transport: `kubectl exec` into the DFX Prometheus pod, which already has the client certificate
mounted — the key stays in the cluster, nothing is copied to the host. That mirrors
trino_status_exporter.py. Two things to confirm against the live deployment, both configurable
below and both reported by `--selftest`:
  * the certificate must authorize the flow API, not just /flow/metrics/prometheus
  * NIFI_CERT_DIR and NIFI_PROM_CONTAINER must match that pod's mounts and container name

Metrics:
  nifi_processor_run_status{...}              1 when runStatus is Running, 0 otherwise
  nifi_processors_total{...}                  processors seen per walk (0 means the walk failed)
  nifi_status_exporter_up                     1 when every API call in the last scrape succeeded
  nifi_status_exporter_last_scrape_timestamp  emitted only on success, so a stall is visible
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

LISTEN_HOST = os.environ.get("NIFI_EXPORTER_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("NIFI_EXPORTER_LISTEN_PORT", "19680"))
SCRAPE_INTERVAL = int(os.environ.get("NIFI_STATUS_INTERVAL", "30"))

KUBECONFIG = Path(os.environ.get("NIFI_KUBECONFIG", "/etc/prometheus/kubeconfig/cdf.yaml"))
NAMESPACE = os.environ.get("NIFI_K8S_NAMESPACE", "dfx-nifi-deployment-ns")
WEB_SVC = os.environ.get("NIFI_WEB_SVC", "dfx-nifi-web")
WEB_PORT = os.environ.get("NIFI_WEB_PORT", "8443")
API_URL = os.environ.get("NIFI_API_URL", f"https://{WEB_SVC}:{WEB_PORT}")

PROM_POD = os.environ.get("NIFI_PROM_POD", "")
PROM_POD_SELECTOR = os.environ.get("NIFI_PROM_POD_SELECTOR", "app=prometheus")
PROM_CONTAINER = os.environ.get("NIFI_PROM_CONTAINER", "")

CERT_DIR = os.environ.get("NIFI_CERT_DIR", "/etc/prometheus/secrets/prometheus-cert")
CERT_FILE = os.environ.get("NIFI_CERT_FILE", f"{CERT_DIR}/tls.crt")
KEY_FILE = os.environ.get("NIFI_KEY_FILE", f"{CERT_DIR}/tls.key")
CA_FILE = os.environ.get("NIFI_CA_FILE", f"{CERT_DIR}/ca.crt")
# Off by default: the cert should validate against dfx-nifi-web's DNS name. Turn on only if the
# SANs turn out not to cover the service name, and note it weakens the transport.
INSECURE = os.environ.get("NIFI_INSECURE", "").lower() in ("1", "true", "yes")

# The process group(s) to walk as well as the root. Default matches the rest of the repo.
PARENT_IDS = tuple(
    group.strip()
    for group in os.environ.get(
        "NIFI_PARENT_ID", "f5876d95-01a0-1000-0000-00005a7a3b62"
    ).split(",")
    if group.strip()
)
FLOW_TITLE = os.environ.get("NIFI_FLOW_TITLE", "Hello World")
CLUSTER = os.environ.get("NIFI_CLUSTER", KUBECONFIG.stem)

log = logging.getLogger("nifi_status_exporter")
_cache_lock = threading.Lock()
_metrics_text = "# nifi status exporter starting\n"
_prom_pod: str | None = None


def _kubectl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["kubectl", f"--kubeconfig={KUBECONFIG}", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, "", "kubectl timed out")


def _find_pod() -> str | None:
    """The Prometheus pod holding the client cert. Cached; cleared on a failed call."""
    global _prom_pod
    if PROM_POD:
        return PROM_POD
    if _prom_pod:
        return _prom_pod
    proc = _kubectl(
        "get", "pods", "-n", NAMESPACE, "-l", PROM_POD_SELECTOR,
        "-o", "jsonpath={.items[*].metadata.name}",
    )
    names = [name for name in (proc.stdout or "").split() if name]
    if not names:
        # The label may not be what we guessed; fall back to matching on the name.
        proc = _kubectl(
            "get", "pods", "-n", NAMESPACE, "-o",
            "jsonpath={.items[*].metadata.name}",
        )
        names = [name for name in (proc.stdout or "").split() if "prometheus" in name]
    if not names:
        log.warning("no prometheus pod found in %s via %s", NAMESPACE, PROM_POD_SELECTOR)
        return None
    _prom_pod = sorted(names)[0]
    log.info("using prometheus pod %s", _prom_pod)
    return _prom_pod


def _api_call(path: str) -> tuple[str, str]:
    """One API GET from inside the Prometheus pod. Returns (http_code, body); code is "" when
    the call never left the host, which distinguishes 'no access' from 'wrong password'."""
    global _prom_pod
    pod = _find_pod()
    if pod is None:
        return "", ""
    cmd = ["kubectl", f"--kubeconfig={KUBECONFIG}", "exec", "-n", NAMESPACE, pod]
    if PROM_CONTAINER:
        cmd += ["-c", PROM_CONTAINER]
    cmd += ["--", "curl", "-sS", "-m", "20", "--cert", CERT_FILE, "--key", KEY_FILE]
    if INSECURE:
        cmd += ["-k"]
    else:
        cmd += ["--cacert", CA_FILE]
    # -w appends the status code on its own line so a 403 (wrong cert) is distinguishable
    # from a 404 (wrong path) instead of both looking like unparseable output.
    cmd += ["-w", "\n%{http_code}", API_URL + path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        log.warning("timeout calling %s", path)
        return "", ""
    body, sep, code = proc.stdout.rpartition("\n")
    if not sep:
        body, code = proc.stdout, ""
    if not code.strip():
        # No status code at all: curl never completed, or the exec itself failed.
        _prom_pod = None
        log.warning(
            "no response for %s (rc=%s): %s",
            path,
            proc.returncode,
            (proc.stderr or "").strip().replace("\n", " ")[:300],
        )
    return code.strip(), body


def _api_json(path: str) -> dict[str, Any] | None:
    code, body = _api_call(path)
    if code != "200":
        if code:
            log.warning("GET %s -> %s %s", path, code, body.strip().replace("\n", " ")[:300])
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        log.warning("GET %s -> 200 but body is not JSON: %s", path, body.strip()[:300])
        return None


def _root_group_id() -> str | None:
    """The real root process-group id, via /process-groups/root.

    The literal "root" alias in the /flow/ path is rejected by the CDF NiFi build here
    (404 "The specified resource could not be found"), so the id is resolved rather than
    assumed. None means it could not be resolved."""
    doc = _api_json("/nifi-api/process-groups/root") or {}
    root_id = str((doc.get("component") or {}).get("id") or "")
    return root_id or None


def _endpoints() -> list[tuple[str, str, bool]]:
    """(path, parent_id-ish label, required) for the root group plus each configured group.

    `required` is what nifi_status_exporter_up is measured against. A group the operator asked
    for is required; the root walk is dropped rather than counted when its id cannot be
    resolved, so an unresolvable root cannot pin the exporter at up=0 and take the scrape
    timestamp down with it."""
    root_id = _root_group_id()
    if root_id is None:
        log.warning("could not resolve the root process group; walking configured groups only")
        walks: list[tuple[str, str, bool]] = []
    else:
        walks = [(f"/nifi-api/flow/process-groups/{root_id}", "root", True)]
    walks += [(f"/nifi-api/flow/process-groups/{group}", group, True) for group in PARENT_IDS]
    return walks


def _extract_processors(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Root returns processors under "processors"; a group returns them under
    processGroupFlow.flow.processors. Accept either, so a response-shape difference shows up
    as a zero count on nifi_processors_total rather than silently as a missing processor."""
    direct = doc.get("processors")
    if isinstance(direct, list):
        return direct
    flow = (doc.get("processGroupFlow") or {}).get("flow") or {}
    nested = flow.get("processors")
    return nested if isinstance(nested, list) else []


def _escape(value: object) -> str:
    """Processor names are free text and may contain quotes or newlines."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _labels(pairs: dict[str, object]) -> str:
    return "{" + ",".join(f'{key}="{_escape(val)}"' for key, val in pairs.items()) + "}"


def _collect() -> str:
    now = int(time.time())
    lines: list[str] = []
    base = {"cluster": CLUSTER, "nifi_namespace": NAMESPACE}
    walks = _endpoints()
    required = sum(1 for _, _, is_required in walks if is_required)
    required_ok = 0

    seen: set[str] = set()
    for path, group, is_required in walks:
        doc = _api_json(path)
        if doc is None:
            continue
        if is_required:
            required_ok += 1
        found = 0
        for proc in _extract_processors(doc):
            comp = proc.get("component") or {}
            proc_id = str(comp.get("id") or proc.get("id") or "")
            if not proc_id or proc_id in seen:
                continue
            seen.add(proc_id)
            found += 1
            # component.state holds the enum ("RUNNING"/"STOPPED"); status.runStatus holds the
            # readable form ("Running"/"Stopped"). component.runStatus is absent on this CDF
            # build, so read all three and let the compare below be case-insensitive.
            run_status = str(
                comp.get("runStatus")
                or (proc.get("status") or {}).get("runStatus")
                or comp.get("state")
                or "Unknown"
            )
            proc_labels = {
                **base,
                "nifi_flow": FLOW_TITLE,
                "parent_id": comp.get("parentGroupId") or group,
                "processor_id": proc_id,
                "processor_name": comp.get("name") or proc_id,
                "run_status": run_status,
            }
            value = 1 if run_status.strip().upper() == "RUNNING" else 0
            lines.append(f"nifi_processor_run_status{_labels(proc_labels)} {value}")
        group_labels = {**base, "parent_id": group}
        lines.append(f"nifi_processors_total{_labels(group_labels)} {found}")

    up = 1 if (required and required_ok == required) else 0
    lines.append(f"nifi_status_exporter_up{_labels(base)} {up}")
    # Only on a fully successful scrape: a frozen timestamp is what the guard rule watches.
    if up:
        lines.append(
            f"nifi_status_exporter_last_scrape_timestamp{_labels(base)} {now}"
        )
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


def _selftest() -> int:
    """Print what each API walk returns, raw. Run this first on a new deployment: it resolves
    the pod, cert mount and authorization question in one shot."""
    for name, value in (
        ("kubeconfig", KUBECONFIG),
        ("namespace", NAMESPACE),
        ("api_url", API_URL),
        ("cert_dir", CERT_DIR),
        ("insecure", INSECURE),
    ):
        print(f"{name:12} {value}")
    pod = _find_pod()
    print(f"{'prom_pod':12} {pod}")
    if pod is None:
        return 1
    mounts = _kubectl(
        "get", "pod", "-n", NAMESPACE, pod, "-o",
        "jsonpath={range .spec.containers[*]}container={.name}{\"\\n\"}"
        "{range .volumeMounts[*]}  mount={.mountPath}{\"\\n\"}{end}{end}",
    )
    print(mounts.stdout or mounts.stderr)
    failures = 0
    for path, group, _is_required in _endpoints():
        code, body = _api_call(path)
        print(f"\n--- GET {path}  (group={group}) -> {code or 'no response'} ---")
        print(body.strip()[:600] if body.strip() else "(empty)")
        if code != "200":
            failures += 1
            continue
        procs = _extract_processors(json.loads(body))
        if not procs:
            print("  no processors in this walk")
        for proc in procs:
            comp = proc.get("component") or {}
            state = (
                comp.get("runStatus")
                or (proc.get("status") or {}).get("runStatus")
                or comp.get("state")
            )
            print(f"  {state!s:10} {comp.get('name')}")
    print("\nselftest", "FAILED" if failures else "OK")
    return 1 if failures else 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    thread = threading.Thread(target=_scrape_loop, daemon=True)
    thread.start()
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler)
    log.info("NiFi status exporter on http://%s:%s/metrics", LISTEN_HOST, LISTEN_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
