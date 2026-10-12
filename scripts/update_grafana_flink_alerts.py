#!/usr/bin/env python3
"""Grafana Flink (CSA) alert rules.

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

FLINK_CLUSTER = os.environ.get("FLINK_CLUSTER", "csa")

# Uids come from the shared registry so the rules and the notification routes cannot drift.
RULE_UID_DEPLOYMENT_FAILED = rule_uid("flink", "deployment-failed")
RULE_UID_SESSIONJOB_FAILED = rule_uid("flink", "sessionjob-failed")

FLINK_LABELS = {
    "flink_cluster": FLINK_CLUSTER,
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
        service="flink",
        folder=folder,
        prom=prom,
        rule_group="flink-job-health",
        threshold_op=threshold_op,
        threshold_param=threshold_param,
        labels=dict(FLINK_LABELS, flink_alert_kind=alert_kind),
    )


def main() -> None:
    prom = prom_uid()
    folder = folder_uid("Flink", "flink-obs")

    # Flink has two job CRs, so a failed job shows up in one of two gauges. Both are terminal
    # state, not events: like Spark above, a failed job keeps alerting until its CR is removed,
    # and the threshold makes one alert instance per deployment/session job.
    deployment_failed = f'flink_deployment_failed{{cluster="{FLINK_CLUSTER}"}}'
    sessionjob_failed = f'flink_sessionjob_failed{{cluster="{FLINK_CLUSTER}"}}'

    rules = [
        prom_rule(
            RULE_UID_DEPLOYMENT_FAILED,
            f"Flink deployment failed ({FLINK_CLUSTER})",
            deployment_failed,
            "A FlinkDeployment job is in the FAILED state.",
            "Check the job in the CSA UI or with "
            "`kubectl get flinkdeployment -A`, then read the job manager and task manager "
            "logs for the failure.",
            "1m",
            "critical",
            "deployment-failed",
            folder,
            prom,
        ),
        prom_rule(
            RULE_UID_SESSIONJOB_FAILED,
            f"Flink session job failed ({FLINK_CLUSTER})",
            sessionjob_failed,
            "A FlinkSessionJob is in the FAILED state.",
            "Check the session job in the CSA UI or with "
            "`kubectl get flinksessionjob -A`, then read the job manager logs for the failure.",
            "1m",
            "critical",
            "sessionjob-failed",
            folder,
            prom,
        ),
    ]

    upsert_rules(rules)
    sync_routing()
    print("flink_cluster", FLINK_CLUSTER)


if __name__ == "__main__":
    main()
