#!/usr/bin/env python3
"""
Stream Kubernetes pod console logs (kubectl logs -f) to OpenTelemetry Collector via OTLP HTTP.

Run on the observability host where kubeconfig and otelcol-contrib (:4318) already exist.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from opentelemetry._logs import SeverityNumber, set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

# kubectl logs --timestamps prefix: 2026-10-01T09:00:00.123456789Z
TS_PREFIX = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(?P<body>.*)$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="kubectl logs → OTLP (local OTel gateway)")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://127.0.0.1:4318/v1/logs"),
        help="OTLP HTTP logs endpoint (default local otelcol-contrib)",
    )
    parser.add_argument(
        "--kubeconfig",
        default=os.environ.get("KUBECONFIG", "/etc/prometheus/kubeconfig/cdf.yaml"),
    )
    parser.add_argument("-n", "--namespace", default=os.environ.get("NIFI_NAMESPACE", "dfx-nifi-deployment-ns"))
    parser.add_argument("--pod", default=os.environ.get("NIFI_POD", "dfx-nifi-0"))
    parser.add_argument(
        "-c",
        "--container",
        default=os.environ.get("NIFI_LOG_CONTAINER", "nifi"),
        help="Container name (use empty string with --all-containers)",
    )
    parser.add_argument(
        "--all-containers",
        action="store_true",
        help="Follow all containers in the pod (ignores --container)",
    )
    parser.add_argument("--prefix", action="store_true", default=True, help="Prefix lines with container name")
    parser.add_argument(
        "--service-name",
        default=os.environ.get("OTEL_SERVICE_NAME", ""),
        help="Override service.name on log records (default: nifi-* heuristic)",
    )
    parser.add_argument("--product", default=os.environ.get("OTEL_PRODUCT", ""))
    parser.add_argument("--cluster", default=os.environ.get("OTEL_CLUSTER", ""))
    parser.add_argument("--trino-instance", default=os.environ.get("TRINO_INSTANCE", ""))
    parser.add_argument("--airflow-release", default=os.environ.get("AIRFLOW_RELEASE", ""))
    parser.add_argument("--spark-app", default=os.environ.get("SPARK_APPLICATION", ""))
    parser.add_argument("--flink-app", default=os.environ.get("FLINK_APP", ""))
    parser.add_argument(
        "--since",
        default=os.environ.get("KUBECTL_LOGS_SINCE", "24h"),
        help="Only tail logs from this window (kubectl logs --since), then follow",
    )
    return parser.parse_args()


def build_logger(
    endpoint: str,
    namespace: str,
    pod: str,
    container: str,
    service_name: str = "",
    product: str = "",
    cluster: str = "",
    trino_instance: str = "",
    airflow_release: str = "",
    spark_app: str = "",
    flink_app: str = "",
):
    if service_name:
        svc = service_name
        telemetry_source = "kubernetes-console"
    elif (container or "") == "app-log":
        svc = "nifi-app-log"
        telemetry_source = "kubernetes-app-log-sidecar"
    else:
        svc = "nifi-kubectl-logs"
        telemetry_source = "kubernetes-console"

    attrs: dict[str, str] = {
        "service.name": svc,
        "telemetry.source": telemetry_source,
        "k8s.namespace.name": namespace,
        "k8s.pod.name": pod,
        "k8s.container.name": container or "all",
    }
    if product:
        attrs["product"] = product
    if cluster:
        attrs["cluster"] = cluster
    if trino_instance:
        attrs["trino.instance"] = trino_instance
    if airflow_release:
        attrs["airflow.release"] = airflow_release
    if spark_app:
        attrs["spark.application"] = spark_app
    if flink_app:
        attrs["flink.app"] = flink_app

    resource = Resource.create(attrs)
    provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(endpoint=endpoint)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)
    return provider, provider.get_logger("kubectl_logs_to_otel")


def kubectl_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "logs",
        "-f",
        args.pod,
        "-n",
        args.namespace,
        "--timestamps",
    ]
    if args.all_containers:
        cmd.append("--all-containers=true")
        if args.prefix:
            cmd.append("--prefix=true")
    elif args.container:
        cmd.extend(["-c", args.container])
    if args.since:
        cmd.extend(["--since", args.since])
    return cmd


def parse_line(raw_line: str) -> tuple[datetime | None, str]:
    line = raw_line.rstrip("\n")
    match = TS_PREFIX.match(line)
    if not match:
        return None, line
    ts_text = match.group("ts")
    body = match.group("body")
    if ts_text.endswith("Z"):
        ts_text = ts_text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(ts_text)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        ts = None
    return ts, body


def stream_logs(args: argparse.Namespace) -> int:
    cmd = kubectl_cmd(args)
    _provider, logger = build_logger(
        args.endpoint,
        args.namespace,
        args.pod,
        "" if args.all_containers else args.container,
        service_name=args.service_name or "",
        product=args.product or "",
        cluster=args.cluster or "",
        trino_instance=args.trino_instance or "",
        airflow_release=args.airflow_release or "",
        spark_app=args.spark_app or "",
        flink_app=args.flink_app or "",
    )

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.info("Starting: %s", " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if proc.stdout is None:
        return 1

    for raw in proc.stdout:
        if not raw.strip():
            continue
        observed_ts, body = parse_line(raw)
        if args.product:
            tag = f"[uob.product={args.product}]"
            if args.cluster:
                tag += f"[uob.cluster={args.cluster}]"
            if tag not in body:
                body = f"{tag} {body}"
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
        ts_ns = int(observed_ts.timestamp() * 1_000_000_000) if observed_ts else now_ns
        log_attrs: dict[str, str] = {}
        if args.product:
            log_attrs["product"] = args.product
        if args.cluster:
            log_attrs["cluster"] = args.cluster
        if args.trino_instance:
            log_attrs["trino.instance"] = args.trino_instance
        if args.airflow_release:
            log_attrs["airflow.release"] = args.airflow_release
        log_attrs["k8s.namespace.name"] = args.namespace
        log_attrs["k8s.pod.name"] = args.pod
        log_attrs["k8s.container.name"] = args.container or "all"

        logger.emit(
            body=body,
            timestamp=ts_ns,
            observed_timestamp=now_ns,
            severity_number=SeverityNumber.INFO,
            severity_text="INFO",
            attributes=log_attrs,
        )

    return proc.wait()


def main() -> None:
    args = parse_args()
    try:
        code = stream_logs(args)
    except KeyboardInterrupt:
        code = 0
    except FileNotFoundError:
        print("kubectl not found in PATH", file=sys.stderr)
        code = 127
    sys.exit(code)


if __name__ == "__main__":
    main()
