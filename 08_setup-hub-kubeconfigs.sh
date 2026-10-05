#!/bin/bash
# Copy all product kubeconfigs to the observability EC2 hub.
#
# Local (from repo root):
#   ./08_setup-hub-kubeconfigs.sh
#
# Env overrides:
#   SSH_KEY=./mail-server.pem
#   EC2_HOST=ec2-user@observability.spoc.cloudera-labs.com
#   REPO_DIR=/path/to/uob_poc
#
# Remote layout:
#   /etc/prometheus/kubeconfig/cdf.yaml
#   /etc/prometheus/kubeconfig/cde.yaml
#   ... (one file per cluster, mode 600, owner ec2-user)
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SSH_KEY="${SSH_KEY:-${REPO_DIR}/mail-server.pem}"
EC2_HOST="${EC2_HOST:-ec2-user@observability.spoc.cloudera-labs.com}"
REMOTE_KCFG_DIR="/etc/prometheus/kubeconfig"

declare -a PAIRS=(
  "cdf:cdf_kubeconfig.yaml"
  "cde:cde_kubeconfig.yaml"
  "cai:cai_kubeconfig.yaml"
  "csa:csa_kubeconfig.yaml"
  "cle:cle_kubeconfig.yaml"
  "cwo:cwo_kubeconfig.yaml"
  "polaris:polaris_kubeconfig.yaml"
)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
die() { echo -e "${RED}[✗]${NC} $1" >&2; exit 1; }

[[ -f "$SSH_KEY" ]] || die "SSH key not found: $SSH_KEY"

missing=0
for entry in "${PAIRS[@]}"; do
  local_file="${entry#*:}"
  if [[ ! -f "${REPO_DIR}/${local_file}" ]]; then
    warn "Missing local file: ${REPO_DIR}/${local_file}"
    missing=$((missing + 1))
  fi
done
[[ "$missing" -eq 0 ]] || die "Fix missing kubeconfig files before continuing."

echo "========================================"
echo "  Hub kubeconfigs → observability EC2"
echo "  Host: ${EC2_HOST}"
echo "========================================"

STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

for entry in "${PAIRS[@]}"; do
  short="${entry%%:*}"
  local_file="${entry#*:}"
  cp "${REPO_DIR}/${local_file}" "${STAGING}/${short}.yaml"
  info "Staged ${short}.yaml"
done

info "Uploading to EC2 (scp)..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$EC2_HOST" "rm -rf /tmp/hub-kubeconfigs && mkdir -p /tmp/hub-kubeconfigs"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "${STAGING}"/*.yaml "${EC2_HOST}:/tmp/hub-kubeconfigs/"

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$EC2_HOST" bash << 'REMOTE'
set -euo pipefail
REMOTE_KCFG_DIR="/etc/prometheus/kubeconfig"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "Installing kubectl..."
  curl -sLO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
  chmod +x kubectl
  sudo mv kubectl /usr/local/bin/
fi

sudo mkdir -p "$REMOTE_KCFG_DIR"
sudo mkdir -p /tmp/hub-kubeconfigs 2>/dev/null || true

for f in /tmp/hub-kubeconfigs/*.yaml; do
  [[ -f "$f" ]] || continue
  base=$(basename "$f")
  sudo cp "$f" "${REMOTE_KCFG_DIR}/${base}"
  sudo chmod 600 "${REMOTE_KCFG_DIR}/${base}"
  sudo chown ec2-user:ec2-user "${REMOTE_KCFG_DIR}/${base}"
done

echo "--- Installed files ---"
sudo ls -la "$REMOTE_KCFG_DIR"

echo "--- Connectivity check (kubectl get ns, 15s timeout) ---"
for kc in "$REMOTE_KCFG_DIR"/*.yaml; do
  name=$(basename "$kc" .yaml)
  if kubectl --kubeconfig="$kc" get ns --request-timeout=15s >/dev/null 2>&1; then
    count=$(kubectl --kubeconfig="$kc" get ns --no-headers 2>/dev/null | wc -l)
    echo "  OK  ${name} (${count} namespaces)"
  else
    echo "  FAIL ${name} (API unreachable or auth error — check VPC/tunnel from EC2)"
  fi
done

rm -rf /tmp/hub-kubeconfigs
REMOTE

echo ""
info "Done. Kubeconfigs on EC2: ${REMOTE_KCFG_DIR}/*.yaml"
echo "  Example: kubectl --kubeconfig=/etc/prometheus/kubeconfig/cle.yaml get nodes"
