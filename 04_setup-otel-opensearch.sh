#!/bin/bash
#===============================================================================
# OpenSearch Server — Native Install (no Docker)
# Amazon Linux 2023
#
# Architecture:
# App Servers ───► OTel Collector ───► OpenSearch ◄──► Dashboards
# │
# └──► S3 (uob-application-logs) — archival
#
# This script sets up THIS server as the OpenSearch + Dashboards endpoint.
# OTel Collector runs on each app server and ships logs directly here.
#===============================================================================
set -euo pipefail

OPENSEARCH_VERSION="2.x"
OPENSEARCH_PORT="9201"
DASHBOARDS_PORT="5601"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# ── 1. System deps ────────────────────────────────────────────────────────────
install_deps() {
	info "Installing Java 17..."
	sudo dnf install -y java-17-amazon-corretto-devel >/dev/null 2>&1 || {
		error "Failed to install Java. Run with sudo."
	}
	java -version 2>&1 | head -1
}

# ── 2. OpenSearch (native RPM) ──────────────────────────────────────────────
install_opensearch() {
	info "Adding OpenSearch repository..."
	sudo tee /etc/yum.repos.d/opensearch.repo >/dev/null <<'REPO'
[opensearch]
name=OpenSearch
baseurl=https://artifacts.opensearch.org/releases/bundle/opensearch/2.x/yum
enabled=1
gpgcheck=1
gpgkey=https://artifacts.opensearch.org/publickeys/opensearch.pgp
REPO

	info "Installing OpenSearch..."
	sudo yum install -y opensearch >/dev/null 2>&1

	info "Configuring OpenSearch for single-node (no security)..."
	sudo sed -i 's/^#network.host:.*/network.host: 0.0.0.0/' /etc/opensearch/opensearch.yml
	sudo sed -i 's/^#http.port:.*/http.port: 9201/' /etc/opensearch/opensearch.yml
	sudo sed -i 's/^#discovery.type:.*/discovery.type: single-node/' /etc/opensearch/opensearch.yml

	# Disable security plugin for simplicity (add later if needed)
	sudo sed -i 's/^plugins.security.disabled:.*/plugins.security.disabled: true/' \
		/etc/opensearch/opensearch.yml 2>/dev/null || true
	echo "plugins.security.disabled: true" | sudo tee -a /etc/opensearch/opensearch.yml >/dev/null

	sudo systemctl daemon-reload
	sudo systemctl enable opensearch
	sudo systemctl start opensearch

	info "OpenSearch installed and started on port $OPENSEARCH_PORT"
}

# ── 3. OpenSearch Dashboards (native RPM) ───────────────────────────────────
install_dashboards() {
	info "Adding OpenSearch Dashboards repository..."
	sudo tee /etc/yum.repos.d/opensearch-dashboards.repo >/dev/null <<'REPO'
[opensearch-dashboards]
name=OpenSearch Dashboards
baseurl=https://artifacts.opensearch.org/releases/bundle/opensearch-dashboards/2.x/yum
enabled=1
gpgcheck=1
gpgkey=https://artifacts.opensearch.org/publickeys/opensearch.pgp
REPO

	info "Installing OpenSearch Dashboards..."
	sudo yum install -y opensearch-dashboards >/dev/null 2>&1

	info "Configuring Dashboards..."
	sudo sed -i 's/^#server.host:.*/server.host: 0.0.0.0/' /etc/opensearch-dashboards/opensearch_dashboards.yml
	sudo sed -i 's/^#server.port:.*/server.port: 5601/' /etc/opensearch-dashboards/opensearch_dashboards.yml
	sudo sed -i 's|^#opensearch.hosts:.*|opensearch.hosts: ["http://localhost:9201"]|' \
		/etc/opensearch-dashboards/opensearch_dashboards.yml

	# Disable security
	echo "opensearch_security.disabled: true" | sudo tee -a /etc/opensearch-dashboards/opensearch_dashboards.yml >/dev/null

	sudo systemctl daemon-reload
	sudo systemctl enable opensearch-dashboards
	sudo systemctl start opensearch-dashboards

	info "OpenSearch Dashboards installed on port $DASHBOARDS_PORT"
}

# ── 4. Wait for OpenSearch ──────────────────────────────────────────────────
wait_for_opensearch() {
	info "Waiting for OpenSearch to be ready..."
	for i in $(seq 1 30); do
		if curl -s http://localhost:9201 >/dev/null 2>&1; then
			info "OpenSearch is ready!"
			return
		fi
		sleep 2
	done
	warn "Timed out. Check: sudo journalctl -u opensearch -n 50 --no-pager"
}

# ── 5. Create index template ──────────────────────���─────────────────────────
create_index_template() {
	info "Creating index template for uob logs..."

	curl -s -X PUT "http://localhost:9201/_index_template/uob-app-logs" \
		-H "Content-Type: application/json" \
		-d '{
	"index_patterns": ["uob-logs-*"],
	"template": {
		"settings": {
			"number_of_shards": 1,
			"number_of_replicas": 0
		},
		"mappings": {
			"properties": {
				"timestamp": { "type": "date" },
				"app_name": { "type": "keyword" },
				"log_level": { "type": "keyword" },
				"service": { "type": "keyword" },
				"message": { "type": "text" },
				"environment": { "type": "keyword" },
				"server_ip": { "type": "ip" },
				"body": { "type": "text" }
			}
		}
	}
}' >/dev/null && info "Index template created" || warn "Index template creation failed"
}

# ── 6. Summary ────────────────────────────────────────────────────────────────
print_summary() {
	local public_ip
	public_ip=$(curl -s http://checkip.amazonaws.com 2>/dev/null || echo "<public-ip>")

	echo ""
	echo "╔═══════════════════════════════════════════════════════════════╗"
	echo "║ OpenSearch Server is ready (native install)                  ║"
	echo "╚═══════════════════════════════════════════════════════════════╝"
	echo ""
	echo "  OpenSearch API:  http://$public_ip:$OPENSEARCH_PORT"
	echo "  Dashboards UI:   http://$public_ip:$DASHBOARDS_PORT"
	echo ""
	echo "── Security Group — open these ports ──"
	echo "  $OPENSEARCH_PORT → OpenSearch API (must allow your app servers)"
	echo "  $DASHBOARDS_PORT → Dashboards (your browser)"
	echo ""
	echo "── Useful commands ──"
	echo "  Status:           sudo systemctl status opensearch"
	echo "  Dashboards status: sudo systemctl status opensearch-dashboards"
	echo "  View logs:        sudo journalctl -u opensearch -f"
	echo "  Restart:          sudo systemctl restart opensearch"
	echo "  OpenSearch config: /etc/opensearch/opensearch.yml"
	echo ""
	echo "── OTel config for each app server ────────────────────────────"
	echo ""
	echo "  receivers:"
	echo "    filelog:"
	echo "      include: [ /var/log/app/*.log, /var/log/app/*.json ]"
	echo "      start_at: beginning"
	echo ""
	echo "  processors:"
	echo "    batch:"
	echo "      timeout: 10s"
	echo "      send_batch_size: 512"
	echo "    attributes:"
	echo "      actions:"
	echo "        - key: environment"
	echo "          value: production"
	echo "          action: upsert"
	echo ""
	echo "  exporters:"
	echo "    opensearch:"
	echo "      dataset_name: uob-logs"
	echo "      namespace: production"
	echo "      http:"
	echo "        endpoint: \"http://$public_ip:$OPENSEARCH_PORT\""
	echo "        tls:"
	echo "          insecure: true"
	echo "      index: \"uob-logs-%{yyyy.MM.dd}\""
	echo "    awss3:"
	echo "      bucket: uob-application-logs"
	echo "      region: us-east-1"
	echo ""
	echo "  service:"
	echo "    pipelines:"
	echo "      logs:"
	echo "        receivers: [filelog]"
	echo "        processors: [attributes, batch]"
	echo "        exporters: [opensearch, awss3]"
	echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
	echo ""
	echo "╔═══════════════════════════════════════════════════════════════╗"
	echo "║ OpenSearch Server Setup (Native)                             ║"
	echo "║ OTel agents → OpenSearch → Dashboards                        ║"
	echo "╚═══════════════════════════════════════════════════════════════╝"
	echo ""

	install_deps
	install_opensearch
	install_dashboards
	wait_for_opensearch
	create_index_template
	print_summary
}

main "$@"
