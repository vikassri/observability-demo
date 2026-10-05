#!/usr/bin/env python3
"""Shared Grafana unified-alerting helpers, and the single source of truth for alert routing.

Why this file exists: each service setup script (12 NiFi, 14 Trino, 16 Airflow) used to PUT
its *own* complete notification-policy tree with the root receiver hardcoded to the NiFi
contact point. Whichever script ran last therefore dropped the other services' routes, and
everything falling through to the root was mailed to NiFi's address with NiFi's template.

Now REGISTRY below is the one place that says which alert rules exist, and sync_routing()
builds the whole tree from it. Every alert rule gets its own contact point and its own
address (<rule-uid>@<domain>), and because any of the three scripts PUTs a byte-identical
tree covering all services, they can be run in any order and repeated safely.

Environment:
  GRAFANA_URL, GRAFANA_AUTH   Grafana endpoint (default http://127.0.0.1:3000, admin:admin)
  ALERT_DOMAIN                mail domain for the per-rule addresses (default local.test)
  ALERT_FALLBACK              local part of the catch-all contact point (default unrouted)
  DRY_RUN=1                   print write payloads instead of sending them
  PRUNE_LEGACY=1              once the tree is in place, delete the legacy per-service
                              contact points and templates (see prune_legacy)

The rule uid overrides each setup script already accepted (NIFI_RULE_UID_*, TRINO_RULE_UID_*,
AIRFLOW_RULE_UID_*) are read here too, so the registry and the rules stay in step — but an
override must be exported for *all three* scripts, or the trees they build will disagree.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
AUTH = base64.b64encode(os.environ.get("GRAFANA_AUTH", "admin:admin").encode()).decode()

_DISABLED = ("", "0", "false", "no")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() not in _DISABLED
PRUNE_LEGACY = os.environ.get("PRUNE_LEGACY", "").lower() not in _DISABLED

ALERT_DOMAIN = os.environ.get("ALERT_DOMAIN", "local.test")
FALLBACK_CP = "mailpit-unrouted"
TEMPLATE_OBJECT = "uob_alert_templates"

LEGACY_TEMPLATES = ("nifi_alert_email", "trino_alert_email", "airflow_alert_email")

# The alert rules that exist, per service: (uid, kind). `kind` is what the rule carries as
# its unified `alert_kind` label and what the route below matches on.
REGISTRY: dict[str, list[tuple[str, str]]] = {
    "nifi": [
        (os.environ.get("NIFI_RULE_UID_BULLETIN", "nifi-proc-bulletin"), "bulletin"),
        (
            os.environ.get("NIFI_RULE_UID_TERMINATED", "nifi-proc-thread-terminated"),
            "thread-terminated",
        ),
        (os.environ.get("NIFI_RULE_UID_COUNTER", "nifi-proc-failure-counter"), "failure-counter"),
        (
            os.environ.get("NIFI_RULE_UID_QUEUE_STALLED", "nifi-conn-queue-stalled"),
            "queue-stalled",
        ),
    ],
    "trino": [
        (
            os.environ.get("TRINO_RULE_UID_COORD", "trino-coordinator-scrape-down"),
            "coordinator-down",
        ),
        (os.environ.get("TRINO_RULE_UID_WORKERS", "trino-workers-scrape-low"), "workers-low"),
        (os.environ.get("TRINO_RULE_UID_QUERY_FAIL", "trino-query-failed-log"), "query-failed"),
    ],
    "airflow": [
        (
            os.environ.get("AIRFLOW_RULE_UID_SCHEDULER", "airflow-scheduler-unhealthy"),
            "scheduler",
        ),
        (os.environ.get("AIRFLOW_RULE_UID_API", "airflow-api-down"), "api"),
        (
            os.environ.get("AIRFLOW_RULE_UID_DAGRUN_FAILED", "airflow-dagrun-failed-log"),
            "dag-failed",
        ),
        (
            os.environ.get("AIRFLOW_RULE_UID_TASK_FAILED", "airflow-task-failed-log"),
            "task-failed",
        ),
    ],
}

def _validate_registry() -> None:
    """Routes match on (service, kind), so a repeated uid or kind would silently collapse
    two rules onto one route (the second route never gets a turn: continue is False)."""
    seen_uid: dict[str, str] = {}
    for service, entries in REGISTRY.items():
        kinds: set[str] = set()
        for uid, kind in entries:
            if uid in seen_uid:
                raise ValueError(f"rule uid {uid!r} appears twice in REGISTRY")
            if kind in kinds:
                raise ValueError(
                    f"kind {kind!r} is used by two {service} rules — their routes would collide"
                )
            seen_uid[uid] = service
            kinds.add(kind)


_validate_registry()


# One generic template for every service — no per-service prose and no runtime env, so any# script can provision it and no service depends on another having run first. Per-service
# values come from the labels with an `or` chain.
EMAIL_TEMPLATE_TEXT = (
    '{{ define "uob_alert_subject" -}}'
    "{{ range .Alerts }}"
    '{{ if eq .Status "firing" }}[FIRING]{{ else }}[RECOVERED]{{ end }}'
    ' {{ or .Labels.severity "unknown" | toUpper }}'
    " {{ .Labels.alertname }}"
    "{{ break }}{{ end }}"
    "{{- end }}\n"
    '{{ define "uob_alert_email" }}'
    "{{ range .Alerts }}"
    '{{ if eq .Status "firing" }}STATUS: FIRING{{ else }}STATUS: RECOVERED{{ end }}\n'
    'Severity: {{ or .Labels.severity "unknown" | toUpper }}\n'
    "Service: {{ .Labels.service }}\n"
    "Kind: {{ .Labels.alert_kind }}\n"
    'Entity: {{ or .Labels.entity .Labels.nifi_flow .Labels.trino_instance'
    ' .Labels.airflow_release "—" }}\n'
    'Namespace: {{ or .Labels.nifi_namespace .Labels.trino_namespace'
    ' .Labels.airflow_namespace "—" }}\n'
    "Folder: {{ .Labels.grafana_folder }}\n"
    "Problem: {{ .Annotations.summary }}\n"
    '{{ if eq .Status "firing" }}Action: {{ .Annotations.description }}'
    "{{ else }}Action: No action needed, condition cleared."
    "{{ end }}\n"
    "Ref: {{ or .Labels.destination_name .Labels.component_name .Labels.source_name"
    ' .Labels.processor_name .Labels.instance "—" }}\n'
    "\n"
    "{{ end }}"
    "{{ end }}"
)


def rule_uid(service: str, kind: str) -> str:
    """The configured uid of one alert rule — keeps scripts and REGISTRY from drifting."""
    for uid, entry_kind in REGISTRY[service]:
        if entry_kind == kind:
            return uid
    raise KeyError(f"no {service} rule of kind {kind!r} in REGISTRY")


def alert_address(uid: str) -> str:
    return f"{uid}@{ALERT_DOMAIN}"


def contact_point_name(uid: str) -> str:
    return f"mailpit-{uid}"


def request(method: str, path: str, data: dict | None = None, *, ok_missing: bool = False) -> dict:
    """Grafana API call. In DRY_RUN mode writes are printed and skipped; reads still run."""
    if DRY_RUN and method in ("POST", "PUT", "DELETE"):
        print(f"[dry-run] {method} {path}")
        if data is not None:
            print(json.dumps(data, indent=2))
        return {}
    headers = {"Authorization": f"Basic {AUTH}"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if ok_missing and exc.code == 404:
            return {}
        print(
            f"grafana {method} {path} failed: {exc.code} {exc.read().decode(errors='replace')[:800]}",
            file=sys.stderr,
        )
        raise


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


def folder_uid(title: str, fallback_uid: str) -> str:
    for folder in request("GET", "/api/folders"):
        if folder.get("title") == title:
            return folder["uid"]
    if DRY_RUN:
        print(f"[dry-run] POST /api/folders {{'title': {title!r}, 'uid': {fallback_uid!r}}}")
        return fallback_uid
    return request("POST", "/api/folders", {"title": title, "uid": fallback_uid})["uid"]


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


def alert_labels(
    service: str, severity: str, alert_kind: str, extra: dict | None = None
) -> dict:
    """Common labels. `alert_kind` is the unified kind the notification routes match on;
    each service also keeps its own <service>_alert_kind label for readability."""
    labels = {"severity": severity, "service": service, "alert_kind": alert_kind}
    if extra:
        labels.update(extra)
    return labels


def prom_rule(
    *,
    uid: str,
    title: str,
    expr: str,
    summary: str,
    description: str,
    for_duration: str,
    severity: str,
    alert_kind: str,
    service: str,
    folder: str,
    prom: str,
    rule_group: str,
    threshold_op: str = "gt",
    threshold_param: float = 0,
    no_data_state: str = "OK",
    labels: dict | None = None,
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
        "noDataState": no_data_state,
        "execErrState": "Alerting",
        "for": for_duration,
        "annotations": {"summary": summary, "description": description},
        "labels": alert_labels(service, severity, alert_kind, labels),
        "isPaused": False,
        "folderUID": folder,
        "ruleGroup": rule_group,
    }


def es_log_rule(
    *,
    uid: str,
    title: str,
    query: str,
    summary: str,
    description: str,
    severity: str,
    alert_kind: str,
    service: str,
    folder: str,
    es: str,
    rule_group: str,
    index: str,
    window_sec: int = 300,
    time_field: str = "@timestamp",
    no_data_state: str = "OK",
    labels: dict | None = None,
) -> dict:
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
                    "query": query,
                    "refId": "A",
                    "timeField": time_field,
                    "index": index,
                },
            },
            threshold_condition("A"),
        ],
        "noDataState": no_data_state,
        "execErrState": "Alerting",
        "for": "1m",
        "annotations": {"summary": summary, "description": description},
        "labels": alert_labels(service, severity, alert_kind, labels),
        "isPaused": False,
        "folderUID": folder,
        "ruleGroup": rule_group,
    }


def upsert_rules(rules: list[dict]) -> None:
    existing = {r["uid"] for r in request("GET", "/api/v1/provisioning/alert-rules")}
    for rule in rules:
        if rule["uid"] in existing:
            request("PUT", f"/api/v1/provisioning/alert-rules/{rule['uid']}", rule)
        else:
            request("POST", "/api/v1/provisioning/alert-rules", rule)
        print("upserted", rule["uid"], rule["title"])


def desired_policy_tree() -> dict:
    """The complete tree. Built from REGISTRY in a fixed order, so every script that runs
    this PUTs exactly the same bytes — that is what makes the scripts order-independent."""
    routes = [
        {
            "receiver": contact_point_name(uid),
            "object_matchers": [["service", "=", service], ["alert_kind", "=", kind]],
            "continue": False,
        }
        for service, entries in REGISTRY.items()
        for uid, kind in entries
    ]
    return {
        "receiver": FALLBACK_CP,
        "group_by": ["grafana_folder", "alertname"],
        "group_wait": "30s",
        "group_interval": "5m",
        "repeat_interval": "4h",
        "routes": routes,
    }


def ensure_contact_point(name: str, address: str) -> None:
    """Create or update one Mailpit contact point, using the shared generic templates."""
    settings = {
        "addresses": address,
        "singleEmail": True,
        "subject": '{{ template "uob_alert_subject" . }}',
        "message": '{{ template "uob_alert_email" . }}',
    }
    cps = request("GET", "/api/v1/provisioning/contact-points")
    cp = next((c for c in cps if c.get("name") == name), None)
    if cp is None:
        request(
            "POST",
            "/api/v1/provisioning/contact-points",
            {
                "name": name,
                "type": "email",
                "settings": settings,
                "disableResolveMessage": False,
            },
        )
        print("contact_point_created", name, address)
        return
    # Keep only the keys we own, so a contact point created by an older version of these
    # scripts (different address, singleEmail, template names) is corrected rather than merged.
    merged = {k: v for k, v in (cp.get("settings") or {}).items() if k in settings}
    merged.update(settings)
    if merged == (cp.get("settings") or {}):
        print("contact_point_ok", name, address)
        return
    request(
        "PUT",
        f"/api/v1/provisioning/contact-points/{cp['uid']}",
        {
            "uid": cp["uid"],
            "name": name,
            "type": "email",
            "settings": merged,
            "disableResolveMessage": cp.get("disableResolveMessage", False),
        },
    )
    print("contact_point_updated", name, address)


def check_registry_drift() -> None:
    known = {uid for entries in REGISTRY.values() for uid, _ in entries}
    rules = request("GET", "/api/v1/provisioning/alert-rules")
    existing = {r["uid"] for r in rules}
    for uid in sorted(known - existing):
        print(f"warning: {uid} is in REGISTRY but not provisioned — run its setup script")
    for rule in rules:
        service = (rule.get("labels") or {}).get("service")
        if service in REGISTRY and rule["uid"] not in known:
            print(
                f"warning: rule {rule['uid']} (service {service}) is not in REGISTRY — "
                f"its alerts fall through to {FALLBACK_CP}"
            )


def prune_legacy(tree: dict) -> None:
    """Opt-in cleanup: delete every `mailpit-*` contact point the tree just written does not
    reference (the legacy per-service ones, and any orphan left by a changed rule uid), plus
    the per-service templates. Runs only after the policies PUT, and never deletes a contact
    point the tree still references — in particular not the catch-all when it is the root."""
    keep = {r["receiver"] for r in tree["routes"]} | {tree["receiver"]}
    for cp in request("GET", "/api/v1/provisioning/contact-points"):
        name = cp.get("name", "")
        if not name.startswith("mailpit-") or name in keep:
            continue
        request(
            "DELETE",
            f"/api/v1/provisioning/contact-points/{cp['uid']}",
            ok_missing=True,
        )
        print("pruned_contact_point", name)
    for name in LEGACY_TEMPLATES:
        request("DELETE", f"/api/v1/provisioning/templates/{name}", ok_missing=True)
        print("pruned_template", name)


def _note_legacy_email_env() -> None:
    """The old per-service recipient env vars no longer decide the address."""
    for service in REGISTRY:
        var = f"{service.upper()}_ALERT_EMAIL"
        value = os.environ.get(var)
        if value and value != f"{service}-alerts@{ALERT_DOMAIN}":
            print(
                f"note: {var}={value} is ignored — each rule now mails "
                f"<rule-uid>@{ALERT_DOMAIN} (set ALERT_DOMAIN to change the domain)",
                file=sys.stderr,
            )


def delete_rules(uids) -> None:
    """Delete rules a service no longer defines (e.g. it dropped one from REGISTRY)."""
    existing = {r["uid"] for r in request("GET", "/api/v1/provisioning/alert-rules")}
    for uid in uids:
        if uid in existing:
            request("DELETE", f"/api/v1/provisioning/alert-rules/{uid}", ok_missing=True)
            print("deleted_rule", uid)


def sync_routing() -> dict:
    """Provision templates, one contact point per alert rule, the catch-all, and the tree.

    Contact points go first because the policies PUT is validated against receiver names.
    """
    _note_legacy_email_env()
    request("PUT", f"/api/v1/provisioning/templates/{TEMPLATE_OBJECT}", {"template": EMAIL_TEMPLATE_TEXT})
    print("updated_email_template", TEMPLATE_OBJECT)

    for service, entries in REGISTRY.items():
        for uid, _kind in entries:
            ensure_contact_point(contact_point_name(uid), alert_address(uid))
    ensure_contact_point(FALLBACK_CP, f"unrouted@{ALERT_DOMAIN}")

    tree = desired_policy_tree()
    request("PUT", "/api/v1/provisioning/policies", tree)
    print(
        "notification_policies_updated",
        f"{len(tree['routes'])} routes,",
        "root",
        tree["receiver"],
    )

    check_registry_drift()
    if PRUNE_LEGACY:
        prune_legacy(tree)
    return tree
