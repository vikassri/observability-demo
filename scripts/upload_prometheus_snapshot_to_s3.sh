#!/usr/bin/env bash
# Create a Prometheus TSDB snapshot and upload it to S3.
# Intended to run on the observability EC2 (systemd timer or cron).
#
# Env:
#   PROM_URL          default http://127.0.0.1:9090
#   S3_BUCKET         default uob-application-logs
#   S3_PREFIX         default metrics/prometheus
#   AWS_REGION        default us-east-1
#   RETAIN_LOCAL_SNAP default 2  (keep newest N snapshot dirs under PROM_DATA/snapshots)
set -euo pipefail

PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"
S3_BUCKET="${S3_BUCKET:-uob-application-logs}"
S3_PREFIX="${S3_PREFIX:-metrics/prometheus}"
AWS_REGION="${AWS_REGION:-us-east-1}"
RETAIN_LOCAL_SNAP="${RETAIN_LOCAL_SNAP:-2}"
PROM_DATA="${PROM_DATA:-/var/lib/prometheus}"

log() { echo "[$(date -Is)] $*"; }

if ! command -v aws >/dev/null 2>&1; then
  log "ERROR: aws CLI not found"
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  log "ERROR: curl not found"
  exit 1
fi

resp="$(curl -sf -X POST "${PROM_URL}/api/v1/admin/tsdb/snapshot")" || {
  log "ERROR: Prometheus snapshot API failed (is prometheus running?)"
  exit 1
}

snap_name="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['data']['name'])" "$resp")"
snap_dir="${PROM_DATA}/snapshots/${snap_name}"

if [[ ! -d "$snap_dir" ]]; then
  log "ERROR: snapshot dir missing: $snap_dir"
  exit 1
fi

dest="s3://${S3_BUCKET}/${S3_PREFIX}/$(date -u +%Y/%m/%d)/${snap_name}/"
log "Uploading ${snap_dir} -> ${dest}"
aws s3 sync "$snap_dir" "$dest" --region "$AWS_REGION" --only-show-errors

log "Done."

if [[ -d "${PROM_DATA}/snapshots" ]]; then
  mapfile -t dirs < <(find "${PROM_DATA}/snapshots" -mindepth 1 -maxdepth 1 -type d | sort)
  excess=$((${#dirs[@]} - RETAIN_LOCAL_SNAP))
  if (( excess > 0 )); then
    for (( i = 0; i < excess; i++ )); do
      log "Removing old local snapshot ${dirs[i]}"
      rm -rf "${dirs[i]}"
    done
  fi
fi
