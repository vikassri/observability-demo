#!/usr/bin/env python3
"""Grafana Trino alert rules.

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

TRINO_INSTANCE = os.environ.get("TRINO_INSTANCE", "cle01-trino")
TRINO_CLUSTER = os.environ.get("TRINO_CLUSTER", "cle")
TRINO_NAMESPACE = os.environ.get("TRINO_NAMESPACE", "cle01-trino-f80f2a")
TRINO_EXPECTED_WORKERS = os.environ.get("TRINO_EXPECTED_WORKERS", "2")
TRINO_ES_INDEX = os.environ.get("TRINO_ES_INDEX", "uob-logs-*")
QUERY_FAIL_WINDOW = os.environ.get("TRINO_QUERY_FAIL_WINDOW", "5m")

# Uids come from the shared registry so the rules and the notification routes cannot drift.
RULE_UID_COORD_DOWN = rule_uid("trino", "coordinator-down")
RULE_UID_WORKERS = rule_uid("trino", "workers-low")
RULE_UID_QUERY_FAIL = rule_uid("trino", "query-failed")

TRINO_LABELS = {
    "trino_instance": TRINO_INSTANCE,
    "trino_namespace": TRINO_NAMESPACE,
}


def window_seconds(value: str) -> int:
    if value.endswith("m"):
        return int(value[:-1]) * 60
    if value.endswith("h"):
        return int(value[:-1]) * 3600
    return 300


def prom_rule(
    uid,
    title,
    expr,
    summary,
    description,
    for_duration,
    severity,
    alert_kind,
    folder,
    prom,
    threshold_op="gt",
    threshold_param=0,
):
    return _prom_rule(
        uid=uid,
        title=title,
        expr=expr,
        summary=summary,
        description=description,
        for_duration=for_duration,
        severity=severity,
        alert_kind=alert_kind,
        service="trino",
        folder=folder,
        prom=prom,
        rule_group="trino-health",
        threshold_op=threshold_op,
        threshold_param=threshold_param,
        # A coordinator that stops reporting is the alert, so no data counts as alerting.
        no_data_state="Alerting" if alert_kind == "coordinator-down" else "OK",
        labels=dict(TRINO_LABELS, trino_alert_kind=alert_kind),
    )


def es_log_rule(uid, title, lucene_query, summary, description, folder, es):
    return _es_log_rule(
        uid=uid,
        title=title,
        query=lucene_query,
        summary=summary,
        description=description,
        severity="critical",
        alert_kind="query-failed",
        service="trino",
        folder=folder,
        es=es,
        rule_group="trino-query-health",
        index=TRINO_ES_INDEX,
        window_sec=window_seconds(QUERY_FAIL_WINDOW),
        labels=dict(TRINO_LABELS, trino_alert_kind="query-failed"),
    )


def main() -> None:
    prom = prom_uid()
    es = es_uid()
    folder = folder_uid("Trino", "trino-obs")

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
            "The Trino coordinator is not reachable from the hub.",
            "Check the coordinator pod, the status exporter, and the hub agent.",
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
            f"Fewer than {TRINO_EXPECTED_WORKERS} Trino workers are reporting metrics.",
            "Check the worker pods and the hub agent port-forwards.",
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
            "A Trino query failed.",
            "Find the query ID in the Trino UI or coordinator logs, then fix the SQL or permissions.",
            folder,
            es,
        ),
    ]

    upsert_rules(rules)
    sync_routing()

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
