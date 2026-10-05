#!/usr/bin/env python3
"""Grafana NiFi alert rules.

Email templates, contact points and the notification policy tree are shared across all
services — see scripts/grafana_alerting.py, which mails each rule to its own address.
"""
from __future__ import annotations

import os

from grafana_alerting import (
    delete_rules,
    folder_uid,
    prom_rule as _prom_rule,
    prom_uid,
    rule_uid,
    sync_routing,
    upsert_rules,
)

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
QUEUE_STALL_WINDOW = os.environ.get("NIFI_QUEUE_STALL_WINDOW", "15m")
# Depth floor. A stalled queue holding a handful of files is a flow that happens to be idle,
# not an incident: measured over 24h on the Hello World flow, delta==0 held for queues of 3-7
# items in ~5% of samples, which pages on trivia. 100 keeps the rule on queues that actually
# hold a backlog. Set to 0 to alert on any stalled queue regardless of depth.
QUEUE_STALL_THRESHOLD = os.environ.get("NIFI_QUEUE_STALL_THRESHOLD", "100")

# Uids come from the shared registry so the rules and the notification routes cannot drift.
RULE_UID_BULLETIN = rule_uid("nifi", "bulletin")
RULE_UID_TERMINATED = rule_uid("nifi", "thread-terminated")
RULE_UID_COUNTER = rule_uid("nifi", "failure-counter")
RULE_UID_QUEUE_STALLED = rule_uid("nifi", "queue-stalled")

NIFI_LABELS = {
    "nifi_flow": NIFI_FLOW_SLUG,
    "nifi_namespace": NIFI_K8S_NAMESPACE,
}


def payload(
    uid,
    title,
    expr,
    summary,
    description,
    for_duration="1m",
    severity: str = "warning",
    alert_kind: str = "general",
    folder: str = "",
    prom: str = "",
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
        service="nifi",
        folder=folder,
        prom=prom,
        rule_group="nifi-processor-health",
        labels=dict(NIFI_LABELS, nifi_alert_kind=alert_kind),
    )


def main() -> None:
    prom = prom_uid()
    folder = folder_uid("NiFi", "nifi-obs")

    processor_scope = (
        f'job="{NIFI_JOB}", component_type="Processor", parent_id="{NIFI_PARENT_ID}"'
    )
    connection_scope = (
        f'job="{NIFI_JOB}", component_type="Connection", parent_id="{NIFI_PARENT_ID}"'
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
    # Queue that holds files and does not drain — catches a stopped/crashed processor
    # as well as backpressure. Independent of processor run-state, which NiFi does not
    # expose as a Prometheus metric.
    #
    # Group by source as well as destination: one producer fanning out to N consumers
    # (RouteOnAttribute, distribute) has N connections that share destination_name and
    # destination_id in some flows, so summing by destination alone merges them into one
    # series and hides which leg stalled. The `on(...)` list must mirror the `by(...)`
    # list exactly or the `and` matches nothing.
    queue_stalled = (
        "("
        "sum by (source_name, source_id, destination_name, destination_id, instance) "
        f"(nifi_amount_items_queued{{{connection_scope}}}) "
        "and on (source_name, source_id, destination_name, destination_id, instance) "
        f"(delta(nifi_amount_items_queued{{{connection_scope}}}[{QUEUE_STALL_WINDOW}]) == 0)"
        f") > {QUEUE_STALL_THRESHOLD}"
    )

    rules = [
        payload(
            RULE_UID_BULLETIN,
            f"NiFi processor bulletin ({NIFI_FLOW_TITLE})",
            bulletin,
            "A processor reported a warning or error.",
            "Check the Bulletin Board in NiFi and fix the reported error.",
            "2m",
            severity="warning",
            alert_kind="bulletin",
            folder=folder,
            prom=prom,
        ),
        payload(
            RULE_UID_TERMINATED,
            f"NiFi processor thread terminated ({NIFI_FLOW_TITLE})",
            terminated,
            "A processor thread terminated (possible crash).",
            "Check the Bulletin Board and pod logs for a stack trace, then restart the processor.",
            "1m",
            severity="critical",
            alert_kind="thread-terminated",
            folder=folder,
            prom=prom,
        ),
        payload(
            RULE_UID_COUNTER,
            f"NiFi processor failure counter ({NIFI_FLOW_TITLE})",
            counter,
            "Failure counters are rising on a processor.",
            "Check the processor's failure queue and bulletins to find the cause.",
            "1m",
            severity="critical",
            alert_kind="failure-counter",
            folder=folder,
            prom=prom,
        ),
        payload(
            RULE_UID_QUEUE_STALLED,
            f"NiFi queue stalled ({NIFI_FLOW_TITLE})",
            queue_stalled,
            f"Queued flow files are not draining (depth above {QUEUE_STALL_THRESHOLD}).",
            "The destination processor may be stopped, crashed, or backpressured. "
            "Confirm it is running in NiFi, and check the source processor if this "
            "connection is one of several feeding the same destination.",
            "10m",
            severity="warning",
            alert_kind="queue-stalled",
            folder=folder,
            prom=prom,
        ),
    ]

    upsert_rules(rules)
    # Superseded by the queue-stalled rule: a stopped processor no longer passes flow files,
    # so the idle test it used was removed.
    delete_rules([RULE_UID_STALLED])
    sync_routing()
    print(
        "flow",
        NIFI_FLOW_TITLE,
        "parent_id",
        NIFI_PARENT_ID,
        "bulletin_window",
        BULLETIN_WINDOW,
    )


if __name__ == "__main__":
    main()
