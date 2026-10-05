#!/bin/bash
# Observability EC2: install kubectl→OTel forwarder (venv + scripts).
# Used by nifi-hub-agent (kubectl logs -c app-log). Optional standalone nifi-kubectl-logs-otel unit (legacy).
# No Kubernetes ConfigMap / DaemonSet changes.
#
#   sudo ./10_setup-k8s-console-logs-to-otel.sh
#
# Env overrides:
#   NIFI_NAMESPACE  NIFI_POD  NIFI_LOG_CONTAINER  OTEL_LOGS_ENDPOINT  KUBECONFIG
set -euo pipefail

INSTALL_DIR="/opt/uob-k8s-log-forwarder"
VENV_DIR="${INSTALL_DIR}/venv"
SCRIPT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NIFI_NAMESPACE="${NIFI_NAMESPACE:-dfx-nifi-deployment-ns}"
NIFI_POD="${NIFI_POD:-dfx-nifi-0}"
NIFI_LOG_CONTAINER="${NIFI_LOG_CONTAINER:-nifi}"
OTEL_LOGS_ENDPOINT="${OTEL_LOGS_ENDPOINT:-http://127.0.0.1:4318/v1/logs}"
KUBECONFIG="${KUBECONFIG:-/etc/prometheus/kubeconfig/cdf.yaml}"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $1"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo" >&2
  exit 1
fi

dnf install -y python3-pip >/dev/null 2>&1 || true

mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_SRC/scripts/kubectl_logs_to_otel.py" "$INSTALL_DIR/"
cp "$SCRIPT_SRC/requirements-k8s-log-forwarder.txt" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/kubectl_logs_to_otel.py"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirements-k8s-log-forwarder.txt"

cat >/etc/systemd/system/nifi-kubectl-logs-otel.service <<UNIT
[Unit]
Description=NiFi kubectl console logs → OTel (OTLP)
After=network.target otelcol-contrib.service
Wants=otelcol-contrib.service

[Service]
Type=simple
Environment=KUBECONFIG=${KUBECONFIG}
Environment=NIFI_NAMESPACE=${NIFI_NAMESPACE}
Environment=NIFI_POD=${NIFI_POD}
Environment=NIFI_LOG_CONTAINER=${NIFI_LOG_CONTAINER}
Environment=OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=${OTEL_LOGS_ENDPOINT}
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/kubectl_logs_to_otel.py \\
  --kubeconfig ${KUBECONFIG} \\
  --namespace ${NIFI_NAMESPACE} \\
  --pod ${NIFI_POD} \\
  --container ${NIFI_LOG_CONTAINER} \\
  --endpoint ${OTEL_LOGS_ENDPOINT}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable nifi-kubectl-logs-otel
systemctl restart nifi-kubectl-logs-otel

sleep 3
systemctl --no-pager --full status nifi-kubectl-logs-otel | head -15

info "Logs: ${NIFI_NAMESPACE}/${NIFI_POD} container ${NIFI_LOG_CONTAINER} → ${OTEL_LOGS_ENDPOINT}"
info "Dashboards: index uob-logs-* (filter message or k8s.pod.name: ${NIFI_POD})"
echo "  journalctl -u nifi-kubectl-logs-otel -f"
