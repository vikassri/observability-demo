#!/bin/bash
# OpenSearch Dashboards UI login (admin) + OpenSearch Security (strong password required by OpenSearch 2.19).
# Run on observability EC2:
#   export OPENSEARCH_INITIAL_ADMIN_PASSWORD='MyAdmin123!@#POC'
#   sudo -E ./07_setup-opensearch-dashboards-ui.sh
#
# UI:  http://<host>:5602  — user admin, password = value of OPENSEARCH_INITIAL_ADMIN_PASSWORD
# Kibana (logs) stays on :5601 without OpenSearch login.
set -euo pipefail

ADMIN_USER="${ADMIN_USER:-admin}"
OS_PASS="${OPENSEARCH_INITIAL_ADMIN_PASSWORD:?Set OPENSEARCH_INITIAL_ADMIN_PASSWORD (min 8 chars, strong)}"
OS_PORT="${OS_PORT:-9201}"
DASH_PORT="${DASH_PORT:-5602}"
TOOLS="/usr/share/opensearch/plugins/opensearch-security/tools"
SEC_DIR="/etc/opensearch/opensearch-security"
JAVA_HOME="${OPENSEARCH_JAVA_HOME:-/usr/lib/jvm/java-17-amazon-corretto}"

[ "$(id -u)" -eq 0 ] || { echo "sudo required"; exit 1; }

export OPENSEARCH_JAVA_HOME="$JAVA_HOME"

echo "[1/6] Stop OpenSearch"
systemctl stop opensearch || true

echo "[2/6] Demo security (TLS + internal users)"
sed -i '/^plugins.security/d' /etc/opensearch/opensearch.yml
grep -q '^http.port:' /etc/opensearch/opensearch.yml || echo "http.port: ${OS_PORT}" >>/etc/opensearch/opensearch.yml

(cd "$TOOLS" && OPENSEARCH_INITIAL_ADMIN_PASSWORD="$OS_PASS" ./install_demo_configuration.sh -y)

echo "[3/6] HTTP with basic auth (no HTTPS on 9201 for Fluent Bit POC)"
sed -i 's/^plugins.security.ssl.http.enabled:.*/plugins.security.ssl.http.enabled: false/' /etc/opensearch/opensearch.yml

echo "[4/6] Start OpenSearch + securityadmin"
systemctl start opensearch
for _ in $(seq 1 30); do
  curl -sk "https://127.0.0.1:${OS_PORT}/" >/dev/null 2>&1 && break
  sleep 2
done
(cd "$TOOLS" && ./securityadmin.sh -cd "$SEC_DIR" -icl -key /etc/opensearch/kirk-key.pem \
  -cert /etc/opensearch/kirk.pem -cacert /etc/opensearch/root-ca.pem -nhnv)

systemctl restart opensearch
sleep 15
curl -sf -u "${ADMIN_USER}:${OS_PASS}" "http://127.0.0.1:${OS_PORT}/" >/dev/null

echo "[5/6] Fluent Bit → OpenSearch with basic auth"
if grep -q 'Name.*opensearch' /etc/fluent-bit/fluent-bit.conf; then
  grep -q HTTP_User /etc/fluent-bit/fluent-bit.conf || \
    sed -i '/Name.*opensearch/a\    HTTP_User          '"${ADMIN_USER}"'\n    HTTP_Passwd        '"${OS_PASS}" /etc/fluent-bit/fluent-bit.conf
  sed -i "s/HTTP_User.*/HTTP_User ${ADMIN_USER}/" /etc/fluent-bit/fluent-bit.conf
  sed -i "s/HTTP_Passwd.*/HTTP_Passwd ${OS_PASS}/" /etc/fluent-bit/fluent-bit.conf
fi
systemctl restart fluent-bit

echo "[6/6] OpenSearch Dashboards on :${DASH_PORT}"
if [ -d /usr/share/opensearch-dashboards/plugins/securityDashboards.disabled ]; then
  mv /usr/share/opensearch-dashboards/plugins/securityDashboards.disabled \
    /usr/share/opensearch-dashboards/plugins/securityDashboards
fi

cat >/etc/opensearch-dashboards/opensearch_dashboards.yml <<EOF
server.host: "0.0.0.0"
server.port: ${DASH_PORT}
opensearch.hosts: ["http://127.0.0.1:${OS_PORT}"]
opensearch.username: "${ADMIN_USER}"
opensearch.password: "${OS_PASS}"
opensearch.ssl.verificationMode: none
EOF

systemctl enable opensearch-dashboards
systemctl restart opensearch-dashboards

echo ""
echo "OpenSearch Dashboards: http://$(curl -s http://checkip.amazonaws.com 2>/dev/null || echo 127.0.0.1):${DASH_PORT}"
echo "Login: ${ADMIN_USER} / (your OPENSEARCH_INITIAL_ADMIN_PASSWORD)"
echo "Kibana logs UI unchanged: http://<host>:5601 (data view uob-logs-*)"
