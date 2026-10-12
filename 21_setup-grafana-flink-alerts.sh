#!/bin/bash
# Grafana Flink (CSA) dashboard + unified alerting -> Mailpit
#
# From repo root:
# ./21_setup-grafana-flink-alerts.sh
#
# Optional env:
# FLINK_CLUSTER=csa
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

FLINK_CLUSTER="${FLINK_CLUSTER:-csa}"

echo "========================================"
echo " Grafana Flink dashboard + alerts"
echo " Cluster: ${FLINK_CLUSTER}"
echo "========================================"

echo ">>> Upload scripts and dashboard JSON..."
$SCP \
    "${REPO_DIR}/scripts/update_grafana_flink_alerts.py" \
    "${REPO_DIR}/scripts/grafana_alerting.py" \
    "${REPO_DIR}/scripts/import_grafana_flink_dashboard.py" \
    "${REPO_DIR}/scripts/patch_grafana_elasticsearch_datasource.py" \
    "${REPO_DIR}/backups/grafana-flink-dashboard.json" \
    "${EC2_HOST}:/tmp/"

$SSH bash -s << REMOTE
set -euo pipefail
sudo mkdir -p /opt/uob-grafana-flink
sudo chown ec2-user:ec2-user /opt/uob-grafana-flink
mv /tmp/update_grafana_flink_alerts.py /tmp/grafana_alerting.py /tmp/import_grafana_flink_dashboard.py /tmp/patch_grafana_elasticsearch_datasource.py /opt/uob-grafana-flink/
mv /tmp/grafana-flink-dashboard.json /opt/uob-grafana-flink/
python3 /opt/uob-grafana-flink/patch_grafana_elasticsearch_datasource.py

export FLINK_CLUSTER='${FLINK_CLUSTER}'
python3 /opt/uob-grafana-flink/update_grafana_flink_alerts.py
PYTHONPATH=/opt/uob-grafana-flink python3 -c "
import import_grafana_flink_dashboard as m
m.DASH = __import__('pathlib').Path('/opt/uob-grafana-flink/grafana-flink-dashboard.json')
m.main()
"
REMOTE

echo ""
echo "========================================"
echo " Flink Grafana configured"
echo "========================================"
echo " Dashboard: http://observability.spoc.cloudera-labs.com:3000/d/flink-csa-obs/flink-csa"
echo " Alerts -> Mailpit: http://observability.spoc.cloudera-labs.com:8025"
echo " Rules: folder Flink (deployment failed, session job failed)"
echo "========================================"
