#!/bin/bash
#===============================================================================
# Observability EC2 — tail / ingest logs → OTel Collector → Fluent Bit → OpenSearch
# UI: OpenSearch Dashboards (Kibana-compatible) on :5601
#
# Run on the observability host (Amazon Linux 2023):
#   sudo ./05_setup-observability-log-pipeline.sh
#
# Optional env:
#   OTEL_VERSION=0.118.0
#   FILELOG_GLOB="/var/log/app/*.log,/var/log/messages"
#   OPENSEARCH_PORT=9201
#   OTEL_FB_GRPC_PORT=24317   # Fluent Bit opentelemetry input (OTLP gRPC)
#===============================================================================
set -euo pipefail

OTEL_VERSION="${OTEL_VERSION:-0.118.0}"
OPENSEARCH_PORT="${OPENSEARCH_PORT:-9201}"
OTEL_FB_GRPC_PORT="${OTEL_FB_GRPC_PORT:-24317}"
FILELOG_GLOB="${FILELOG_GLOB:-/var/log/app/*.log}"
INSTALL_DIR="/opt/otelcol"
SERVICE_USER="otelcol"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    error "Run with sudo: sudo $0"
  fi
}

install_otel_contrib() {
  if [ -x /usr/local/bin/otelcol-contrib ]; then
    info "otelcol-contrib already installed"
    return
  fi
  info "Installing OpenTelemetry Collector Contrib v${OTEL_VERSION}..."
  local arch
  arch=$(uname -m)
  [ "$arch" = "x86_64" ] && arch="amd64"
  [ "$arch" = "aarch64" ] && arch="arm64"
  local pkg="otelcol-contrib_${OTEL_VERSION}_linux_${arch}.tar.gz"
  curl -fsSL \
    "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTEL_VERSION}/${pkg}" \
    -o "/tmp/${pkg}"
  mkdir -p "$INSTALL_DIR"
  tar -xzf "/tmp/${pkg}" -C "$INSTALL_DIR"
  ln -sf "$INSTALL_DIR/otelcol-contrib" /usr/local/bin/otelcol-contrib
  id -u "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home "$SERVICE_USER"
}

write_otel_config() {
  info "Writing OTel config (receivers → OTLP → Fluent Bit opentelemetry input)..."

  # Build filelog include list from comma-separated FILELOG_GLOB
  local includes=""
  IFS=',' read -ra paths <<< "$FILELOG_GLOB"
  for p in "${paths[@]}"; do
    includes="${includes}      - ${p}"$'\n'
  done

  mkdir -p "$INSTALL_DIR/config"
  cat >"$INSTALL_DIR/config/config.yaml" <<OTEL
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

  filelog:
    include:
${includes}    start_at: end
    include_file_path: true
    poll_interval: 500ms

  journald:
    directory: /var/log/journal
    units:
      - nifi-prometheus-portforward.service
      - prometheus.service
      - grafana-server.service
      - fluent-bit.service

processors:
  batch:
    timeout: 5s
    send_batch_size: 256
  resource/host:
    attributes:
      - key: service.name
        value: observability-host
        action: upsert

exporters:
  otlp/fluentbit:
    endpoint: 127.0.0.1:${OTEL_FB_GRPC_PORT}
    tls:
      insecure: true

  debug:
    verbosity: basic

service:
  pipelines:
    logs/otlp:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/fluentbit, debug]
    logs/host:
      receivers: [filelog, journald]
      processors: [resource/host, batch]
      exporters: [otlp/fluentbit, debug]
OTEL

  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/config"
}

write_otel_systemd() {
  cat >/etc/systemd/system/otelcol-contrib.service <<'UNIT'
[Unit]
Description=OpenTelemetry Collector (contrib) — logs to Fluent Bit
After=network.target fluent-bit.service
Wants=fluent-bit.service

[Service]
Type=simple
User=otelcol
Group=otelcol
SupplementaryGroups=systemd-journal
ExecStart=/usr/local/bin/otelcol-contrib --config /opt/otelcol/config/config.yaml
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable otelcol-contrib
}

install_fluent_bit() {
  if rpm -q fluent-bit &>/dev/null; then
    info "fluent-bit RPM already installed"
    return
  fi
  curl -fsSL https://raw.githubusercontent.com/fluent/fluent-bit/master/install.sh | sh
}

write_fluent_bit_config() {
  info "Configuring Fluent Bit (OTLP in → OpenSearch out)..."

  mkdir -p /var/log/app
  cat >/etc/fluent-bit/fluent-bit.conf <<FB
[SERVICE]
    Flush        1
    Daemon       Off
    Log_Level    info
    Parsers_File parsers.conf

# Logs from OTel gateway (K8s OTLP + local filelog/journald)
[INPUT]
    Name              opentelemetry
    Listen            127.0.0.1
    Port              ${OTEL_FB_GRPC_PORT}
    Tag               otel.logs

[FILTER]
    Name          modify
    Match         *
    Add           pipeline uob-observability

[OUTPUT]
    Name               opensearch
    Match              *
    Host               127.0.0.1
    Port               ${OPENSEARCH_PORT}
    Index              uob-logs
    Logstash_Format    On
    Logstash_Prefix    uob-logs
    Logstash_DateFormat %Y.%m.%d
    Suppress_Type_Name On
    tls                Off
    Trace_Error        On

# Optional mirror for Kibana (Elasticsearch :9200 on this host)
[OUTPUT]
    Name               es
    Match              *
    Host               127.0.0.1
    Port               9200
    Index              uob-logs
    Logstash_Format    On
    Logstash_Prefix    uob-logs
    Logstash_DateFormat %Y.%m.%d
    Suppress_Type_Name On
    Trace_Error        On
FB

  systemctl enable fluent-bit
}

ensure_opensearch_template() {
  info "Ensuring OpenSearch index template uob-logs-*..."
  curl -sf -X PUT "http://127.0.0.1:${OPENSEARCH_PORT}/_index_template/uob-app-logs" \
    -H "Content-Type: application/json" \
    -d '{
  "index_patterns": ["uob-logs-*"],
  "template": {
    "settings": { "number_of_shards": 1, "number_of_replicas": 0 },
    "mappings": {
      "properties": {
        "@timestamp": { "type": "date" },
        "message": { "type": "text" },
        "log": { "type": "text" },
        "product": { "type": "keyword" },
        "cluster": { "type": "keyword" },
        "k8s.pod.name": { "type": "keyword" },
        "k8s.namespace.name": { "type": "keyword" },
        "pipeline": { "type": "keyword" },
        "tag": { "type": "keyword" }
      }
    }
  }
}' >/dev/null || warn "Could not create index template (is OpenSearch up on :${OPENSEARCH_PORT}?)"
}

start_services() {
  systemctl restart fluent-bit
  sleep 2
  systemctl restart otelcol-contrib
  sleep 2
}

smoke_test() {
  info "Smoke test: append line to /var/log/app/smoke.log"
  echo "$(date -Is) smoke-test observability log pipeline" >>/var/log/app/smoke.log
  sleep 8
  local count
  count=$(curl -sf "http://127.0.0.1:${OPENSEARCH_PORT}/uob-logs-$(date +%Y.%m.%d)/_count" 2>/dev/null | grep -o '"count":[0-9]*' | cut -d: -f2 || echo "0")
  if [ "${count:-0}" -gt 0 ] 2>/dev/null; then
    info "OpenSearch uob-logs-$(date +%Y.%m.%d) doc count: $count"
  else
    warn "No documents in uob-logs-$(date +%Y.%m.%d) yet — check: journalctl -u otelcol-contrib -u fluent-bit -n 40"
  fi
}

print_summary() {
  echo ""
  echo "Log pipeline: K8s/local → OTel (:4317/:4318) → Fluent Bit (OTLP :${OTEL_FB_GRPC_PORT}) → OpenSearch (:${OPENSEARCH_PORT}) → Dashboards (:5601)"
  echo ""
  echo "  systemctl status fluent-bit otelcol-contrib"
  echo "  journalctl -u otelcol-contrib -f"
  echo "  curl -s http://127.0.0.1:${OPENSEARCH_PORT}/_cat/indices/uob-logs*?v"
  echo ""
  echo "Remote agents: point OTLP logs to http://<observability-ip>:4318 or gRPC :4317"
  echo "NiFi pods: run Fluent Bit or OTel on the cluster and ship OTLP to this host (metrics stay on Prometheus federation)."
  echo ""
}

main() {
  require_root
  install_otel_contrib
  write_otel_config
  write_otel_systemd
  install_fluent_bit
  write_fluent_bit_config
  ensure_opensearch_template
  start_services
  smoke_test
  print_summary
}

main "$@"
