#!/usr/bin/env python3
"""Import or update Grafana Trino dashboard (Prometheus + Elasticsearch logs)."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import base64
from pathlib import Path

BASE = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
AUTH = base64.b64encode(
    os.environ.get("GRAFANA_AUTH", "admin:admin").encode()
).decode()
FOLDER_UID = os.environ.get("TRINO_FOLDER_UID", "trino-obs")
DASH_UID = os.environ.get("TRINO_DASHBOARD_UID", "trino-cle-obs")
REPO_DASH = Path(
    os.environ.get(
        "TRINO_DASHBOARD_JSON",
        Path(__file__).resolve().parent / "grafana-trino-dashboard.json",
    )
)
if not REPO_DASH.is_file():
    REPO_DASH = Path(__file__).resolve().parent.parent / "backups" / "grafana-trino-dashboard.json"


def request(method: str, path: str, data: dict | None = None) -> dict:
    headers = {"Authorization": f"Basic {AUTH}"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def datasource_uid(ds_type: str, name: str | None = None) -> str:
    for item in request("GET", "/api/datasources"):
        if item.get("type") != ds_type:
            continue
        if name is None or item.get("name") == name:
            return item["uid"]
    raise RuntimeError(f"No datasource type={ds_type} name={name}")


def ensure_folder() -> str:
    for folder in request("GET", "/api/folders"):
        if folder.get("title") == "Trino":
            return folder["uid"]
    created = request(
        "POST",
        "/api/folders",
        {"title": "Trino", "uid": FOLDER_UID},
    )
    return created["uid"]


def main() -> None:
    prom_uid = datasource_uid("prometheus")
    es_uid = datasource_uid("elasticsearch", "elasticsearch")
    folder_uid = ensure_folder()

    if not REPO_DASH.is_file():
        print(f"Missing dashboard file: {REPO_DASH}", file=sys.stderr)
        sys.exit(1)

    raw = REPO_DASH.read_text()
    raw = raw.replace("__PROM_UID__", prom_uid).replace("__ES_UID__", es_uid)
    dashboard = json.loads(raw)

    payload = {
        "dashboard": dashboard,
        "folderUid": folder_uid,
        "overwrite": True,
        "message": "import trino dashboard",
    }
    result = request("POST", "/api/dashboards/db", payload)
    print("imported", result.get("uid"), result.get("url"))


if __name__ == "__main__":
    main()
