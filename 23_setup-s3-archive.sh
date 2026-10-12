#!/bin/bash
# Enable S3 archival on the observability EC2:
#   • Logs  — Fluent Bit S3 output (parallel to OpenSearch + Elasticsearch)
#   • Metrics — daily Prometheus TSDB snapshot upload via systemd timer
#
# Run from laptop (repo root):
#   ./23_setup-s3-archive.sh
#
# Prerequisites: 05_setup-observability-log-pipeline.sh (Fluent Bit), 02_setup-prometheus.sh
# EC2 instance role (or env creds) needs s3:PutObject + s3:ListBucket on the bucket.
#
# Env:
#   SSH_KEY, EC2_HOST, S3_BUCKET (default uob-application-logs), AWS_REGION (default us-east-1)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $EC2_HOST"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

S3_BUCKET="${S3_BUCKET:-uob-application-logs}"
AWS_REGION="${AWS_REGION:-us-east-1}"
S3_LOG_PREFIX="${S3_LOG_PREFIX:-logs/fluent-bit}"

echo "========================================"
echo " S3 archive — logs (Fluent Bit) + metrics (Prometheus snapshots)"
echo " Bucket: s3://${S3_BUCKET}/"
echo "========================================"

$SCP "${REPO_DIR}/scripts/upload_prometheus_snapshot_to_s3.sh" "${EC2_HOST}:/tmp/"
$SSH bash -s <<REMOTE
set -euo pipefail
S3_BUCKET='${S3_BUCKET}'
AWS_REGION='${AWS_REGION}'
S3_LOG_PREFIX='${S3_LOG_PREFIX}'

sudo install -m 755 /tmp/upload_prometheus_snapshot_to_s3.sh /usr/local/bin/upload_prometheus_snapshot_to_s3.sh

echo ">>> Preflight: AWS access to bucket..."
if ! aws s3 ls "s3://\${S3_BUCKET}/" --region "\${AWS_REGION}" >/dev/null 2>&1; then
  echo "Cannot list s3://\${S3_BUCKET}/ — fix IAM or AWS credentials on this host." >&2
  exit 1
fi

FB=/etc/fluent-bit/fluent-bit.conf
if [[ ! -f "\$FB" ]]; then
  echo "Missing \$FB — run 05_setup-observability-log-pipeline.sh first." >&2
  exit 1
fi

if ! grep -q 'Name[[:space:]]*s3' "\$FB" 2>/dev/null; then
  echo ">>> Adding Fluent Bit S3 output (gzip objects, parallel to OpenSearch/ES)..."
  sudo cp "\$FB" "\${FB}.bak.\$(date +%Y%m%d%H%M%S)"
  sudo mkdir -p /var/fluent-bit/s3
  sudo tee -a "\$FB" >/dev/null <<'FBOUT'

# Archival copy (POC) — same log stream as OpenSearch; query live data in Kibana, use S3 for backup/audit
[OUTPUT]
    Name                  s3
    Match                 *
    bucket                __S3_BUCKET__
    region                __AWS_REGION__
    total_file_size       10M
    upload_timeout        10m
    use_put_object        On
    compression           gzip
    store_dir             /var/fluent-bit/s3
    s3_key_format         /__S3_LOG_PREFIX__/%Y/%m/%d/%H_%M_%S_\$UUID.gz
    retry_limit           3
FBOUT
  sudo sed -i "s|__S3_BUCKET__|\${S3_BUCKET}|g; s|__AWS_REGION__|\${AWS_REGION}|g; s|__S3_LOG_PREFIX__|\${S3_LOG_PREFIX}|g" "\$FB"
  sleep 1
else
  echo "Fluent Bit S3 output already present in \$FB"
fi

sudo mkdir -p /etc/systemd/system/fluent-bit.service.d
sudo tee /etc/systemd/system/fluent-bit.service.d/aws-env.conf >/dev/null <<FBENV
[Service]
Environment=AWS_REGION=${AWS_REGION}
Environment=AWS_DEFAULT_REGION=${AWS_REGION}
Environment=HOME=/var/fluent-bit
FBENV
sudo systemctl daemon-reload
sudo systemctl restart fluent-bit

echo ">>> Enabling Prometheus admin snapshot API (if needed)..."
if ! systemctl show prometheus -p ExecStart 2>/dev/null | grep -q 'enable-admin-api'; then
  sudo mkdir -p /etc/systemd/system/prometheus.service.d
  sudo tee /etc/systemd/system/prometheus.service.d/admin-api.conf >/dev/null <<PROMDROP
[Service]
ExecStart=
ExecStart=/usr/local/bin/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/var/lib/prometheus --web.listen-address=:9090 --web.enable-admin-api
PROMDROP
  sudo systemctl daemon-reload
  sudo systemctl restart prometheus
  sleep 3
fi

echo ">>> Installing Prometheus snapshot upload timer (daily 03:15 UTC)..."
sudo tee /etc/systemd/system/prometheus-s3-snapshot.service >/dev/null <<UNIT
[Unit]
Description=Upload Prometheus TSDB snapshot to S3
After=network-online.target prometheus.service
Wants=network-online.target

[Service]
Type=oneshot
Environment=S3_BUCKET=${S3_BUCKET}
Environment=AWS_REGION=${AWS_REGION}
Environment=S3_PREFIX=metrics/prometheus
ExecStart=/usr/local/bin/upload_prometheus_snapshot_to_s3.sh
UNIT

sudo tee /etc/systemd/system/prometheus-s3-snapshot.timer >/dev/null <<'TIMER'
[Unit]
Description=Daily Prometheus snapshot to S3

[Timer]
OnCalendar=*-*-* 03:15:00 UTC
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
TIMER

sudo systemctl daemon-reload
sudo systemctl enable --now prometheus-s3-snapshot.timer
systemctl list-timers prometheus-s3-snapshot.timer --no-pager

echo ">>> Optional: run one metrics upload now..."
if sudo /usr/local/bin/upload_prometheus_snapshot_to_s3.sh; then
  echo "Initial Prometheus snapshot upload OK."
else
  echo "WARN: first snapshot upload failed — check prometheus and IAM; timer will retry." >&2
fi

echo ""
echo "Verify:"
echo "  aws s3 ls s3://\${S3_BUCKET}/\${S3_LOG_PREFIX}/ --recursive | tail"
echo "  aws s3 ls s3://\${S3_BUCKET}/metrics/prometheus/ --recursive | tail"
echo "  journalctl -u fluent-bit -n 30 --no-pager"
REMOTE

echo ""
echo "========================================"
echo " S3 archive enabled on observability host"
echo "========================================"
