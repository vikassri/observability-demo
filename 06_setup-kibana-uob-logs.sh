#!/bin/bash
# Create Kibana data view + saved searches for uob-logs-* (Elasticsearch :9200).
# Run on observability EC2: ./06_setup-kibana-uob-logs.sh
set -euo pipefail

KIBANA="${KIBANA_URL:-http://127.0.0.1:5601}"
PATTERN="${PATTERN:-uob-logs-*}"
TITLE="${TITLE:-UOB Logs}"

create_data_view() {
  local resp id
  resp=$(curl -sf -X POST "${KIBANA}/api/data_views/data_view" \
    -H "kbn-xsrf: true" -H "Content-Type: application/json" \
    -d "{\"data_view\":{\"title\":\"${PATTERN}\",\"name\":\"${TITLE}\",\"timeFieldName\":\"@timestamp\"}}" 2>/dev/null || true)
  if [ -n "$resp" ]; then
    id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['data_view']['id'])" 2>/dev/null || true)
  fi
  if [ -z "${id:-}" ]; then
    id=$(curl -sf "${KIBANA}/api/saved_objects/_find?type=index-pattern&search_fields=title&search=uob" \
      -H "kbn-xsrf: true" | python3 -c "import sys,json; o=json.load(sys.stdin)['saved_objects']; print(o[0]['id'] if o else '')")
  fi
  [ -n "$id" ] || { echo "Could not create or find data view"; exit 1; }
  echo "$id"
}

main() {
  id=$(create_data_view)
  curl -sf -X POST "${KIBANA}/api/data_views/default" \
    -H "kbn-xsrf: true" -H "Content-Type: application/json" \
    -d "{\"data_view_id\":\"${id}\",\"force\":true}" >/dev/null

  save_search() {
    local sid="$1" title="$2" query="$3"
    curl -sf -X POST "${KIBANA}/api/saved_objects/search/${sid}" \
      -H "kbn-xsrf: true" -H "Content-Type: application/json" \
      -d "{
        \"attributes\": {
          \"title\": \"${title}\",
          \"columns\": [\"@timestamp\", \"log\", \"message\", \"pipeline\"],
          \"sort\": [[\"@timestamp\", \"desc\"]],
          \"kibanaSavedObjectMeta\": {
            \"searchSourceJSON\": \"{\\\"query\\\":{\\\"query\\\":\\\"${query}\\\",\\\"language\\\":\\\"kuery\\\"},\\\"filter\\\":[],\\\"indexRefName\\\":\\\"kibanaSavedObjectMeta.searchSourceJSON.index\\\"}\"
          }
        },
        \"references\": [{\"name\": \"kibanaSavedObjectMeta.searchSourceJSON.index\", \"type\": \"index-pattern\", \"id\": \"${id}\"}]
      }" >/dev/null || true
  }

  save_search "uob-logs-all" "UOB Logs - All" ""
  save_search "uob-logs-airflow" "UOB Logs - Airflow (CWO)" "log: *airflow* or log: *uob.product=airflow* or log: *cwo01-cwo*"
  save_search "uob-logs-trino" "UOB Logs - Trino (CLE)" "log: *uob.product=trino* or log: *io.trino* or log: *QueryMonitor* or log: *cle01-trino*"
  save_search "uob-logs-nifi" "UOB Logs - NiFi (CDF)" "log: *nifi* or log: *NiFi*"
  save_search "uob-logs-spark" "UOB Logs - Spark (CDE)" "log: *uob.product=spark* or log: *spark-operator* or log: *SparkApplication* or log: *dex-app*"
  save_search "uob-logs-flink" "UOB Logs - Flink (CSA)" "log: *uob.product=flink* or log: *org.apache.flink* or log: *ssb* or log: *flink-kubernetes-operator*"

  count=$(curl -sf "http://127.0.0.1:9200/uob-logs-$(date +%Y.%m.%d)/_count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "?")
  af=$(curl -sf "http://127.0.0.1:9200/uob-logs-$(date +%Y.%m.%d)/_count" -H 'Content-Type: application/json' -d '{"query":{"query_string":{"query":"log:airflow OR log:cwo01-cwo"}}}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "?")
  tr=$(curl -sf "http://127.0.0.1:9200/uob-logs-$(date +%Y.%m.%d)/_count" -H 'Content-Type: application/json' -d '{"query":{"query_string":{"query":"log:trino OR log:QueryMonitor OR log:cle01-trino"}}}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "?")
  echo "Kibana data view: ${TITLE} (${PATTERN}) id=${id}"
  echo "Documents today (ES): ${count} (airflow-ish: ${af}, trino-ish: ${tr})"
  echo "Discover: ${KIBANA}/app/discover — open saved search 'UOB Logs - Airflow' or 'UOB Logs - Trino'"
  echo "Use field log (not MESSAGE). Time: Last 24 hours."
}

main "$@"
