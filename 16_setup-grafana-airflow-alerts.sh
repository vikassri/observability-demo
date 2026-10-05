#!/bin/bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

AIRFLOW_RELEASE="${AIRFLOW_RELEASE:-cwo01-cwo-cw-e94d4f}"
AIRFLOW_NAMESPACE="${AIRFLOW_NAMESPACE:-cwo01-cwo-1b56da}"
AIRFLOW_CLUSTER="${AIRFLOW_CLUSTER:-cwo}"
AIRFLOW_ALERT_EMAIL="${AIRFLOW_ALERT_EMAIL:-airflow-alerts@local.test}"
AIRFLOW_UI_URL="${AIRFLOW_UI_URL:-https://cwo01-cwo-airflow-api-server.cwo01.spoc.cloudera-labs.com/}"

$SCP \
  "${REPO_DIR}/scripts/update_grafana_airflow_alerts.py" \
  "${REPO_DIR}/scripts/grafana_alerting.py" \
  "${REPO_DIR}/scripts/import_grafana_airflow_dashboard.py" \
  "${REPO_DIR}/scripts/patch_grafana_elasticsearch_datasource.py" \
  "${REPO_DIR}/backups/grafana-airflow-dashboard.json" \
  "${EC2_HOST}:/tmp/"

$SSH bash -s << REMOTE
set -euo pipefail
sudo mkdir -p /opt/uob-grafana-airflow && sudo chown ec2-user:ec2-user /opt/uob-grafana-airflow
mv /tmp/update_grafana_airflow_alerts.py /tmp/grafana_alerting.py /tmp/import_grafana_airflow_dashboard.py /tmp/patch_grafana_elasticsearch_datasource.py /opt/uob-grafana-airflow/
mv /tmp/grafana-airflow-dashboard.json /opt/uob-grafana-airflow/
python3 /opt/uob-grafana-airflow/patch_grafana_elasticsearch_datasource.py
export AIRFLOW_RELEASE='${AIRFLOW_RELEASE}'
export AIRFLOW_NAMESPACE='${AIRFLOW_NAMESPACE}'
export AIRFLOW_CLUSTER='${AIRFLOW_CLUSTER}'
export AIRFLOW_ALERT_EMAIL='${AIRFLOW_ALERT_EMAIL}'
export AIRFLOW_UI_URL='${AIRFLOW_UI_URL}'
python3 /opt/uob-grafana-airflow/update_grafana_airflow_alerts.py
PYTHONPATH=/opt/uob-grafana-airflow python3 /opt/uob-grafana-airflow/import_grafana_airflow_dashboard.py
REMOTE

echo "Dashboard: http://observability.spoc.cloudera-labs.com:3000/d/airflow-cwo-obs/airflow-cwo"
echo "Mailpit: http://observability.spoc.cloudera-labs.com:8025"
