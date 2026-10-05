#!/bin/bash
#===============================================================================
# App Server — OTel Collector Agent Setup
# Amazon Linux 2023
#
# This script runs on each app server. It installs the OpenTelemetry Collector
# as a lightweight agent that ships logs directly to the central OpenSearch
# server and optionally archives to S3.
#
# Usage:  ./99_setup-app-server.sh <OPENSEARCH_IP>
# Example: ./99_setup-app-server.sh observability.spoc.cloudera-labs.com
#===============================================================================
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
OTEL_VERSION="0.118.0"
OPENSEARCH_IP="${1:?Usage: $0 <OPENSEARCH_IP>}"
S3_BUCKET="uob-application-logs"
INSTALL_DIR="/opt/otelcol"
SERVICE_USER="otelcol"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }

# ── 1. Install OTel Collector ────────────────────────────────────────────────
install_otel() {
  info "Downloading OpenTelemetry Collector v${OTEL_VERSION}..."

  local arch
  arch=$(uname -m)
  [ "$arch" = "x86_64" ] && arch="amd64"
  [ "$arch" = "aarch64" ] && arch="arm64"

  local pkg="otelcol-contrib_${OTEL_VERSION}_linux_${arch}.tar.gz"
  wget -q "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTEL_VERSION}/${pkg}" \
    -O "/tmp/${pkg}"

  sudo mkdir -p "$INSTALL_DIR"
  sudo tar -xzf "/tmp/${pkg}" -C "$INSTALL_DIR"
  sudo chown -R root:root "$INSTALL_DIR"
  sudo ln -sf "$INSTALL_DIR/otelcol-contrib" /usr/local/bin/otelcol

  id -u "$SERVICE_USER" &>/dev/null || sudo useradd --system --no-create-home "$SERVICE_USER"
  info "OTel Collector installed at $INSTALL_DIR"
}

# ── 2. Configure OTel to ship logs to OpenSearch + archive to S3 ────────────
configure_otel() {
  info "Writing OTel Collector config..."

  sudo mkdir -p "$INSTALL_DIR/config"
  cat <<'CONFIG' | sudo tee "$INSTALL_DIR/config/config.yaml" >/dev/null
receivers:
  # ── Collect logs from your app directories ──
  filelog:
    include:
      - /var/log/app/**/*.log          # adjust to your app paths
      - /var/log/app/**/*.json
#     - /var/log/messages              # system logs (optional)
#     - /var/log/secure                # auth logs (optional)
    start_at: beginning
    include_file_path: true
    include_file_name: false
    operators:
      # Auto-detect JSON logs
      - type: json_parser
        id: json-parser
        parse_from: body
        timestamp:
          parse_from: attributes.timestamp
          layout: '%Y-%m-%dT%H:%M:%S.%LZ'
        severity:
          parse_from: attributes.level

  # ── Host metrics (CPU, memory, disk — useful for dashboards) ──
  hostmetrics:
    collection_interval: 60s
    scrapers:
      - cpu
      - memory
      - disk
      - load
      - network

processors:
  batch:
    timeout: 10s
    send_batch_size: 512

  attributes:
    actions:
      - key: "environment"
        value: "production"
        action: upsert
      - key: "server_ip"
        value: "${env:MY_IP}"
        action: upsert
      - key: "hostname"
        value: "${env:HOSTNAME}"
        action: upsert

  resourcedetection:
    detectors: [ec2, env]

exporters:
  # ── PRIMARY: Ship logs to OpenSearch ──
  opensearch:
    dataset_name: uob-logs
    namespace: production
    http:
      endpoint: "http://OPENSEARCH_IP_PLACEHOLDER:9201"
      tls:
        insecure: true
    index: "uob-logs-%{yyyy.MM.dd}"
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 300s

  # ── ARCHIVAL: Backup logs to S3 ──
  awss3:
    bucket: "uob-application-logs"
    endpoint: "https://s3.us-east-1.amazonaws.com"
    region: "us-east-1"
    path_style: true
    encoding:
      compression: gzip
    marshalling:
      value: json

  # ── Debug output (helpful for testing) ──
  debug:
    verbosity: detailed

service:
  pipelines:
    logs:
      receivers:
        - filelog
      processors:
        - resourcedetection
        - attributes
        - batch
      exporters:
        - opensearch
        - awss3
        - debug

    metrics:
      receivers:
        - hostmetrics
      processors:
        - batch
      exporters:
        - opensearch
        - debug
CONFIG

  # ── Replace placeholder with actual IP ──
  sudo sed -i "s/OPENSEARCH_IP_PLACEHOLDER/$OPENSEARCH_IP/g" \
    "$INSTALL_DIR/config/config.yaml"

  # ── Write env vars for attributes processor ──
  sudo mkdir -p /etc/otelcol
  echo 'MY_IP=$(hostname -I | awk "{print \$1}")' | sudo tee /etc/otelcol/otelcol.conf >/dev/null

  sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/config"
  info "Config written — points to OpenSearch at $OPENSEARCH_IP:9201"
}

# ── 3. Systemd service ──────────────────────────────────────────────────────
setup_service() {
  info "Creating systemd service..."

  cat <<'UNIT' | sudo tee /etc/systemd/system/otelcol.service >/dev/null
[Unit]
Description=OpenTelemetry Collector Agent
After=network.target

[Service]
Type=simple
User=otelcol
Group=otelcol
EnvironmentFile=/etc/otelcol/otelcol.conf
ExecStart=/usr/local/bin/otelcol --config /opt/otelcol/config/config.yaml
Restart=always
RestartSec=10
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT

  sudo systemctl daemon-reload
  sudo systemctl enable otelcol
  info "otelcol service created & enabled"
}

# ── 4. Credential check ─────────────────────────────────────────────────────
check_aws_creds() {
  if aws sts get-caller-identity &>/dev/null; then
    info "AWS credentials found — S3 archival enabled"
  else
    warn "No AWS credentials — S3 archival will fail."
    echo "  Either:"
    echo "    1. Attach an IAM role to this EC2 with:"
    echo "       - s3:PutObject on arn:aws:s3:::uob-application-logs/*"
    echo "       - s3:ListBucket on arn:aws:s3:::uob-application-logs"
    echo "    2. Or skip S3 archival by removing 'awss3' from the config"
  fi
}

# ── 5. Quick test ───────────────────────────────────────────────────────────
test_connection() {
  info "Testing connection to OpenSearch at $OPENSEARCH_IP:9201..."
  if curl -s --connect-timeout 5 "http://$OPENSEARCH_IP:9201" >/dev/null 2>&1; then
    info "OpenSearch is reachable!"
  else
    warn "Cannot reach OpenSearch at $OPENSEARCH_IP:9201"
    echo "  Check:"
    echo "    - Is the OpenSearch server running?"
    echo "    - Does the security group allow inbound on port 9201?"
    echo "    - Is $OPENSEARCH_IP the correct IP?"
  fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  echo ""
  echo "╔═══════════════════════════════════════════════════════════════╗"
  echo "║  App Server — OTel Collector Agent Setup                    ║"
  echo "║  Ships logs to OpenSearch at: $OPENSEARCH_IP         ║"
  echo "╚═══════════════════════════════════════════════════════════════╝"
  echo ""

  install_otel
  configure_otel
  setup_service
  check_aws_creds
  test_connection

  echo ""
  echo "  ── Start the agent:  sudo systemctl start otelcol"
  echo "  ── Check status:     sudo systemctl status otelcol"
  echo "  ── View live logs:   sudo journalctl -u otelcol -f"
  echo "  ── OpenSearch API:   curl http://$OPENSEARCH_IP:9201"
  echo "  ── Dashboards:       http://$OPENSEARCH_IP:5601"
  echo ""
}

main "$@"
