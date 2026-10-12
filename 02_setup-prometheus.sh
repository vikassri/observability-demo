#!/bin/bash
set -e

echo "========================================"
echo "  Prometheus + Node Exporter Setup"
echo "  Mail Server: observability.spoc.cloudera-labs.com"
echo "========================================"

SSH_KEY="/Users/vikassri/Desktop/uob_poc/mail-server.pem"
HOST="ec2-user@observability.spoc.cloudera-labs.com"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $HOST"

echo ">>> Installing Prometheus and Node Exporter..."
$SSH bash << 'REMOTE'
    set -e

    PROM_VERSION="2.54.1"
    NODE_EXP_VERSION="1.8.2"

    # ---------- Node Exporter ----------
    echo ">>> Installing Node Exporter v${NODE_EXP_VERSION}..."
    cd /tmp
    wget -q https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXP_VERSION}/node_exporter-${NODE_EXP_VERSION}.linux-amd64.tar.gz
    tar xzf node_exporter-${NODE_EXP_VERSION}.linux-amd64.tar.gz
    sudo install node_exporter-${NODE_EXP_VERSION}.linux-amd64/node_exporter /usr/local/bin/
    rm -rf node_exporter-${NODE_EXP_VERSION}.linux-amd64*

    # Node Exporter systemd service
    sudo tee /etc/systemd/system/node_exporter.service > /dev/null << 'SVC'
[Unit]
Description=Prometheus Node Exporter
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/node_exporter \
  --web.listen-address=:9100 \
  --collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($|/)

[Install]
WantedBy=multi-user.target
SVC

    sudo systemctl daemon-reload
    sudo systemctl enable node_exporter
    sudo systemctl start node_exporter

    # ---------- Prometheus ----------
    echo ">>> Installing Prometheus v${PROM_VERSION}..."
    cd /tmp
    wget -q https://github.com/prometheus/prometheus/releases/download/v${PROM_VERSION}/prometheus-${PROM_VERSION}.linux-amd64.tar.gz
    tar xzf prometheus-${PROM_VERSION}.linux-amd64.tar.gz
    sudo install prometheus-${PROM_VERSION}.linux-amd64/prometheus /usr/local/bin/
    sudo install prometheus-${PROM_VERSION}.linux-amd64/promtool /usr/local/bin/
    sudo mkdir -p /etc/prometheus /var/lib/prometheus
    sudo cp -r prometheus-${PROM_VERSION}.linux-amd64/consoles /etc/prometheus/
    sudo cp -r prometheus-${PROM_VERSION}.linux-amd64/console_libraries /etc/prometheus/
    rm -rf prometheus-${PROM_VERSION}.linux-amd64*

    # Prometheus config
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
YML

    # Prometheus systemd service
    sudo tee /etc/systemd/system/prometheus.service > /dev/null << 'SVC'
[Unit]
Description=Prometheus Time Series Collection and Alerting
After=network.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
ExecStart=/usr/local/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --web.listen-address=:9090 \
  --web.enable-admin-api

[Install]
WantedBy=multi-user.target
SVC

    sudo systemctl daemon-reload
    sudo systemctl enable prometheus
    sudo systemctl start prometheus

    echo ">>> Waiting for services..."
    sleep 3
    echo ">>> Prometheus targets:"
    curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool 2>/dev/null || true
REMOTE

echo ""
echo ">>> Checking services..."
$SSH "sudo systemctl status node_exporter --no-pager | head -10"
echo "---"
$SSH "sudo systemctl status prometheus --no-pager | head -10"

echo ""
echo "========================================"
echo "  Prometheus + Node Exporter installed!"
echo "========================================"
echo "  Prometheus:    http://observability.spoc.cloudera-labs.com:9090"
echo "  Node Exporter: http://observability.spoc.cloudera-labs.com:9100"
echo "========================================"
