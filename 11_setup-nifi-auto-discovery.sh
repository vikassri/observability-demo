#!/bin/bash
# Deploy NiFi auto-discovery on observability EC2:
#   - Label-based discovery of NiFi namespaces (all kubeconfigs in /etc/prometheus/kubeconfig/)
#   - kubectl port-forward per deployment with svc/prometheus → Prometheus file_sd
#   - kubectl logs -c app-log → OTel for each NiFi pod (Kibana uob-logs-*, not Prometheus)
#
# Run from laptop (repo root):
#   ./11_setup-nifi-auto-discovery.sh
#
# Prerequisites:
#   ./08_setup-hub-kubeconfigs.sh
#   sudo ./10_setup-k8s-console-logs-to-otel.sh  (on EC2, log forwarder venv)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

echo ">>> Uploading discovery agent + NiFi app log forwarder..."
$SSH "sudo mkdir -p /opt/uob-nifi-hub-agent /opt/uob-k8s-log-forwarder && sudo chown ec2-user:ec2-user /opt/uob-nifi-hub-agent /opt/uob-k8s-log-forwarder"
$SCP "${REPO_DIR}/scripts/nifi_discovery.py" "${REPO_DIR}/scripts/nifi_hub_agent.py" \
  "${EC2_HOST}:/tmp/"
$SSH "mv /tmp/nifi_discovery.py /tmp/nifi_hub_agent.py /opt/uob-nifi-hub-agent/ && chmod +x /opt/uob-nifi-hub-agent/*.py"

echo ">>> Installing systemd unit + Prometheus file_sd..."
$SSH bash << 'REMOTE'
set -euo pipefail
sudo mkdir -p /etc/prometheus/file_sd
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd
echo '[]' | sudo tee /etc/prometheus/file_sd/nifi_federate.json >/dev/null
sudo chown ec2-user:ec2-user /etc/prometheus/file_sd/nifi_federate.json

# Replace static nifi_federate target with file_sd (idempotent-ish)
if grep -q 'file_sd_configs' /etc/prometheus/prometheus.yml && grep -q 'nifi_federate.json' /etc/prometheus/prometheus.yml; then
  echo "Prometheus nifi_federate file_sd already configured"
else
  sudo cp /etc/prometheus/prometheus.yml "/etc/prometheus/prometheus.yml.bak.$(date +%Y%m%d%H%M%S)"
  sudo python3 << 'PY'
from pathlib import Path
import re
path = Path("/etc/prometheus/prometheus.yml")
text = path.read_text()
block = """
  - job_name: nifi_federate
    scrape_interval: 30s
    honor_labels: true
    metrics_path: /federate
    params:
      match[]:
        - '{job="dfx-nifi-web"}'
    file_sd_configs:
      - files:
          - /etc/prometheus/file_sd/nifi_federate.json
        refresh_interval: 30s
"""
if re.search(r"^\s+- job_name: nifi_federate\n", text, re.M):
    text = re.sub(
        r"^\s+- job_name: nifi_federate\n.*?(?=^\s+- job_name:|\Z)",
        block + "\n",
        text,
        count=1,
        flags=re.M | re.S,
    )
else:
    text = text.rstrip() + "\n" + block + "\n"
path.write_text(text)
print("Updated prometheus.yml nifi_federate job")
PY
  promtool check config /etc/prometheus/prometheus.yml
fi

sudo tee /etc/systemd/system/nifi-hub-agent.service > /dev/null << 'UNIT'
[Unit]
Description=NiFi hub agent (discovery, port-forward, logs to OTel)
After=network-online.target otelcol-contrib.service
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=PYTHONPATH=/opt/uob-nifi-hub-agent
Environment=NIFI_KUBECONFIG_DIR=/etc/prometheus/kubeconfig
Environment=NIFI_DISCOVERY_INTERVAL=60
Environment=OTEL_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
Environment=NIFI_LOG_CONTAINER=nifi
Environment=NIFI_LOG_MODE=app
Environment=NIFI_APP_LOG_CONTAINER=app-log
Environment=NIFI_APP_LOG_PATH=/opt/nifi/nifi-current/logs/nifi-app.log
Environment=NIFI_APP_LOG_TAIL_LINES=500
WorkingDirectory=/opt/uob-nifi-hub-agent
ExecStart=/usr/bin/python3 /opt/uob-nifi-hub-agent/nifi_hub_agent.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT

sudo mkdir -p /var/lib/nifi-hub-agent
sudo chown ec2-user:ec2-user /var/lib/nifi-hub-agent

sudo systemctl daemon-reload
sudo systemctl disable --now nifi-prometheus-portforward 2>/dev/null || true
sudo systemctl disable --now nifi-kubectl-logs-otel 2>/dev/null || true
sudo systemctl enable nifi-hub-agent
sudo systemctl restart nifi-hub-agent
sleep 5
sudo systemctl restart prometheus
sleep 3

echo "--- nifi-hub-agent ---"
systemctl --no-pager --full status nifi-hub-agent | head -15
echo "--- file_sd ---"
cat /etc/prometheus/file_sd/nifi_federate.json
echo "--- nifi series count ---"
curl -sf --data-urlencode 'query=count({__name__=~"nifi_.*"})' http://localhost:9090/api/v1/query | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else '0')"
REMOTE

echo ""
echo "Done. Metrics: Prometheus job nifi_federate + file_sd (auto-updated)."
echo "Logs: OTel :4318 → Fluent Bit → uob-logs-* (not Prometheus)."
echo "Check: journalctl -u nifi-hub-agent -f"
