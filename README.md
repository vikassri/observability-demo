# UOB observability POC

Central **observability hub** on AWS EC2 for a Cloudera-style platform: one place for **metrics**, **logs**, and **alert email** across **NiFi**, **Trino**, **Airflow**, **Spark**, and **Flink** in Kubernetes.

| | |
|---|---|
| **Metrics** | Prometheus + Grafana |
| **Logs** | OpenTelemetry → Fluent Bit → OpenSearch / Kibana |
| **Alerts** | Grafana → Mailpit (POC inbox) |
| **Backup** | Optional S3 archive (`23_setup-s3-archive.sh`) |

Hub agents on the observability server use **kubeconfig** files to reach remote clusters (no in-cluster logging DaemonSets in this POC).

---

## Documentation

**Full setup, architecture, ports, and troubleshooting:** **[PROJECT.md](./PROJECT.md)**

| Topic | Section |
|--------|---------|
| Architecture (hostnames, ports, clusters) | [PROJECT.md — Architecture](./PROJECT.md#architecture) |
| Step-by-step install (Parts 1–8) | [PROJECT.md — Step-by-step setup](./PROJECT.md#step-by-step-setup) |
| URLs and logins | [PROJECT.md — Open the apps](./PROJECT.md#open-the-apps-poc-urls-and-logins) |
| S3 log + metrics backup | Script **`23_setup-s3-archive.sh`** (see PROJECT.md data-flow) |

Optional local docs (if present): `docs/OBSERVABILITY_PLAN.md`, `docs/NIFI_IN_CLUSTER_GRAFANA.md`.

---

## Quick start

**SSH to the observability host** (replace key path if needed):

```bash
ssh -i mail-server.pem ec2-user@observability.spoc.cloudera-labs.com
```

**Install from scratch** (from repo root on your laptop):

1. **Parts 1–3** — Grafana, Prometheus, Mailpit, log pipeline, kubeconfigs (`01_`–`08_`, some steps on EC2 with sudo — see PROJECT.md).
2. **Part 4** — NiFi hub + alerts: recommended order **`11_` → `22_` → `12_`**.
3. **Parts 5–8** — Trino, Airflow, Spark, Flink (`13_`–`21_`).
4. **Optional** — S3 backup: `./23_setup-s3-archive.sh`.

**Main UIs (POC):**

| App | URL |
|-----|-----|
| Grafana | http://observability.spoc.cloudera-labs.com:3000 |
| Kibana (logs) | http://observability.spoc.cloudera-labs.com:5601 |
| Prometheus | http://observability.spoc.cloudera-labs.com:9090 |
| Mailpit | http://observability.spoc.cloudera-labs.com:8025 |

Credentials and details: [PROJECT.md](./PROJECT.md#open-the-apps-poc-urls-and-logins).

---

## Repository layout

| Path | Purpose |
|------|---------|
| `01_setup-*.sh` … `23_setup-*.sh` | Idempotent setup scripts |
| `08_setup-hub-kubeconfigs.sh` | Copy `*_kubeconfig.yaml` to the hub (required before hub agents) |
| `scripts/` | Hub agents, Grafana alert updaters, S3 snapshot helper |
| `backups/` | Grafana dashboard JSON (Trino, Airflow, Spark, Flink) |
| `PROJECT.md` | Master guide |
| `RCA_Architecture.png` | Architecture diagram (companion to PROJECT.md) |

**Do not commit:** `*.pem`, `*_kubeconfig.yaml`, secrets.

`99_setup-app-server.sh` is an unused optional pattern; the live POC uses **hub agents** on the observability EC2 only.

---

## Requirements

- Observability VM: Amazon Linux 2023 (see PROJECT.md).
- Laptop: `bash`, `ssh`, `scp`, Python 3.
- Network from hub to Kubernetes API `:6443` (bastion/tunnel if needed).
- Local kubeconfig files for CDF, CLE, CWO, CDE, CSA (and others per `08_`).

---

## License / status

Internal proof-of-concept — not production-hardened (default passwords, single EC2, Mailpit instead of real email).
