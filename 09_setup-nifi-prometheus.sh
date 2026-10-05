#!/bin/bash
set -e

echo "========================================"
echo "  NiFi Metrics -> EC2 Prometheus"
echo "  Observability: observability.spoc.cloudera-labs.com"
echo "========================================"

SSH_KEY="/Users/vikassri/Desktop/uob_poc/mail-server.pem"
HOST="ec2-user@observability.spoc.cloudera-labs.com"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $HOST"
KUBECONFIG_LOCAL="/Users/vikassri/Desktop/uob_poc/cdf_kubeconfig.yaml"

if [[ -z "${KNOX_NIFI_JWT:-}" ]]; then
  echo "ERROR: Set KNOX_NIFI_JWT to a Knox Bearer token before running."
  exit 1
fi

echo ">>> Uploading Knox JWT (not logged)..."
$SSH "mkdir -p /tmp/nifi-setup && chmod 700 /tmp/nifi-setup"
printf '%s' "$KNOX_NIFI_JWT" | $SSH "cat > /tmp/nifi-setup/knox-nifi.jwt"

echo ">>> Installing kubectl + cluster kubeconfig (for DFX Prometheus federation fallback)..."
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "$KUBECONFIG_LOCAL" "$HOST:/tmp/nifi-setup/cdf_kubeconfig.yaml"
$SSH bash << 'REMOTE'
set -e
if ! command -v kubectl >/dev/null 2>&1; then
  curl -sLO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
  chmod +x kubectl
  sudo mv kubectl /usr/local/bin/
fi
sudo mkdir -p /etc/prometheus/secrets /etc/prometheus/kubeconfig
sudo mv /tmp/nifi-setup/knox-nifi.jwt /etc/prometheus/secrets/knox-nifi.jwt
sudo chmod 600 /etc/prometheus/secrets/knox-nifi.jwt
sudo chown ec2-user:ec2-user /etc/prometheus/secrets/knox-nifi.jwt
sudo cp /tmp/nifi-setup/cdf_kubeconfig.yaml /etc/prometheus/kubeconfig/cdf.yaml
sudo chmod 600 /etc/prometheus/kubeconfig/cdf.yaml
sudo chown ec2-user:ec2-user /etc/prometheus/kubeconfig/cdf.yaml
rm -rf /tmp/nifi-setup
REMOTE

echo ">>> Configuring Prometheus scrape jobs + kubectl port-forward..."
$SSH bash << 'REMOTE'
set -e
sudo cp /etc/prometheus/prometheus.yml /etc/prometheus/prometheus.yml.bak.$(date +%Y%m%d%H%M%S)

sudo tee /etc/prometheus/prometheus.yml > /dev/null << 'YML'
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]

  - job_name: "node_exporter"
    static_configs:
      - targets: ["localhost:9100"]

  - job_name: nifi
    scheme: https
    metrics_path: /dfx-nifi-deployment-ns/nifi-api/flow/metrics/prometheus
    scrape_interval: 30s
    scrape_timeout: 25s
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/knox-nifi.jwt
    tls_config:
      insecure_skip_verify: true
    static_configs:
      - targets: ["cdf.cdf01.spoc.cloudera-labs.com:443"]
        labels:
          cluster: cdf01
          deployment: dfx-nifi-deployment-ns

  - job_name: nifi_federate
    scrape_interval: 30s
    honor_labels: true
    metrics_path: /federate
    params:
      match[]:
        - '{job="dfx-nifi-web"}'
    static_configs:
      - targets: ["localhost:19090"]

YML

sudo tee /etc/systemd/system/nifi-prometheus-portforward.service > /dev/null << 'SVC'
[Unit]
Description=Kubectl port-forward to DFX NiFi Prometheus
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
Environment=KUBECONFIG=/etc/prometheus/kubeconfig/cdf.yaml
ExecStart=/usr/local/bin/kubectl port-forward -n dfx-nifi-deployment-ns svc/prometheus 19090:19090 --address=127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC

promtool check config /etc/prometheus/prometheus.yml
sudo systemctl daemon-reload
sudo systemctl enable nifi-prometheus-portforward
sudo systemctl restart nifi-prometheus-portforward
sleep 3
sudo systemctl restart prometheus
sleep 3

echo ">>> Port-forward status:"
sudo systemctl status nifi-prometheus-portforward --no-pager | head -8

echo ">>> NiFi-related Prometheus targets:"
curl -s 'http://localhost:9090/api/v1/targets' | python3 -c "
import sys, json
for t in json.load(sys.stdin)['data']['activeTargets']:
    job = t['labels'].get('job', '')
    if 'nifi' in job:
        err = (t.get('lastError') or '')[:120]
        print(f\"  {job}: {t['health']} {err}\")
"

echo ">>> NiFi series count:"
curl -s --data-urlencode 'query=count({__name__=~"nifi_.*"})' http://localhost:9090/api/v1/query | python3 -c "
import sys, json
r = json.load(sys.stdin)['data']['result']
print(r[0]['value'][1] if r else '0')
"
REMOTE

echo ""
echo "========================================"
echo "  NiFi Prometheus setup complete"
echo "========================================"
echo "  Knox scrape job: nifi (may stay DOWN if JWT gets SSO 302)"
echo "  Active metrics:  nifi_federate -> job dfx-nifi-web"
echo "  Grafana:         use job=dfx-nifi-web on NiFi dashboard"
echo "========================================"
