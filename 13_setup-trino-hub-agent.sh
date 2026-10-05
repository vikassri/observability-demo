#!/bin/bash
# Deploy Trino hub on observability EC2 (CLE kubeconfig):
#   - trino-status-exporter: GET /v1/status :8080 via kubectl exec (Trino UI API family)
#   - trino-hub-agent: file_sd + coordinator/worker logs → OTel
#
# Run from laptop (repo root):
#   ./13_setup-trino-hub-agent.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

echo ">>> Upload Trino hub scripts..."
$SSH "sudo mkdir -p /opt/uob-trino-hub-agent && sudo chown ec2-user:ec2-user /opt/uob-trino-hub-agent"
$SCP \
  "${REPO_DIR}/scripts/trino_discovery.py" \
  "${REPO_DIR}/scripts/trino_hub_agent.py" \
  "${REPO_DIR}/scripts/trino_status_exporter.py" \
  "${REPO_DIR}/scripts/trino_prometheus_sd.py" \
  "${REPO_DIR}/scripts/kubectl_logs_to_otel.py" \
  "${EC2_HOST}:/tmp/"
$SSH "mv /tmp/trino_*.py /opt/uob-trino-hub-agent/ && \
  mv /tmp/kubectl_logs_to_otel.py /opt/uob-k8s-log-forwarder/ && \
  chmod +x /opt/uob-trino-hub-agent/*.py /opt/uob-k8s-log-forwarder/kubectl_logs_to_otel.py"

echo ">>> systemd + Prometheus (job trino → :19640 status exporter)..."
$SSH bash << 'REMOTE'
set -euo pipefail
sudo mkdir -p /etc/prometheus/file_sd
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd
echo '[]' | sudo tee /etc/prometheus/file_sd/trino_status.json >/dev/null
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd/trino_status.json

sudo cp /etc/prometheus/prometheus.yml "/etc/prometheus/prometheus.yml.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
sudo python3 << 'PY'
from pathlib import Path
import re
path = Path("/etc/prometheus/prometheus.yml")
text = path.read_text()
block = """
  - job_name: trino
    scrape_interval: 30s
    metrics_path: /metrics
    file_sd_configs:
      - files:
          - /etc/prometheus/file_sd/trino_status.json
        refresh_interval: 30s
"""
text = re.sub(
    r"^\s+- job_name: trino\n.*?(?=^\s+- job_name:|\Z)",
    block + "\n",
    text,
    count=1,
    flags=re.M | re.S,
)
if "trino_status.json" not in text:
    text = text.rstrip() + "\n" + block + "\n"
path.write_text(text)
print("prometheus.yml trino job -> trino_status.json")
PY
promtool check config /etc/prometheus/prometheus.yml

sudo tee /etc/systemd/system/trino-status-exporter.service > /dev/null << 'UNIT'
[Unit]
Description=Trino /v1/status Prometheus exporter (port 8080 via kubectl exec)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-trino-hub-agent
Environment=TRINO_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=TRINO_EXPORTER_LISTEN_HOST=127.0.0.1
Environment=TRINO_EXPORTER_LISTEN_PORT=19640
Environment=TRINO_STATUS_INTERVAL=15
Environment=TRINO_QUERY_LOG_WINDOW=15m
WorkingDirectory=/opt/uob-trino-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-trino-hub-agent/trino_status_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/trino-hub-agent.service > /dev/null << 'UNIT'
[Unit]
Description=Trino hub agent (file_sd, logs to OTel)
After=network-online.target otelcol-contrib.service trino-status-exporter.service
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-trino-hub-agent
Environment=TRINO_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=TRINO_DISCOVERY_INTERVAL=60
Environment=OTEL_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
WorkingDirectory=/opt/uob-trino-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-trino-hub-agent/trino_hub_agent.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable trino-status-exporter trino-hub-agent
sudo systemctl restart trino-status-exporter
sleep 3
sudo systemctl restart trino-hub-agent
sleep 4
sudo systemctl restart prometheus
sleep 3

echo "--- trino-status-exporter (sample metrics) ---"
curl -sf http://127.0.0.1:19640/metrics | head -25
echo "--- file_sd ---"
cat /etc/prometheus/file_sd/trino_status.json
REMOTE

echo ""
echo "Done. Metrics: http://127.0.0.1:19640/metrics (Trino /v1/status on :8080)."
echo "UI: https://cle01-trino-f80f2a.cle01.spoc.cloudera-labs.com/ui/"
echo "Then: ./14_setup-grafana-trino-alerts.sh to refresh dashboard + alerts"
