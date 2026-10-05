#!/usr/bin/env python3
"""Ensure Grafana Elasticsearch datasource maps log body field for Logs panels."""
from __future__ import annotations

import base64
import json
import os
import urllib.request

BASE = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
AUTH = base64.b64encode(os.environ.get("GRAFANA_AUTH", "admin:admin").encode()).decode()
DS_NAME = os.environ.get("GRAFANA_ES_DATASOURCE", "elasticsearch")
LOG_FIELD = os.environ.get("GRAFANA_ES_LOG_FIELD", "log")
TIME_FIELD = os.environ.get("GRAFANA_ES_TIME_FIELD", "@timestamp")


def request(method: str, path: str, data: dict | None = None) -> dict:
    headers = {"Authorization": f"Basic {AUTH}"}
    body = json.dumps(data).encode() if data is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def main() -> None:
    ds_list = request("GET", "/api/datasources")
    ds = next((d for d in ds_list if d.get("name") == DS_NAME and d.get("type") == "elasticsearch"), None)
    if not ds:
        ds = next((d for d in ds_list if d.get("type") == "elasticsearch"), None)
    if not ds:
        raise SystemExit("No Elasticsearch datasource in Grafana")

    json_data = dict(ds.get("jsonData") or {})
    json_data["timeField"] = TIME_FIELD
    json_data["logMessageField"] = LOG_FIELD
    json_data.setdefault("maxConcurrentShardRequests", 5)
    json_data.setdefault("tlsSkipVerify", True)

    payload = {
        "id": ds["id"],
        "uid": ds["uid"],
        "orgId": ds.get("orgId", 1),
        "name": ds["name"],
        "type": "elasticsearch",
        "access": ds.get("access", "proxy"),
        "url": ds.get("url", "http://localhost:9200"),
        "isDefault": ds.get("isDefault", False),
        "jsonData": json_data,
    }
    if ds.get("basicAuth"):
        payload["basicAuth"] = True
        payload["basicAuthUser"] = ds.get("basicAuthUser", "")

    request("PUT", f"/api/datasources/{ds['id']}", payload)
    print(f"Patched datasource {ds['name']} logMessageField={LOG_FIELD}")


if __name__ == "__main__":
    main()
