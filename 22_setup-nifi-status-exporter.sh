#!/bin/bash
# NiFi processor run-state exporter + Prometheus job on the observability EC2.
#
# Run from laptop (repo root):
# ./22_setup-nifi-status-exporter.sh
#
# Why this exists: the federated NiFi metrics (job nifi_federate -> dfx-nifi-web) carry
# queues, bulletins and failure counters, but no processor run-state — NiFi's Prometheus
# reporter publishes none. So nothing could alert on a processor being stopped; the
# queue-stalled rule could only infer it from a deep connection that stopped draining.
# This installs scripts/nifi_status_exporter.py, which polls the flow REST API for runStatus
# and exposes nifi_processor_run_status on 127.0.0.1:19680 as Prometheus job nifi_status.
# The matching alert rule is the processor-stopped rule in scripts/update_grafana_nifi_alerts.py.
#
# Prerequisites: 08_setup-hub-kubeconfigs.sh, 11_setup-nifi-auto-discovery.sh.
#
# Optional env:
#   NIFI_WEB_SVC            flow API host reachable from the Prometheus pod (default: pod FQDN)
#   NIFI_K8S_NAMESPACE      dfx-nifi-deployment-ns
#   NIFI_PROM_STATUS_JOB    nifi_status       NIFI_EXPORTER_PORT 19680
#   NIFI_STATUS_INTERVAL    30                NIFI_PARENT_ID     f5876d95-01a0-1000-0000-00005a7a3b62
#   NIFI_FLOW_TITLE         Hello World       NIFI_CLUSTER       cdf
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

AGENT_DIR=/opt/uob-nifi-hub-agent
NIFI_KUBECONFIG="${NIFI_KUBECONFIG:-/etc/prometheus/kubeconfig/cdf.yaml}"
NIFI_K8S_NAMESPACE="${NIFI_K8S_NAMESPACE:-dfx-nifi-deployment-ns}"
# The Service name dfx-nifi-web resolves to the CDF ingress waypoint, which answers HTTPS but
# serves no flow API (400 "No resource method found for GET"). The pod's own DNS name does
# reach the NiFi API, and the client cert in the Prometheus pod carries a SAN for it, so the
# pod FQDN is the default. If the cert is ever reissued without that SAN, either point this at
# a Service that does reach the UI pod or set NIFI_INSECURE=1 in the unit (which stops
# verifying the flow API's certificate — see the exporter's header).
NIFI_WEB_SVC="${NIFI_WEB_SVC:-dfx-nifi-0.dfx-nifi.dfx-nifi-deployment-ns.svc.cluster.local}"
NIFI_WEB_PORT="${NIFI_WEB_PORT:-8443}"
NIFI_EXPORTER_PORT="${NIFI_EXPORTER_PORT:-19680}"
NIFI_STATUS_INTERVAL="${NIFI_STATUS_INTERVAL:-30}"
NIFI_PROM_STATUS_JOB="${NIFI_PROM_STATUS_JOB:-nifi_status}"
NIFI_PARENT_ID="${NIFI_PARENT_ID:-f5876d95-01a0-1000-0000-00005a7a3b62}"
NIFI_FLOW_TITLE="${NIFI_FLOW_TITLE:-Hello World}"
NIFI_CLUSTER="${NIFI_CLUSTER:-cdf}"

echo "========================================"
echo " NiFi processor run-state exporter"
echo " Flow API: https://${NIFI_WEB_SVC}:${NIFI_WEB_PORT}"
echo " Exporter: 127.0.0.1:${NIFI_EXPORTER_PORT} -> Prometheus job ${NIFI_PROM_STATUS_JOB}"
echo " Flow:     ${NIFI_FLOW_TITLE} (parent_id=${NIFI_PARENT_ID})"
echo "========================================"

echo ">>> Uploading exporter..."
$SSH "sudo mkdir -p ${AGENT_DIR} && sudo chown ec2-user:ec2-user ${AGENT_DIR}"
$SCP "${REPO_DIR}/scripts/nifi_status_exporter.py" "${EC2_HOST}:/tmp/"
$SSH "mv /tmp/nifi_status_exporter.py ${AGENT_DIR}/ && chmod +x ${AGENT_DIR}/nifi_status_exporter.py"

# Fail before anything is installed: a wrong host name, cert mount or authorization shows up
# here as a specific HTTP code, and installing the unit first would leave the POC with a job
# that scrapes nothing and an alert rule that can never fire.
echo ">>> Preflight: cert, pod and flow API (nothing installed yet)..."
$SSH bash -s << REMOTE
set -euo pipefail
export NIFI_KUBECONFIG='${NIFI_KUBECONFIG}'
export NIFI_K8S_NAMESPACE='${NIFI_K8S_NAMESPACE}'
export NIFI_WEB_SVC='${NIFI_WEB_SVC}'
export NIFI_WEB_PORT='${NIFI_WEB_PORT}'
export NIFI_PARENT_ID='${NIFI_PARENT_ID}'
export NIFI_FLOW_TITLE='${NIFI_FLOW_TITLE}'
command -v kubectl >/dev/null || {
  echo "kubectl is not on PATH for ec2-user — the unit shells out to it" >&2
  exit 1
}
test -r '${NIFI_KUBECONFIG}' || {
  echo "kubeconfig ${NIFI_KUBECONFIG} is not readable — run 08_setup-hub-kubeconfigs.sh" >&2
  exit 1
}
if ! python3 '${AGENT_DIR}/nifi_status_exporter.py' --selftest; then
  echo "" >&2
  echo "selftest FAILED — nothing was installed. Read the GET lines above:" >&2
  echo "  no response -> the host in https://${NIFI_WEB_SVC}:${NIFI_WEB_PORT} is unreachable from the Prometheus pod" >&2
  echo "  403         -> the client cert is not authorized for the flow API" >&2
  echo "  404         -> wrong path or wrong host (a CDF ingress waypoint answers this way)" >&2
  exit 1
fi
REMOTE

echo ">>> Installing systemd unit + Prometheus file_sd + job..."
$SSH bash -s << REMOTE
set -euo pipefail
sudo mkdir -p /etc/prometheus/file_sd
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd
# Unlike nifi_federate.json, which the hub agent rewrites as it discovers deployments, the
# status exporter is a single fixed local target, so this file is written once and only needs
# regenerating when NIFI_EXPORTER_PORT changes.
echo '[{"targets": ["127.0.0.1:${NIFI_EXPORTER_PORT}"]}]' | sudo tee /etc/prometheus/file_sd/${NIFI_PROM_STATUS_JOB}.json > /dev/null
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd/${NIFI_PROM_STATUS_JOB}.json

if grep -q "job_name: ${NIFI_PROM_STATUS_JOB}" /etc/prometheus/prometheus.yml; then
  echo "Prometheus job ${NIFI_PROM_STATUS_JOB} already present, replacing it"
else
  echo "Adding Prometheus job ${NIFI_PROM_STATUS_JOB}"
fi
BACKUP="/etc/prometheus/prometheus.yml.bak.\$(date +%Y%m%d%H%M%S)"
sudo cp /etc/prometheus/prometheus.yml "\$BACKUP"
sudo python3 << 'PY'
import re
from pathlib import Path

path = Path("/etc/prometheus/prometheus.yml")
text = path.read_text()
job = "${NIFI_PROM_STATUS_JOB}"
block = """
  - job_name: {job}
    scrape_interval: 30s
    metrics_path: /metrics
    file_sd_configs:
      - files:
          - /etc/prometheus/file_sd/{job}.json
        refresh_interval: 30s
""".format(job=job)
pattern = r"^\s+- job_name: " + re.escape(job) + r"\n.*?(?=^\s+- job_name:|\Z)"
if re.search(r"^\s+- job_name: " + re.escape(job) + r"\n", text, re.M):
    # A job with a metric relabel rule or extra keys keeps them only until this block is
    # replaced; the file_sd target is the whole configuration for this exporter.
    text = re.sub(pattern, block.lstrip("\n") + "\n", text, count=1, flags=re.M | re.S)
else:
    text = text.rstrip() + "\n" + block + "\n"
path.write_text(text)
print("updated prometheus.yml job", job)
PY
if ! promtool check config /etc/prometheus/prometheus.yml; then
  sudo cp "\$BACKUP" /etc/prometheus/prometheus.yml
  echo "promtool rejected the edited prometheus.yml — restored \$BACKUP, nothing restarted" >&2
  exit 1
fi

sudo tee /etc/systemd/system/nifi-status-exporter.service > /dev/null << 'UNIT'
[Unit]
Description=NiFi processor run-state Prometheus exporter (flow API via kubectl exec)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-nifi-hub-agent
Environment=NIFI_KUBECONFIG=/etc/prometheus/kubeconfig/cdf.yaml
Environment=NIFI_K8S_NAMESPACE=${NIFI_K8S_NAMESPACE}
Environment=NIFI_WEB_SVC=${NIFI_WEB_SVC}
Environment=NIFI_WEB_PORT=${NIFI_WEB_PORT}
Environment=NIFI_CERT_DIR=/etc/prometheus/secrets/prometheus-cert
Environment=NIFI_EXPORTER_LISTEN_HOST=127.0.0.1
Environment=NIFI_EXPORTER_LISTEN_PORT=${NIFI_EXPORTER_PORT}
Environment=NIFI_STATUS_INTERVAL=${NIFI_STATUS_INTERVAL}
Environment=NIFI_PARENT_ID=${NIFI_PARENT_ID}
Environment=NIFI_FLOW_TITLE=${NIFI_FLOW_TITLE}
Environment=NIFI_CLUSTER=${NIFI_CLUSTER}
WorkingDirectory=/opt/uob-nifi-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-nifi-hub-agent/nifi_status_exporter.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable nifi-status-exporter
sudo systemctl restart nifi-status-exporter
sleep 5
sudo systemctl restart prometheus
sleep 4

echo "--- nifi-status-exporter ---"
systemctl --no-pager --full status nifi-status-exporter | head -12
echo "--- exporter metrics ---"
curl -sf http://127.0.0.1:${NIFI_EXPORTER_PORT}/metrics | grep -E '^nifi_(processors_total|status_exporter_)' || echo "(no exporter output yet)"
echo "--- processors not RUNNING (what the alert fires on) ---"
curl -sf http://127.0.0.1:${NIFI_EXPORTER_PORT}/metrics | grep 'nifi_processor_run_status' | grep ' 0$' || echo "(none — every processor is running)"
echo "--- Prometheus: series by run_status ---"
curl -sf --data-urlencode 'query=count by (run_status) (nifi_processor_run_status)' http://localhost:9090/api/v1/query | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print('\n'.join('  %s: %s' % (s['metric'].get('run_status','?'), s['value'][1]) for s in r) or '  (no series — check the job in Prometheus targets)')"
REMOTE

echo ""
echo "========================================"
echo " NiFi run-state exporter configured"
echo "========================================"
echo " Service: nifi-status-exporter (127.0.0.1:${NIFI_EXPORTER_PORT})"
echo " Metrics: nifi_processor_run_status, nifi_processors_total, nifi_status_exporter_up"
echo " Prometheus targets: http://observability.spoc.cloudera-labs.com:9090/targets"
echo ""
echo " Next: rerun ./12_setup-grafana-nifi-alerts.sh to add the processor-stopped rule."
echo "========================================"
