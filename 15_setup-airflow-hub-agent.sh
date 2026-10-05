#!/bin/bash
# Airflow on CWO (cwo.yaml): status exporter + log streams → OTel
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

echo ">>> Upload Airflow hub scripts..."
$SSH "sudo mkdir -p /opt/uob-airflow-hub-agent && sudo chown ec2-user:ec2-user /opt/uob-airflow-hub-agent"
$SCP \
  "${REPO_DIR}/scripts/airflow_discovery.py" \
  "${REPO_DIR}/scripts/airflow_status_exporter.py" \
  "${REPO_DIR}/scripts/airflow_prometheus_sd.py" \
  "${REPO_DIR}/scripts/airflow_hub_agent.py" \
  "${REPO_DIR}/scripts/kubectl_logs_to_otel.py" \
  "${EC2_HOST}:/tmp/"
$SSH "mv /tmp/airflow_*.py /opt/uob-airflow-hub-agent/ && \
  mv /tmp/kubectl_logs_to_otel.py /opt/uob-k8s-log-forwarder/ && \
  chmod +x /opt/uob-airflow-hub-agent/*.py"

$SSH bash << 'REMOTE'
set -euo pipefail
sudo mkdir -p /etc/prometheus/file_sd
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd
echo '[]' | sudo tee /etc/prometheus/file_sd/airflow_status.json >/dev/null
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd/airflow_status.json

sudo python3 << 'PY'
from pathlib import Path
import re
path = Path("/etc/prometheus/prometheus.yml")
text = path.read_text()
block = """
  - job_name: airflow
    scrape_interval: 30s
    metrics_path: /metrics
    file_sd_configs:
      - files:
          - /etc/prometheus/file_sd/airflow_status.json
        refresh_interval: 30s
"""
if "airflow_status.json" not in text:
    text = text.rstrip() + "\n" + block + "\n"
    path.write_text(text)
    print("added airflow scrape job")
else:
    print("airflow scrape job present")
PY
promtool check config /etc/prometheus/prometheus.yml

sudo tee /etc/systemd/system/airflow-status-exporter.service > /dev/null << 'UNIT'
[Unit]
Description=Airflow /api/v2/monitor/health Prometheus exporter
After=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-airflow-hub-agent
Environment=AIRFLOW_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=AIRFLOW_EXPORTER_LISTEN_PORT=19650
Environment=AIRFLOW_STATUS_INTERVAL=15
Environment=AIRFLOW_LOG_WINDOW=15m
WorkingDirectory=/opt/uob-airflow-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-airflow-hub-agent/airflow_status_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/airflow-hub-agent.service > /dev/null << 'UNIT'
[Unit]
Description=Airflow hub agent (file_sd, logs to OTel)
After=network-online.target otelcol-contrib.service airflow-status-exporter.service

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-airflow-hub-agent
Environment=AIRFLOW_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=AIRFLOW_DISCOVERY_INTERVAL=60
Environment=AIRFLOW_UI_URL=https://cwo01-cwo-airflow-api-server.cwo01.spoc.cloudera-labs.com/
Environment=OTEL_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
WorkingDirectory=/opt/uob-airflow-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-airflow-hub-agent/airflow_hub_agent.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable airflow-status-exporter airflow-hub-agent
sudo systemctl restart airflow-status-exporter
sleep 3
sudo systemctl restart airflow-hub-agent
sudo systemctl restart prometheus
sleep 2
curl -sf http://127.0.0.1:19650/metrics | head -20
REMOTE

echo "Done. Run ./16_setup-grafana-airflow-alerts.sh for dashboard + alerts."
