#!/usr/bin/env python3
"""Grafana Spark (CDE) alert rules.

Email templates, contact points and the notification policy tree are shared across all
services — see scripts/grafana_alerting.py, which mails each rule to its own address.
"""
from __future__ import annotations

import os

from grafana_alerting import (
    folder_uid,
    prom_rule as _prom_rule,
    prom_uid,
    rule_uid,
    sync_routing,
    upsert_rules,
)

SPARK_CLUSTER = os.environ.get("SPARK_CLUSTER", "cde")

# Uids come from the shared registry so the rules and the notification routes cannot drift.
RULE_UID_APPLICATION_FAILED = rule_uid("spark", "application-failed")

SPARK_LABELS = {
    "spark_cluster": SPARK_CLUSTER,
}


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
        service="spark",
        folder=folder,
        prom=prom,
        rule_group="spark-job-health",
        threshold_op=threshold_op,
        threshold_param=threshold_param,
        labels=dict(SPARK_LABELS, spark_alert_kind=alert_kind),
    )


def main() -> None:
    prom = prom_uid()
    folder = folder_uid("Spark", "spark-obs")

    # The exporter publishes one series per SparkApplication, valued 0 or 1: the gauge is the
    # CR's terminal state, not a one-shot event, so a failed application keeps alerting until
    # its CR is deleted (or the exporter stops seeing it). The threshold turns that gauge into
    # one alert instance per application, named by its `spark_application` label.
    application_failed = f'spark_application_failed{{cluster="{SPARK_CLUSTER}"}}'

    rules = [
        prom_rule(
            RULE_UID_APPLICATION_FAILED,
            f"Spark application failed ({SPARK_CLUSTER})",
            application_failed,
            "A Spark application is in the FAILED state.",
            "Find the application in the CDE UI or with "
            "`kubectl get sparkapplication -A`, then read the driver logs for the failure.",
            "1m",
            "critical",
            "application-failed",
            folder,
            prom,
        ),
    ]

    upsert_rules(rules)
    sync_routing()
    print("spark_cluster", SPARK_CLUSTER)


if __name__ == "__main__":
    main()
