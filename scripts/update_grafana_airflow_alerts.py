#!/usr/bin/env python3
"""Grafana Airflow alert rules.

Email templates, contact points and the notification policy tree are shared across all
services — see scripts/grafana_alerting.py, which mails each rule to its own address.
"""
from __future__ import annotations

import os

from grafana_alerting import (
    es_log_rule as _es_log_rule,
)
from grafana_alerting import (
    es_uid,
    folder_uid,
    prom_rule as _prom_rule,
    prom_uid,
    rule_uid,
    sync_routing,
    upsert_rules,
)

AIRFLOW_RELEASE = os.environ.get("AIRFLOW_RELEASE", "cwo01-cwo-cw-e94d4f")
AIRFLOW_NAMESPACE = os.environ.get("AIRFLOW_NAMESPACE", "cwo01-cwo-1b56da")
AIRFLOW_CLUSTER = os.environ.get("AIRFLOW_CLUSTER", "cwo")
AIRFLOW_ES_INDEX = os.environ.get("AIRFLOW_ES_INDEX", "uob-logs-*")

# Uids come from the shared registry so the rules and the notification routes cannot drift.
RULE_UID_SCHEDULER = rule_uid("airflow", "scheduler")
RULE_UID_API = rule_uid("airflow", "api")
RULE_UID_DAGRUN_FAILED = rule_uid("airflow", "dag-failed")
RULE_UID_TASK_FAILED = rule_uid("airflow", "task-failed")

AIRFLOW_LABELS = {
    "airflow_release": AIRFLOW_RELEASE,
    "airflow_namespace": AIRFLOW_NAMESPACE,
}


def prom_rule(uid, title, expr, summary, desc, for_d, severity, kind, folder, prom, op="lt", param=1):
    return _prom_rule(
        uid=uid,
        title=title,
        expr=expr,
        summary=summary,
        description=desc,
        for_duration=for_d,
        severity=severity,
        alert_kind=kind,
        service="airflow",
        folder=folder,
        prom=prom,
        rule_group="airflow-health",
        threshold_op=op,
        threshold_param=param,
        # The API rule uses `or vector(0)`, so zero is the alert; no data means the exporter
        # itself is gone, which is worth alerting on.
        no_data_state="Alerting" if kind == "api" else "OK",
        labels=dict(AIRFLOW_LABELS, airflow_alert_kind=kind),
    )


def es_rule(uid, title, query, summary, desc, folder, es, kind="dag-failed"):
    return _es_log_rule(
        uid=uid,
        title=title,
        query=query,
        summary=summary,
        description=desc,
        severity="critical",
        alert_kind=kind,
        service="airflow",
        folder=folder,
        es=es,
        rule_group="airflow-jobs",
        index=AIRFLOW_ES_INDEX,
        labels=dict(AIRFLOW_LABELS, airflow_alert_kind=kind),
    )


def main() -> None:
    prom = prom_uid()
    es = es_uid()
    folder = folder_uid("Airflow", "airflow-obs")
    base = f'airflow_release="{AIRFLOW_RELEASE}", cluster="{AIRFLOW_CLUSTER}"'

    rules = [
        prom_rule(
            RULE_UID_SCHEDULER,
            f"Airflow scheduler unhealthy ({AIRFLOW_RELEASE})",
            f'min(airflow_component_healthy{{{base},component="scheduler"}}) or vector(0)',
            "Airflow scheduler is unhealthy.",
            "Check the scheduler pods and the API server health endpoint.",
            "3m",
            "critical",
            "scheduler",
            folder,
            prom,
            "lt",
            1,
        ),
        prom_rule(
            RULE_UID_API,
            f"Airflow API unreachable ({AIRFLOW_RELEASE})",
            f'min(airflow_api_reachable{{{base}}}) or vector(0)',
            "The Airflow API is unreachable from the hub.",
            "Check the API server pod, the hub kubeconfig, and the status exporter.",
            "3m",
            "critical",
            "api",
            folder,
            prom,
            "lt",
            1,
        ),
        es_rule(
            RULE_UID_DAGRUN_FAILED,
            f"Airflow DAG run failed ({AIRFLOW_RELEASE})",
            "log:*DagRun* AND log:*Finished* AND log:*state=failed*",
            "A DAG run failed.",
            "Open the DAG run in the Airflow UI to see the failing tasks and logs.",
            folder,
            es,
            "dag-failed",
        ),
        es_rule(
            RULE_UID_TASK_FAILED,
            f"Airflow task failed ({AIRFLOW_RELEASE})",
            "log:*TaskInstance* AND log:*Finished* AND log:*state=failed*",
            "An Airflow task failed.",
            "Open the task logs in the Airflow UI, or the remote S3 logs.",
            folder,
            es,
            "task-failed",
        ),
    ]

    upsert_rules(rules)
    sync_routing()
    print("airflow_release", AIRFLOW_RELEASE, "namespace", AIRFLOW_NAMESPACE)


if __name__ == "__main__":
    main()
