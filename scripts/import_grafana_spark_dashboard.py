#!/usr/bin/env python3
import json
import os
import urllib.request
import base64
from pathlib import Path

BASE = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
AUTH = base64.b64encode(os.environ.get("GRAFANA_AUTH", "admin:admin").encode()).decode()
DASH = Path(__file__).resolve().parent.parent / "backups" / "grafana-spark-dashboard.json"


def request(method, path, data=None):
    headers = {"Authorization": f"Basic {AUTH}"}
    body = json.dumps(data).encode() if data else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read() or b"{}")


def main():
    prom = next(d["uid"] for d in request("GET", "/api/datasources") if d["type"] == "prometheus")
    es = next(d["uid"] for d in request("GET", "/api/datasources") if d["type"] == "elasticsearch")
    folders = request("GET", "/api/folders")
    folder_uid = next((f["uid"] for f in folders if f["title"] == "Spark"), None)
    if not folder_uid:
        folder_uid = request("POST", "/api/folders", {"title": "Spark", "uid": "spark-obs"})["uid"]
    dash = json.loads(DASH.read_text().replace("__PROM_UID__", prom).replace("__ES_UID__", es))
    out = request("POST", "/api/dashboards/db", {"dashboard": dash, "folderUid": folder_uid, "overwrite": True})
    print("imported", out.get("url"))


if __name__ == "__main__":
    main()
