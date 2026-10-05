#!/usr/bin/env python3
import json
import os
import urllib.request
import base64

BASE = "http://127.0.0.1:3000"
AUTH = base64.b64encode(b"admin:admin").decode()

# Which NiFi flow (process group) to monitor — see README "NiFi metrics and alerts"
BULLETIN_WINDOW = os.environ.get("NIFI_BULLETIN_WINDOW", "5m")
NIFI_JOB = os.environ.get("NIFI_PROM_JOB", "dfx-nifi-web")
NIFI_PARENT_ID = os.environ.get(
    "NIFI_PARENT_ID", "f5876d95-01a0-1000-0000-00005a7a3b62"
)
NIFI_FLOW_TITLE = os.environ.get("NIFI_FLOW_TITLE", "Hello World")
NIFI_FLOW_SLUG = os.environ.get("NIFI_FLOW_SLUG", "hello-world")
NIFI_K8S_NAMESPACE = os.environ.get("NIFI_K8S_NAMESPACE", "dfx-nifi-deployment-ns")
RULE_UID_STALLED = os.environ.get("NIFI_RULE_UID_STALLED", "nifi-proc-stalled")
RULE_UID_BULLETIN = os.environ.get("NIFI_RULE_UID_BULLETIN", "nifi-proc-bulletin")
RULE_UID_TERMINATED = os.environ.get("NIFI_RULE_UID_TERMINATED", "nifi-proc-thread-terminated")
RULE_UID_COUNTER = os.environ.get("NIFI_RULE_UID_COUNTER", "nifi-proc-failure-counter")


def request(method, path, data=None):
    headers = {"Authorization": f"Basic {AUTH}"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


prom_uid = next(d["uid"] for d in request("GET", "/api/datasources") if d["type"] == "prometheus")
folder_uid = next((f["uid"] for f in request("GET", "/api/folders") if f["title"] == "NiFi"), "nifi-obs")


def payload(
    uid,
    title,
    expr,
    summary,
    description,
    for_duration="1m",
    severity: str = "warning",
    alert_kind: str = "general",
):
    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": prom_uid,
                "model": {
                    "editorMode": "code",
                    "expr": expr,
                    "instant": True,
                    "legendFormat": "__auto",
                    "range": False,
                    "refId": "A",
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "conditions": [
                        {
                            "evaluator": {"params": [0], "type": "gt"},
                            "operator": {"type": "and"},
                            "query": {"params": ["C"]},
                            "reducer": {"params": [], "type": "last"},
                            "type": "query",
                        }
                    ],
                    "datasource": {"type": "__expr__", "uid": "__expr__"},
                    "expression": "A",
                    "refId": "C",
                    "type": "threshold",
                },
            },
        ],
        "noDataState": "OK",
        "execErrState": "Alerting",
        "for": for_duration,
        "annotations": {"summary": summary, "description": description},
        "labels": {
            "severity": severity,
            "service": "nifi",
            "nifi_flow": NIFI_FLOW_SLUG,
            "nifi_namespace": NIFI_K8S_NAMESPACE,
            "nifi_alert_kind": alert_kind,
        },
        "isPaused": False,
        "folderUID": folder_uid,
        "ruleGroup": "nifi-processor-health",
    }


processor_scope = (
    f'job="{NIFI_JOB}", component_type="Processor", parent_id="{NIFI_PARENT_ID}"'
)
bulletin_scope = (
    f'job="{NIFI_JOB}", parent_id="{NIFI_PARENT_ID}", level=~"WARNING|ERROR"'
)
bulletin = (
    "sum by (source_name, source_id, level, instance) "
    f"(increase(nifi_bulletin{{{bulletin_scope}}}[{BULLETIN_WINDOW}]))"
)
terminated = (
    "sum by (component_name, component_id, instance) "
    f"(increase(nifi_amount_threads_terminated{{{processor_scope}}}[5m]))"
)
counter = (
    "sum by (processor_name, processor_id, counter_name, instance) "
    f'(increase(nifi_processor_counters{{job="{NIFI_JOB}", counter_name=~"(?i).*(fail|error).*"}}[5m]))'
)
bulletin_summary = "NiFi reported a warning or error on this processor (bulletin)."
bulletin_description = (
    "Open NiFi → Bulletin Board for details; fix validation, I/O, or configuration."
)
terminated_summary = "A processor thread terminated unexpectedly (likely a crash)."
terminated_description = (
    "Check Bulletin Board and app logs for stack traces; restart the processor if needed."
)
counter_summary = "A failure or error counter increased on a processor."
counter_description = (
    "Inspect failure queues, relationships, and bulletins for that processor."
)

rules = [
    payload(
        RULE_UID_BULLETIN,
        f"NiFi processor bulletin ({NIFI_FLOW_TITLE})",
        bulletin,
        bulletin_summary,
        bulletin_description,
        "2m",
        severity="warning",
        alert_kind="bulletin",
    ),
    payload(
        RULE_UID_TERMINATED,
        f"NiFi processor thread terminated ({NIFI_FLOW_TITLE})",
        terminated,
        terminated_summary,
        terminated_description,
        "1m",
        severity="critical",
        alert_kind="thread-terminated",
    ),
    payload(
        RULE_UID_COUNTER,
        f"NiFi processor failure counter ({NIFI_FLOW_TITLE})",
        counter,
        counter_summary,
        counter_description,
        "1m",
        severity="critical",
        alert_kind="failure-counter",
    ),
]

NIFI_EMAIL_TEMPLATE = "nifi_alert_email"


def upsert_notification_template():
    # Subject + body templates (plain text for Mailpit / email clients).
    body = {
        "template": (
            '{{ define "nifi_alert_subject" -}}'
            "{{ range .Alerts }}"
            "{{ if eq .Status \"firing\" }}NiFi alert{{ else }}NiFi cleared{{ end }}"
            " {{ or .Labels.severity \"unknown\" | toUpper }}"
            " {{ .Labels.nifi_flow }}"
            "{{ if eq .Labels.nifi_alert_kind \"bulletin\" }} bulletin"
            "{{ else if eq .Labels.nifi_alert_kind \"thread-terminated\" }} crash"
            "{{ else if eq .Labels.nifi_alert_kind \"failure-counter\" }} failures"
            "{{ else }} alert"
            "{{ end }}"
            "{{ break }}{{ end }}"
            "{{- end }}\n"
            '{{ define "nifi_alert_email" }}'
            "{{ range .Alerts }}"
            "{{ if eq .Status \"firing\" }}STATUS:      FIRING"
            "{{ else }}STATUS:      RECOVERED"
            "{{ end }}\n"
            "Criticality: {{ or .Labels.severity \"unknown\" | toUpper }}\n"
            "{{ if eq .Labels.nifi_alert_kind \"bulletin\" }}Type:        Processor warning or error (bulletin)"
            "{{ else if eq .Labels.nifi_alert_kind \"thread-terminated\" }}Type:        Processor crashed (thread terminated)"
            "{{ else if eq .Labels.nifi_alert_kind \"failure-counter\" }}Type:        Failure counter increased"
            "{{ else }}Type:        {{ .Labels.alertname }}"
            "{{ end }}\n"
            "Flow:        {{ .Labels.nifi_flow }} (namespace {{ .Labels.nifi_namespace }})\n"
            "{{ if .Labels.level }}Bulletin:    {{ .Labels.level }}\n{{ end }}"
            "Processor:   {{ or .Labels.component_name .Labels.source_name .Labels.processor_name \"—\" }}\n"
            "Problem:     {{ .Annotations.summary }}\n"
            "{{ if eq .Status \"firing\" }}Action:      {{ .Annotations.description }}"
            "{{ else }}Action:      No action needed — condition cleared."
            "{{ end }}\n"
            "Ref ID:      {{ or .Labels.component_id .Labels.processor_id \"—\" }}\n"
            "\n"
            "{{ end }}"
            "{{ end }}"
        ),
    }
    request("PUT", f"/api/v1/provisioning/templates/{NIFI_EMAIL_TEMPLATE}", body)


def update_mailpit_contact_point_message():
    cps = request("GET", "/api/v1/provisioning/contact-points")
    cp = next((c for c in cps if c.get("name") == "mailpit-nifi"), None)
    if not cp:
        return
    uid = cp["uid"]
    settings = dict(cp.get("settings") or {})
    settings["subject"] = '{{ template "nifi_alert_subject" . }}'
    settings["message"] = '{{ template "nifi_alert_email" . }}'
    request(
        "PUT",
        f"/api/v1/provisioning/contact-points/{uid}",
        {
            "uid": uid,
            "name": cp["name"],
            "type": "email",
            "settings": settings,
            "disableResolveMessage": cp.get("disableResolveMessage", False),
        },
    )

upsert_notification_template()
update_mailpit_contact_point_message()
print("updated_email_template", NIFI_EMAIL_TEMPLATE)

existing = {r["uid"] for r in request("GET", "/api/v1/provisioning/alert-rules")}
for rule in rules:
    if rule["uid"] in existing:
        request("PUT", f"/api/v1/provisioning/alert-rules/{rule['uid']}", rule)
    else:
        request("POST", "/api/v1/provisioning/alert-rules", rule)
    print("upserted", rule["uid"], rule["title"])

if RULE_UID_STALLED in existing:
    request("DELETE", f"/api/v1/provisioning/alert-rules/{RULE_UID_STALLED}")
    print("deleted", RULE_UID_STALLED, "(idle / no-flowfile rule removed)")

print(
    "flow",
    NIFI_FLOW_TITLE,
    "parent_id",
    NIFI_PARENT_ID,
    "bulletin_window",
    BULLETIN_WINDOW,
)
