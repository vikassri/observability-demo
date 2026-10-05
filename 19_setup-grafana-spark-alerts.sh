#!/bin/bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

$SCP \
  "${REPO_DIR}/scripts/import_grafana_spark_dashboard.py" \
  "${REPO_DIR}/scripts/patch_grafana_elasticsearch_datasource.py" \
  "${REPO_DIR}/backups/grafana-spark-dashboard.json" \
  "${EC2_HOST}:/tmp/"
$SSH "sudo mkdir -p /opt/uob-grafana-spark && sudo chown ec2-user:ec2-user /opt/uob-grafana-spark && \
  mv /tmp/import_grafana_spark_dashboard.py /tmp/patch_grafana_elasticsearch_datasource.py /opt/uob-grafana-spark/ && \
  mv /tmp/grafana-spark-dashboard.json /opt/uob-grafana-spark/ && \
  python3 /opt/uob-grafana-spark/patch_grafana_elasticsearch_datasource.py && \
  cp /opt/uob-grafana-spark/grafana-spark-dashboard.json /opt/uob-grafana-spark/../ 2>/dev/null || true && \
  PYTHONPATH=/opt/uob-grafana-spark python3 -c \"
import import_grafana_spark_dashboard as m
m.DASH = __import__('pathlib').Path('/opt/uob-grafana-spark/grafana-spark-dashboard.json')
m.main()
\""
echo "Dashboard: http://observability.spoc.cloudera-labs.com:3000/d/spark-cde-obs/spark-cde"
