#!/bin/bash
set -e

echo "========================================"
echo "  Grafana NiFi processor failure alerts"
echo "  Observability: observability.spoc.cloudera-labs.com"
echo "========================================"

SSH_KEY="/Users/vikassri/Desktop/uob_poc/mail-server.pem"
HOST="ec2-user@observability.spoc.cloudera-labs.com"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $HOST"
ALERT_EMAIL="${NIFI_ALERT_EMAIL:-nifi-alerts@local.test}"
NIFI_BULLETIN_WINDOW="${NIFI_BULLETIN_WINDOW:-5m}"
NIFI_PROM_JOB="${NIFI_PROM_JOB:-dfx-nifi-web}"
NIFI_PARENT_ID="${NIFI_PARENT_ID:-f5876d95-01a0-1000-0000-00005a7a3b62}"
NIFI_FLOW_TITLE="${NIFI_FLOW_TITLE:-Hello World}"
NIFI_FLOW_SLUG="${NIFI_FLOW_SLUG:-hello-world}"
NIFI_K8S_NAMESPACE="${NIFI_K8S_NAMESPACE:-dfx-nifi-deployment-ns}"
NIFI_RULE_UID_BULLETIN="${NIFI_RULE_UID_BULLETIN:-nifi-proc-bulletin}"
NIFI_RULE_UID_TERMINATED="${NIFI_RULE_UID_TERMINATED:-nifi-proc-thread-terminated}"
NIFI_RULE_UID_COUNTER="${NIFI_RULE_UID_COUNTER:-nifi-proc-failure-counter}"
NIFI_RULE_UID_QUEUE_STALLED="${NIFI_RULE_UID_QUEUE_STALLED:-nifi-conn-queue-stalled}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ">>> Configuring Grafana SMTP -> Mailpit (localhost:1025)..."
$SSH bash << 'REMOTE'
set -e
sudo mkdir -p /etc/systemd/system/grafana-server.service.d
sudo tee /etc/systemd/system/grafana-server.service.d/smtp-mailpit.conf > /dev/null << 'ENV'
[Service]
Environment=GF_SMTP_ENABLED=true
Environment=GF_SMTP_HOST=localhost:1025
Environment=GF_SMTP_FROM_ADDRESS=grafana@observability.local
Environment=GF_SMTP_FROM_NAME=Grafana NiFi Alerts
Environment=GF_SMTP_SKIP_VERIFY=true
Environment=GF_UNIFIED_ALERTING_ENABLED=true
ENV
sudo systemctl daemon-reload
sudo systemctl restart grafana-server
sleep 4
curl -s -o /dev/null -w 'grafana_http=%{http_code}\n' http://127.0.0.1:3000/api/health
REMOTE

echo ">>> Folder, Mailpit contact point, and alert rules..."
echo "    Flow: ${NIFI_FLOW_TITLE} (parent_id=${NIFI_PARENT_ID})"
echo "    Bulletin window=${NIFI_BULLETIN_WINDOW}"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no \
  "$SCRIPT_DIR/scripts/update_grafana_nifi_alerts.py" \
  "$SCRIPT_DIR/scripts/grafana_alerting.py" \
  "$HOST:/tmp/"
$SSH bash -s << REMOTE
set -e
ALERT_EMAIL='${ALERT_EMAIL}'

python3 << 'PY'
import json
import urllib.request
import base64

BASE = "http://127.0.0.1:3000"
AUTH = base64.b64encode(b"admin:admin").decode()
ALERT_EMAIL = "${ALERT_EMAIL}"


def request(method, path, data=None):
    headers = {"Authorization": f"Basic {AUTH}"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode()
        print(f"HTTP {err.code} {path}: {raw[:800]}", file=sys.stderr)
        raise


def get_prom_uid():
    _, ds = request("GET", "/api/datasources")
    for item in ds:
        if item.get("type") == "prometheus" and item.get("isDefault"):
            return item["uid"]
    for item in ds:
        if item.get("type") == "prometheus":
            return item["uid"]
    raise RuntimeError("No Prometheus datasource")


prom_uid = get_prom_uid()
print("prometheus_uid", prom_uid)

# Folder
_, folders = request("GET", "/api/folders")
folder_uid = next((f["uid"] for f in folders if f.get("title") == "NiFi"), None)
if not folder_uid:
    status, folder = request("POST", "/api/folders", {"title": "NiFi", "uid": "nifi-obs"})
    folder_uid = folder["uid"]
    print("created_folder", folder_uid)
else:
    print("folder", folder_uid)

# Contact point (Mailpit)
cp_name = "mailpit-nifi"
_, cps = request("GET", "/api/v1/provisioning/contact-points")
if not any(cp.get("name") == cp_name for cp in cps):
    request(
        "POST",
        "/api/v1/provisioning/contact-points",
        {
            "name": cp_name,
            "type": "email",
            "settings": {"addresses": ALERT_EMAIL, "singleEmail": False},
            "disableResolveMessage": False,
        },
    )
    print("contact_point_created", cp_name)
else:
    print("contact_point_exists", cp_name)

# Default notification policy -> mailpit
request(
    "PUT",
    "/api/v1/provisioning/policies",
    {
        "receiver": cp_name,
        "group_by": ["grafana_folder", "alertname"],
        "group_wait": "30s",
        "group_interval": "5m",
        "repeat_interval": "4h",
        "routes": [],
    },
)

print("contact_point_ready", cp_name)
PY

export NIFI_BULLETIN_WINDOW='${NIFI_BULLETIN_WINDOW}'
export NIFI_PROM_JOB='${NIFI_PROM_JOB}'
export NIFI_PARENT_ID='${NIFI_PARENT_ID}'
export NIFI_FLOW_TITLE='${NIFI_FLOW_TITLE}'
export NIFI_FLOW_SLUG='${NIFI_FLOW_SLUG}'
export NIFI_K8S_NAMESPACE='${NIFI_K8S_NAMESPACE}'
export NIFI_RULE_UID_BULLETIN='${NIFI_RULE_UID_BULLETIN}'
export NIFI_RULE_UID_TERMINATED='${NIFI_RULE_UID_TERMINATED}'
export NIFI_RULE_UID_COUNTER='${NIFI_RULE_UID_COUNTER}'
export NIFI_RULE_UID_QUEUE_STALLED='${NIFI_RULE_UID_QUEUE_STALLED}'
python3 /tmp/update_grafana_nifi_alerts.py
REMOTE

echo ""
echo "========================================"
echo "  NiFi Grafana alerts configured"
echo "========================================"
echo "  Alert emails -> Mailpit: http://observability.spoc.cloudera-labs.com:8025"
echo "  Recipient: ${ALERT_EMAIL}"
echo "  Flow: ${NIFI_FLOW_TITLE} (see README — NiFi metrics and Grafana alerts)"
echo "  Grafana -> Alerting -> Alert rules (folder NiFi)"
echo "========================================"
