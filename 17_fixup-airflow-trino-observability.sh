#!/bin/bash
# Fix Airflow Prometheus file_sd permissions, Grafana ES log field, hub log shippers.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

$SCP \
  "${REPO_DIR}/06_setup-kibana-uob-logs.sh" \
  "${REPO_DIR}/scripts/kubectl_logs_to_otel.py" \
  "${REPO_DIR}/scripts/patch_grafana_elasticsearch_datasource.py" \
  "${REPO_DIR}/scripts/airflow_prometheus_sd.py" \
  "${EC2_HOST}:/tmp/"

$SCP \
  "${REPO_DIR}/backups/grafana-airflow-dashboard.json" \
  "${REPO_DIR}/backups/grafana-trino-dashboard.json" \
  "${REPO_DIR}/scripts/import_grafana_airflow_dashboard.py" \
  "${REPO_DIR}/scripts/import_grafana_trino_dashboard.py" \
  "${EC2_HOST}:/tmp/"

$SSH bash -s << 'REMOTE'
set -euo pipefail
sudo cp /tmp/kubectl_logs_to_otel.py /opt/uob-k8s-log-forwarder/
chmod +x /tmp/06_setup-kibana-uob-logs.sh 2>/dev/null || true
/tmp/06_setup-kibana-uob-logs.sh || bash /tmp/06_setup-kibana-uob-logs.sh
sudo cp /tmp/airflow_prometheus_sd.py /opt/uob-airflow-hub-agent/
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd/airflow_status.json
PYTHONPATH=/opt/uob-airflow-hub-agent python3 /opt/uob-airflow-hub-agent/airflow_prometheus_sd.py /etc/prometheus/kubeconfig
curl -sf -X POST http://127.0.0.1:9090/-/reload || sudo systemctl reload prometheus
sudo systemctl restart airflow-hub-agent trino-hub-agent
python3 /tmp/patch_grafana_elasticsearch_datasource.py
mkdir -p /opt/uob-grafana-fix && cp /tmp/import_grafana_*.py /tmp/grafana-*-dashboard.json /opt/uob-grafana-fix/
export AIRFLOW_UI_URL="${AIRFLOW_UI_URL:-https://cwo01-cwo-airflow-api-server.cwo01.spoc.cloudera-labs.com/}"
PYTHONPATH=/opt/uob-grafana-fix python3 /opt/uob-grafana-fix/import_grafana_airflow_dashboard.py
PYTHONPATH=/opt/uob-grafana-fix python3 /opt/uob-grafana-fix/import_grafana_trino_dashboard.py
echo "--- airflow file_sd ---"
cat /etc/prometheus/file_sd/airflow_status.json
echo "--- prometheus airflow metric ---"
curl -sf 'http://127.0.0.1:9090/api/v1/label/airflow_release/values'
REMOTE

echo "Done. Refresh Grafana dashboards; Airflow variables should populate within ~30s."
