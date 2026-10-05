#!/usr/bin/env python3
"""Grafana Airflow folder, alerts, Mailpit templates."""
from __future__ import annotations

import json
import os
import urllib.request
import base64

BASE = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
AUTH = base64.b64encode(os.environ.get("GRAFANA_AUTH", "admin:admin").encode()).decode()

AIRFLOW_RELEASE = os.environ.get("AIRFLOW_RELEASE", "cwo01-cwo-cw-e94d4f")
AIRFLOW_NAMESPACE = os.environ.get("AIRFLOW_NAMESPACE", "cwo01-cwo-1b56da")
AIRFLOW_CLUSTER = os.environ.get("AIRFLOW_CLUSTER", "cwo")
CONTACT_POINT = os.environ.get("AIRFLOW_CONTACT_POINT", "mailpit-airflow")


def request(method: str, path: str, data: dict | None = None) -> dict:
    headers = {"Authorization": f"Basic {AUTH}"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def prom_uid() -> str:
    for item in request("GET", "/api/datasources"):
        if item.get("type") == "prometheus":
            return item["uid"]
    raise RuntimeError("no prometheus")


def es_uid() -> str:
    for item in request("GET", "/api/datasources"):
        if item.get("type") == "elasticsearch":
            return item["uid"]
    raise RuntimeError("no elasticsearch")


def folder_uid() -> str:
    for f in request("GET", "/api/folders"):
        if f.get("title") == "Airflow":
            return f["uid"]
    return request("POST", "/api/folders", {"title": "Airflow", "uid": "airflow-obs"})["uid"]


def threshold(ref: str = "A", op: str = "gt", param: float = 0) -> dict:
    return {
        "refId": "C",
        "relativeTimeRange": {"from": 0, "to": 0},
        "datasourceUid": "__expr__",
        "model": {
            "conditions": [
                {
                    "evaluator": {"params": [param], "type": op},
                    "operator": {"type": "and"},
                    "query": {"params": ["C"]},
                    "reducer": {"params": [], "type": "last"},
                    "type": "query",
                }
            ],
            "datasource": {"type": "__expr__", "uid": "__expr__"},
            "expression": ref,
            "refId": "C",
            "type": "threshold",
        },
    }


def prom_rule(uid, title, expr, summary, desc, for_d, severity, kind, folder, prom, op="lt", param=1):
    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": prom,
                "model": {"editorMode": "code", "expr": expr, "instant": True, "refId": "A"},
            },
            threshold("A", op, param),
        ],
        "noDataState": "Alerting" if kind == "api" else "OK",
        "execErrState": "Alerting",
        "for": for_d,
        "annotations": {"summary": summary, "description": desc},
        "labels": {
            "severity": severity,
            "service": "airflow",
            "airflow_release": AIRFLOW_RELEASE,
            "airflow_namespace": AIRFLOW_NAMESPACE,
            "airflow_alert_kind": kind,
        },
        "isPaused": False,
        "folderUID": folder,
        "ruleGroup": "airflow-health",
    }


def es_rule(uid, title, query, summary, desc, folder, es):
    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": es,
                "model": {
                    "metrics": [{"id": "1", "type": "count"}],
                    "query": query,
                    "refId": "A",
                    "timeField": "@timestamp",
                    "index": "uob-logs-*",
                },
            },
            threshold("A", "gt", 0),
        ],
        "noDataState": "OK",
        "execErrState": "Alerting",
        "for": "1m",
        "annotations": {"summary": summary, "description": desc},
        "labels": {
            "severity": "critical",
            "service": "airflow",
            "airflow_release": AIRFLOW_RELEASE,
            "airflow_namespace": AIRFLOW_NAMESPACE,
            "airflow_alert_kind": "dag-failed",
        },
        "isPaused": False,
        "folderUID": folder,
        "ruleGroup": "airflow-jobs",
    }


def main() -> None:
    email = os.environ.get("AIRFLOW_ALERT_EMAIL", "airflow-alerts@local.test")
    prom = prom_uid()
    es = es_uid()
    folder = folder_uid()
    base = f'airflow_release="{AIRFLOW_RELEASE}", cluster="{AIRFLOW_CLUSTER}"'

    cps = request("GET", "/api/v1/provisioning/contact-points")
    if not any(c.get("name") == CONTACT_POINT for c in cps):
        request(
            "POST",
            "/api/v1/provisioning/contact-points",
            {
                "name": CONTACT_POINT,
                "type": "email",
                "settings": {
                    "addresses": email,
                    "subject": '{{ template "airflow_alert_subject" . }}',
                    "message": '{{ template "airflow_alert_email" . }}',
                },
            },
        )

    request(
        "PUT",
        "/api/v1/provisioning/templates/airflow_alert_email",
        {
            "template": (
                '{{ define "airflow_alert_subject" -}}'
                "{{ range .Alerts }}Airflow alert {{ .Labels.severity }} {{ .Labels.airflow_release }}{{ break }}{{ end }}"
                "{{- end }}\n"
                '{{ define "airflow_alert_email" }}{{ range .Alerts }}'
                "STATUS: {{ .Status }}\nProblem: {{ .Annotations.summary }}\nAction: {{ .Annotations.description }}\n{{ end }}{{ end }}"
            )
        },
    )

    request(
        "PUT",
        "/api/v1/provisioning/policies",
        {
            "receiver": "mailpit-nifi",
            "group_by": ["grafana_folder", "alertname"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
            "routes": [
                {"receiver": CONTACT_POINT, "object_matchers": [["service", "=", "airflow"]]},
                {"receiver": "mailpit-trino", "object_matchers": [["service", "=", "trino"]]},
            ],
        },
    )

    rules = [
        prom_rule(
            "airflow-scheduler-unhealthy",
            f"Airflow scheduler unhealthy ({AIRFLOW_RELEASE})",
            f'min(airflow_component_healthy{{{base},component="scheduler"}}) or vector(0)',
            "Airflow scheduler health check is not healthy.",
            "Check scheduler pods and /api/v2/monitor/health on the api-server.",
            "3m",
            "critical",
            "scheduler",
            folder,
            prom,
            "lt",
            1,
        ),
        prom_rule(
            "airflow-api-down",
            f"Airflow API unreachable ({AIRFLOW_RELEASE})",
            f'min(airflow_api_reachable{{{base}}}) or vector(0)',
            "Cannot reach Airflow api-server /api/v2 from hub.",
            "Check cwo kubeconfig, api-server pod, and airflow-status-exporter.",
            "3m",
            "critical",
            "api",
            folder,
            prom,
            "lt",
            1,
        ),
        es_rule(
            "airflow-dagrun-failed-log",
            f"Airflow DAG run failed ({AIRFLOW_RELEASE})",
            'log:*DagRun* AND log:*Finished* AND log:*state=failed*',
            "Scheduler logged a failed DAG run (see Airflow UI).",
            "Open Airflow UI → DAG run details for stack trace and task logs.",
            folder,
            es,
        ),
        es_rule(
            "airflow-task-failed-log",
            f"Airflow task failed ({AIRFLOW_RELEASE})",
            'log:*TaskInstance* AND log:*Finished* AND log:*state=failed*',
            "Scheduler logged a failed task instance.",
            "Check task logs in Airflow UI or S3 remote logs.",
            folder,
            es,
        ),
    ]

    existing = {r["uid"] for r in request("GET", "/api/v1/provisioning/alert-rules")}
    for rule in rules:
        if rule["uid"] in existing:
            request("PUT", f"/api/v1/provisioning/alert-rules/{rule['uid']}", rule)
        else:
            request("POST", "/api/v1/provisioning/alert-rules", rule)
        print("upserted", rule["uid"])


if __name__ == "__main__":
    main()
