#!/bin/bash
# Flink / SSB on CSA (csa.yaml): FlinkDeployment + SessionJob metrics + logs → OTel
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

$SSH "sudo mkdir -p /opt/uob-flink-hub-agent && sudo chown ec2-user:ec2-user /opt/uob-flink-hub-agent"
$SCP \
  "${REPO_DIR}/scripts/flink_"*.py \
  "${REPO_DIR}/scripts/kubectl_logs_to_otel.py" \
  "${EC2_HOST}:/tmp/"
$SSH "mv /tmp/flink_*.py /opt/uob-flink-hub-agent/ && mv /tmp/kubectl_logs_to_otel.py /opt/uob-k8s-log-forwarder/ && chmod +x /opt/uob-flink-hub-agent/*.py"

$SSH bash << 'REMOTE'
set -euo pipefail
sudo mkdir -p /etc/prometheus/file_sd
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd
echo '[]' | sudo tee /etc/prometheus/file_sd/flink_status.json >/dev/null
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd/flink_status.json

sudo python3 << 'PY'
from pathlib import Path
path = Path("/etc/prometheus/prometheus.yml")
text = path.read_text()
block = """
  - job_name: flink
    scrape_interval: 30s
    metrics_path: /metrics
    file_sd_configs:
      - files:
          - /etc/prometheus/file_sd/flink_status.json
        refresh_interval: 30s
"""
if "flink_status.json" not in text:
    path.write_text(text.rstrip() + "\n" + block + "\n")
    print("added flink scrape job")
PY
promtool check config /etc/prometheus/prometheus.yml

sudo tee /etc/systemd/system/flink-status-exporter.service > /dev/null << 'UNIT'
[Unit]
Description=Flink CR Prometheus exporter (SSB / CSA)
After=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-flink-hub-agent
Environment=FLINK_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=FLINK_EXPORTER_LISTEN_PORT=19670
Environment=FLINK_STATUS_INTERVAL=30
WorkingDirectory=/opt/uob-flink-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-flink-hub-agent/flink_status_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/flink-hub-agent.service > /dev/null << 'UNIT'
[Unit]
Description=Flink hub agent (file_sd, logs to OTel)
After=network-online.target otelcol-contrib.service flink-status-exporter.service

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-flink-hub-agent
Environment=FLINK_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=FLINK_DISCOVERY_INTERVAL=60
Environment=OTEL_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
WorkingDirectory=/opt/uob-flink-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-flink-hub-agent/flink_hub_agent.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable flink-status-exporter flink-hub-agent
sudo systemctl restart flink-status-exporter
sleep 2
sudo systemctl restart flink-hub-agent
sudo systemctl reload prometheus 2>/dev/null || sudo systemctl restart prometheus
curl -sf http://127.0.0.1:19670/metrics | grep -E '^flink_' | head -12
REMOTE
echo "Done. Run ./21_setup-grafana-flink-alerts.sh"
