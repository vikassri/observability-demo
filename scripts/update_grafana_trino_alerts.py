#!/usr/bin/env python3
"""Grafana folder, Mailpit contact point, Trino alert rules, and email templates."""
from __future__ import annotations

import json
import os
import urllib.request
import base64

BASE = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
AUTH = base64.b64encode(
    os.environ.get("GRAFANA_AUTH", "admin:admin").encode()
).decode()

TRINO_INSTANCE = os.environ.get("TRINO_INSTANCE", "cle01-trino")
TRINO_CLUSTER = os.environ.get("TRINO_CLUSTER", "cle")
TRINO_NAMESPACE = os.environ.get("TRINO_NAMESPACE", "cle01-trino-f80f2a")
TRINO_EXPECTED_WORKERS = os.environ.get("TRINO_EXPECTED_WORKERS", "2")
TRINO_ES_INDEX = os.environ.get("TRINO_ES_INDEX", "uob-logs-*")
QUERY_FAIL_WINDOW = os.environ.get("TRINO_QUERY_FAIL_WINDOW", "5m")

RULE_UID_COORD_DOWN = os.environ.get("TRINO_RULE_UID_COORD", "trino-coordinator-scrape-down")
RULE_UID_WORKERS = os.environ.get("TRINO_RULE_UID_WORKERS", "trino-workers-scrape-low")
RULE_UID_QUERY_FAIL = os.environ.get("TRINO_RULE_UID_QUERY_FAIL", "trino-query-failed-log")
CONTACT_POINT = os.environ.get("TRINO_CONTACT_POINT", "mailpit-trino")
EMAIL_TEMPLATE = "trino_alert_email"


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
    raise RuntimeError("No Prometheus datasource")


def es_uid() -> str:
    for item in request("GET", "/api/datasources"):
        if item.get("type") == "elasticsearch":
            return item["uid"]
    raise RuntimeError("No Elasticsearch datasource")


def folder_uid() -> str:
    for folder in request("GET", "/api/folders"):
        if folder.get("title") == "Trino":
            return folder["uid"]
    return request("POST", "/api/folders", {"title": "Trino", "uid": "trino-obs"})["uid"]


def threshold_condition(ref: str = "A", op: str = "gt", param: float = 0) -> dict:
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


def prom_rule(
    uid: str,
    title: str,
    expr: str,
    summary: str,
    description: str,
    for_duration: str,
    severity: str,
    alert_kind: str,
    folder: str,
    prom: str,
    threshold_op: str = "gt",
    threshold_param: float = 0,
) -> dict:
    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": prom,
                "model": {
                    "editorMode": "code",
                    "expr": expr,
                    "instant": True,
                    "legendFormat": "__auto",
                    "range": False,
                    "refId": "A",
                },
            },
            threshold_condition("A", threshold_op, threshold_param),
        ],
        "noDataState": "Alerting" if alert_kind == "coordinator-down" else "OK",
        "execErrState": "Alerting",
        "for": for_duration,
        "annotations": {"summary": summary, "description": description},
        "labels": {
            "severity": severity,
            "service": "trino",
            "trino_instance": TRINO_INSTANCE,
            "trino_namespace": TRINO_NAMESPACE,
            "trino_alert_kind": alert_kind,
        },
        "isPaused": False,
        "folderUID": folder,
        "ruleGroup": "trino-health",
    }


def es_log_rule(
    uid: str,
    title: str,
    lucene_query: str,
    summary: str,
    description: str,
    folder: str,
    es: str,
) -> dict:
    window_sec = 300
    if QUERY_FAIL_WINDOW.endswith("m"):
        window_sec = int(QUERY_FAIL_WINDOW[:-1]) * 60
    elif QUERY_FAIL_WINDOW.endswith("h"):
        window_sec = int(QUERY_FAIL_WINDOW[:-1]) * 3600

    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": window_sec, "to": 0},
                "datasourceUid": es,
                "model": {
                    "alias": "",
                    "bucketAggs": [],
                    "datasource": {"type": "elasticsearch", "uid": es},
                    "metrics": [{"id": "1", "type": "count"}],
                    "query": lucene_query,
                    "refId": "A",
                    "timeField": "@timestamp",
                    "index": TRINO_ES_INDEX,
                },
            },
            threshold_condition("A"),
        ],
        "noDataState": "OK",
        "execErrState": "Alerting",
        "for": "1m",
        "annotations": {"summary": summary, "description": description},
        "labels": {
            "severity": "critical",
            "service": "trino",
            "trino_instance": TRINO_INSTANCE,
            "trino_namespace": TRINO_NAMESPACE,
            "trino_alert_kind": "query-failed",
        },
        "isPaused": False,
        "folderUID": folder,
        "ruleGroup": "trino-query-health",
    }


def upsert_contact_point(alert_email: str) -> None:
    cps = request("GET", "/api/v1/provisioning/contact-points")
    if not any(cp.get("name") == CONTACT_POINT for cp in cps):
        request(
            "POST",
            "/api/v1/provisioning/contact-points",
            {
                "name": CONTACT_POINT,
                "type": "email",
                "settings": {
                    "addresses": alert_email,
                    "singleEmail": False,
                    "subject": '{{ template "trino_alert_subject" . }}',
                    "message": '{{ template "trino_alert_email" . }}',
                },
                "disableResolveMessage": False,
            },
        )
        print("contact_point_created", CONTACT_POINT)
    else:
        cp = next(c for c in cps if c.get("name") == CONTACT_POINT)
        settings = dict(cp.get("settings") or {})
        settings["addresses"] = alert_email
        settings["subject"] = '{{ template "trino_alert_subject" . }}'
        settings["message"] = '{{ template "trino_alert_email" . }}'
        request(
            "PUT",
            f"/api/v1/provisioning/contact-points/{cp['uid']}",
            {
                "uid": cp["uid"],
                "name": CONTACT_POINT,
                "type": "email",
                "settings": settings,
                "disableResolveMessage": cp.get("disableResolveMessage", False),
            },
        )
        print("contact_point_updated", CONTACT_POINT)


def upsert_notification_template() -> None:
    body = {
        "template": (
            '{{ define "trino_alert_subject" -}}'
            "{{ range .Alerts }}"
            "{{ if eq .Status \"firing\" }}Trino alert{{ else }}Trino cleared{{ end }}"
            " {{ or .Labels.severity \"unknown\" | toUpper }}"
            " {{ .Labels.trino_instance }}"
            "{{ if eq .Labels.trino_alert_kind \"query-failed\" }} query failed"
            "{{ else if eq .Labels.trino_alert_kind \"coordinator-down\" }} coordinator down"
            "{{ else if eq .Labels.trino_alert_kind \"workers-low\" }} workers down"
            "{{ else }} health"
            "{{ end }}"
            "{{ break }}{{ end }}"
            "{{- end }}\n"
            '{{ define "trino_alert_email" }}'
            "{{ range .Alerts }}"
            "{{ if eq .Status \"firing\" }}STATUS:      FIRING"
            "{{ else }}STATUS:      RECOVERED"
            "{{ end }}\n"
            "Criticality: {{ or .Labels.severity \"unknown\" | toUpper }}\n"
            "{{ if eq .Labels.trino_alert_kind \"query-failed\" }}Type:        SQL query failed (QueryMonitor log)"
            "{{ else if eq .Labels.trino_alert_kind \"coordinator-down\" }}Type:        Coordinator metrics scrape down"
            "{{ else if eq .Labels.trino_alert_kind \"workers-low\" }}Type:        Worker scrape targets below expected"
            "{{ else }}Type:        {{ .Labels.alertname }}"
            "{{ end }}\n"
            "Instance:    {{ .Labels.trino_instance }} ({{ .Labels.trino_namespace }})\n"
            "Problem:     {{ .Annotations.summary }}\n"
            "{{ if eq .Status \"firing\" }}Action:      {{ .Annotations.description }}"
            "{{ else }}Action:      Condition cleared."
            "{{ end }}\n"
            "\n"
            "{{ end }}"
            "{{ end }}"
        ),
    }
    request("PUT", f"/api/v1/provisioning/templates/{EMAIL_TEMPLATE}", body)
    print("updated_email_template", EMAIL_TEMPLATE)


def upsert_notification_policies() -> None:
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
                {
                    "receiver": CONTACT_POINT,
                    "object_matchers": [["service", "=", "trino"]],
                    "continue": False,
                }
            ],
        },
    )
    print("notification_policies_updated")


def main() -> None:
    alert_email = os.environ.get("TRINO_ALERT_EMAIL", "trino-alerts@local.test")
    prom = prom_uid()
    es = es_uid()
    folder = folder_uid()

    upsert_contact_point(alert_email)
    upsert_notification_template()
    upsert_notification_policies()

    coord_expr = (
        f'min(trino_node_up{{trino_role="coordinator", trino_instance="{TRINO_INSTANCE}", '
        f'cluster="{TRINO_CLUSTER}"}}) or on() vector(0)'
    )
    workers_up_expr = (
        f'sum(trino_node_up{{trino_role="worker", trino_instance="{TRINO_INSTANCE}", '
        f'cluster="{TRINO_CLUSTER}"}}) or on() vector(0)'
    )
    expected = int(TRINO_EXPECTED_WORKERS)
    workers_gap_expr = f"{expected} - ({workers_up_expr})"

    rules = [
        prom_rule(
            RULE_UID_COORD_DOWN,
            f"Trino coordinator scrape down ({TRINO_INSTANCE})",
            coord_expr,
            "Trino coordinator /v1/status is unreachable from the hub status exporter.",
            "Check trino-status-exporter, trino-hub-agent, cle kubeconfig, and coordinator pod health.",
            "3m",
            "critical",
            "coordinator-down",
            folder,
            prom,
            threshold_op="lt",
            threshold_param=1,
        ),
        prom_rule(
            RULE_UID_WORKERS,
            f"Trino workers scrape below expected ({TRINO_INSTANCE})",
            workers_gap_expr,
            f"Fewer than {TRINO_EXPECTED_WORKERS} worker metric targets are UP.",
            "Check worker pods in the Trino namespace and trino-hub-agent port-forwards.",
            "3m",
            "warning",
            "workers-low",
            folder,
            prom,
            threshold_op="gt",
            threshold_param=0,
        ),
        es_log_rule(
            RULE_UID_QUERY_FAIL,
            f"Trino query failed ({TRINO_INSTANCE})",
            "log:*QueryMonitor* AND log:*FAILED*",
            "At least one Trino query logged FAILED in QueryMonitor (see Kibana/Grafana logs).",
            "Open Trino UI or coordinator logs for query ID; fix SQL, catalog, or permissions.",
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
        print("upserted", rule["uid"], rule["title"])

    print(
        "trino_instance",
        TRINO_INSTANCE,
        "cluster",
        TRINO_CLUSTER,
        "expected_workers",
        TRINO_EXPECTED_WORKERS,
    )


if __name__ == "__main__":
    main()
