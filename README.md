# UOB POC — Observability Stack

Proof-of-concept observability on AWS EC2: **Grafana + Prometheus** for metrics, **Kibana + OpenSearch** for logs, and **Mailpit** for test alert email. Hub agents connect to NiFi, Trino, Airflow, Spark, and Flink in Kubernetes.

## Start here

**[PROJECT.md](./PROJECT.md)** — step-by-step setup guide and **[full architecture (hostnames, ports, clusters)](./PROJECT.md#architecture)**.

Optional deeper reading (if present in your repo):

- [docs/OBSERVABILITY_PLAN.md](./docs/OBSERVABILITY_PLAN.md) — multi-cluster strategy
- [docs/NIFI_IN_CLUSTER_GRAFANA.md](./docs/NIFI_IN_CLUSTER_GRAFANA.md) — NiFi metrics inside the cluster

## Quick start

```bash
ssh -i mail-server.pem ec2-user@observability.spoc.cloudera-labs.com
```

**Build from scratch:** follow **Parts 1–8** in [PROJECT.md](./PROJECT.md) (`01_` scripts through `22_`).

**Web apps and passwords:** [PROJECT.md — Open the apps](./PROJECT.md#open-the-apps-poc-urls-and-logins).

## Repo layout

| Path | Role |
|------|------|
| `01_`–`22_` `*.sh` | Setup scripts (laptop or EC2); **`08_`** uploads kubeconfigs |
| `*_kubeconfig.yaml` | Cluster credentials (local only — never commit) |
| `scripts/` | Hub agents, Grafana alert helpers |
| `backups/` | Grafana dashboard JSON exports |

`99_setup-app-server.sh` is in the repo but **not used** in the deployed POC (logs come from hub agents on the observability server).
