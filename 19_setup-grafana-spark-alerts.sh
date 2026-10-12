#!/bin/bash
# Grafana Spark (CDE) dashboard + unified alerting -> Mailpit
#
# From repo root:
# ./19_setup-grafana-spark-alerts.sh
#
# Optional env:
# SPARK_CLUSTER=cde
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

SPARK_CLUSTER="${SPARK_CLUSTER:-cde}"

echo "========================================"
echo " Grafana Spark dashboard + alerts"
echo " Cluster: ${SPARK_CLUSTER}"
echo "========================================"

echo ">>> Upload scripts and dashboard JSON..."
$SCP \
    "${REPO_DIR}/scripts/update_grafana_spark_alerts.py" \
    "${REPO_DIR}/scripts/grafana_alerting.py" \
    "${REPO_DIR}/scripts/import_grafana_spark_dashboard.py" \
    "${REPO_DIR}/scripts/patch_grafana_elasticsearch_datasource.py" \
    "${REPO_DIR}/backups/grafana-spark-dashboard.json" \
    "${EC2_HOST}:/tmp/"

$SSH bash -s << REMOTE
set -euo pipefail
sudo mkdir -p /opt/uob-grafana-spark
sudo chown ec2-user:ec2-user /opt/uob-grafana-spark
mv /tmp/update_grafana_spark_alerts.py /tmp/grafana_alerting.py /tmp/import_grafana_spark_dashboard.py /tmp/patch_grafana_elasticsearch_datasource.py /opt/uob-grafana-spark/
mv /tmp/grafana-spark-dashboard.json /opt/uob-grafana-spark/
python3 /opt/uob-grafana-spark/patch_grafana_elasticsearch_datasource.py

export SPARK_CLUSTER='${SPARK_CLUSTER}'
python3 /opt/uob-grafana-spark/update_grafana_spark_alerts.py
PYTHONPATH=/opt/uob-grafana-spark python3 -c "
import import_grafana_spark_dashboard as m
m.DASH = __import__('pathlib').Path('/opt/uob-grafana-spark/grafana-spark-dashboard.json')
m.main()
"
REMOTE

echo ""
echo "========================================"
echo " Spark Grafana configured"
echo "========================================"
echo " Dashboard: http://observability.spoc.cloudera-labs.com:3000/d/spark-cde-obs/spark-cde"
echo " Alerts -> Mailpit: http://observability.spoc.cloudera-labs.com:8025"
echo " Rules: folder Spark (application failed)"
echo "========================================"
