#!/bin/bash
set -e

echo "========================================"
echo "  Mailpit Setup + Grafana Integration"
echo "  Mail Server: observability.spoc.cloudera-labs.com"
echo "========================================"

SSH_KEY="/Users/vikassri/Desktop/uob_poc/mail-server.pem"
HOST="ec2-user@observability.spoc.cloudera-labs.com"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $HOST"

echo ">>> Installing Mailpit..."
$SSH bash << 'REMOTE'
    set -e

    # Install Mailpit
    curl -sL https://raw.githubusercontent.com/axllent/mailpit/develop/install.sh | bash

    # Create systemd service
    sudo tee /etc/systemd/system/mailpit.service > /dev/null << 'SVC'
[Unit]
Description=Mailpit - Email testing tool
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mailpit \
  --smtp 0.0.0.0:1025 \
  --listen 0.0.0.0:8025 \
  --webui-metrics \
  --disable-html-check \
  --db-file /var/lib/mailpit/mailpit.db

[Install]
WantedBy=multi-user.target
SVC

    sudo mkdir -p /var/lib/mailpit
    sudo systemctl daemon-reload
    sudo systemctl enable mailpit
    sudo systemctl start mailpit

    echo ">>> Waiting for Mailpit..."
    sleep 2
    sudo systemctl status mailpit --no-pager | head -12
REMOTE

# ---- Add Mailpit to Prometheus scrape config ----
echo ">>> Adding Mailpit to Prometheus scrape config..."
$SSH bash << 'REMOTE'
    sudo cp /etc/prometheus/prometheus.yml /etc/prometheus/prometheus.yml.bak

    # Insert mailpit job after node_exporter
    sudo tee /etc/prometheus/prometheus.yml > /dev/null << 'YML'
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]

  - job_name: "node_exporter"
    static_configs:
      - targets: ["localhost:9100"]

  - job_name: "mailpit"
    metrics_path: "/api/v1/metrics"
    static_configs:
      - targets: ["localhost:8025"]
YML

    sudo systemctl restart prometheus
    sleep 2
    sudo systemctl status prometheus --no-pager | head -8
REMOTE

echo ""
echo "========================================"
echo "  Mailpit is running!"
echo "========================================"
echo "  Web UI:    http://observability.spoc.cloudera-labs.com:8025"
echo "  SMTP:      observability.spoc.cloudera-labs.com:1025"
echo "========================================"
