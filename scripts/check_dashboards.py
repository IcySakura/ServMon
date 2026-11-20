#!/usr/bin/env python3
"""Runs every PromQL query in the dashboards against Prometheus and reports
errors / empty results. Usage: python3 scripts/check_dashboards.py [range]"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROM = "http://127.0.0.1:9390"
RANGE = sys.argv[1] if len(sys.argv) > 1 else "10m"
SECS = {"m": 60, "h": 3600, "d": 86400}[RANGE[-1]] * int(RANGE[:-1])
SUBS = {"$__range_s": str(SECS), "$__range": RANGE, "$__interval": "5m", "$step": "15", "$min_idle": "60",
        "$top": "10", "$host": ".*", "$user": ".*", "$kind": ".*", "$cmd": ".*"}


def walk(panels):
    for p in panels:
        yield p
        yield from walk(p.get("panels", []))


bad = 0
for f in sorted((Path(__file__).resolve().parent.parent / "deploy/grafana/dashboards").glob("*.json")):
    d = json.loads(f.read_text())
    for p in walk(d["panels"]):
        for t in p.get("targets", []):
            q = t["expr"]
            for k in sorted(SUBS, key=len, reverse=True):
                q = q.replace(k, SUBS[k])
            url = PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": q})
            try:
                r = json.load(urllib.request.urlopen(url, timeout=120))
                n = len(r["data"]["result"])
                status = "ok   %3d" % n if n else "EMPTY   "
            except urllib.error.HTTPError as e:
                status = "ERROR " + json.load(e).get("error", "")[:150]
                bad += 1
            print("%-14s %-45s %s %s" % (f.stem, p["title"][:45], t["refId"], status))
sys.exit(1 if bad else 0)
