#!/bin/bash
# Grafana Trino dashboard + unified alerting → Mailpit
#
# From repo root:
#   ./14_setup-grafana-trino-alerts.sh
#
# Optional env:
#   TRINO_ALERT_EMAIL=you@example.com
#   TRINO_INSTANCE=cle01-trino
#   TRINO_CLUSTER=cle
#   TRINO_EXPECTED_WORKERS=2
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

TRINO_ALERT_EMAIL="${TRINO_ALERT_EMAIL:-trino-alerts@local.test}"
TRINO_INSTANCE="${TRINO_INSTANCE:-cle01-trino}"
TRINO_CLUSTER="${TRINO_CLUSTER:-cle}"
TRINO_NAMESPACE="${TRINO_NAMESPACE:-cle01-trino-f80f2a}"
TRINO_EXPECTED_WORKERS="${TRINO_EXPECTED_WORKERS:-2}"

echo "========================================"
echo "  Grafana Trino dashboard + alerts"
echo "  Instance: ${TRINO_INSTANCE} (${TRINO_NAMESPACE})"
echo "========================================"

echo ">>> Upload scripts and dashboard JSON..."
$SCP \
  "${REPO_DIR}/scripts/update_grafana_trino_alerts.py" \
  "${REPO_DIR}/scripts/import_grafana_trino_dashboard.py" \
  "${REPO_DIR}/scripts/patch_grafana_elasticsearch_datasource.py" \
  "${REPO_DIR}/backups/grafana-trino-dashboard.json" \
  "${EC2_HOST}:/tmp/"
$SSH "mkdir -p /tmp/trino-hub-refresh"
$SCP \
  "${REPO_DIR}/scripts/trino_status_exporter.py" \
  "${REPO_DIR}/scripts/trino_prometheus_sd.py" \
  "${REPO_DIR}/scripts/trino_discovery.py" \
  "${REPO_DIR}/scripts/trino_hub_agent.py" \
  "${EC2_HOST}:/tmp/trino-hub-refresh/"

$SSH bash -s << REMOTE
set -euo pipefail
sudo mkdir -p /opt/uob-grafana-trino /opt/uob-trino-hub-agent
sudo chown ec2-user:ec2-user /opt/uob-grafana-trino /opt/uob-trino-hub-agent
mv /tmp/update_grafana_trino_alerts.py /tmp/import_grafana_trino_dashboard.py /tmp/patch_grafana_elasticsearch_datasource.py /opt/uob-grafana-trino/
mv /tmp/grafana-trino-dashboard.json /opt/uob-grafana-trino/
python3 /opt/uob-grafana-trino/patch_grafana_elasticsearch_datasource.py
chmod +x /opt/uob-grafana-trino/*.py
if [ -d /tmp/trino-hub-refresh ]; then
  mv /tmp/trino-hub-refresh/*.py /opt/uob-trino-hub-agent/
  chmod +x /opt/uob-trino-hub-agent/*.py
  sudo systemctl restart trino-status-exporter trino-hub-agent 2>/dev/null || true
fi

export TRINO_ALERT_EMAIL='${TRINO_ALERT_EMAIL}'
export TRINO_INSTANCE='${TRINO_INSTANCE}'
export TRINO_CLUSTER='${TRINO_CLUSTER}'
export TRINO_NAMESPACE='${TRINO_NAMESPACE}'
export TRINO_EXPECTED_WORKERS='${TRINO_EXPECTED_WORKERS}'

python3 /opt/uob-grafana-trino/update_grafana_trino_alerts.py
PYTHONPATH=/opt/uob-grafana-trino python3 /opt/uob-grafana-trino/import_grafana_trino_dashboard.py
REMOTE

echo ""
echo "========================================"
echo "  Trino Grafana configured"
echo "========================================"
echo "  Dashboard: http://observability.spoc.cloudera-labs.com:3000/d/trino-cle-obs/trino-cle"
echo "  Alerts -> Mailpit: http://observability.spoc.cloudera-labs.com:8025"
echo "  Email: ${TRINO_ALERT_EMAIL}"
echo "  Rules: folder Trino (coordinator down, workers low, query FAILED log)"
echo "========================================"
