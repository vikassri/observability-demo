#!/bin/bash
# Spark on CDE (cde.yaml): SparkApplication CR metrics + operator/driver logs → OTel
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

$SSH "sudo mkdir -p /opt/uob-spark-hub-agent && sudo chown ec2-user:ec2-user /opt/uob-spark-hub-agent"
$SCP \
  "${REPO_DIR}/scripts/spark_"*.py \
  "${REPO_DIR}/scripts/kubectl_logs_to_otel.py" \
  "${EC2_HOST}:/tmp/"
$SSH "mv /tmp/spark_*.py /opt/uob-spark-hub-agent/ && mv /tmp/kubectl_logs_to_otel.py /opt/uob-k8s-log-forwarder/ && chmod +x /opt/uob-spark-hub-agent/*.py"

$SSH bash << 'REMOTE'
set -euo pipefail
sudo mkdir -p /etc/prometheus/file_sd
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd
echo '[]' | sudo tee /etc/prometheus/file_sd/spark_status.json >/dev/null
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd/spark_status.json

sudo python3 << 'PY'
from pathlib import Path
path = Path("/etc/prometheus/prometheus.yml")
text = path.read_text()
block = """
  - job_name: spark
    scrape_interval: 30s
    metrics_path: /metrics
    file_sd_configs:
      - files:
          - /etc/prometheus/file_sd/spark_status.json
        refresh_interval: 30s
"""
if "spark_status.json" not in text:
    path.write_text(text.rstrip() + "\n" + block + "\n")
    print("added spark scrape job")
PY
promtool check config /etc/prometheus/prometheus.yml

sudo tee /etc/systemd/system/spark-status-exporter.service > /dev/null << 'UNIT'
[Unit]
Description=Spark SparkApplication CR Prometheus exporter
After=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-spark-hub-agent
Environment=SPARK_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=SPARK_EXPORTER_LISTEN_PORT=19660
Environment=SPARK_NAMESPACE=dex-app-jjqrq8z9
Environment=SPARK_STATUS_INTERVAL=30
WorkingDirectory=/opt/uob-spark-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-spark-hub-agent/spark_status_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/spark-hub-agent.service > /dev/null << 'UNIT'
[Unit]
Description=Spark hub agent (file_sd, logs to OTel)
After=network-online.target otelcol-contrib.service spark-status-exporter.service

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-spark-hub-agent
Environment=SPARK_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=SPARK_DISCOVERY_INTERVAL=60
Environment=OTEL_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
WorkingDirectory=/opt/uob-spark-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-spark-hub-agent/spark_hub_agent.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable spark-status-exporter spark-hub-agent
sudo systemctl restart spark-status-exporter
sleep 2
sudo systemctl restart spark-hub-agent
sudo systemctl reload prometheus 2>/dev/null || sudo systemctl restart prometheus
curl -sf http://127.0.0.1:19660/metrics | grep -E '^spark_' | head -12
REMOTE
echo "Done. Run ./19_setup-grafana-spark-alerts.sh"
