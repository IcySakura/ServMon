#!/usr/bin/env python3
"""Generates the Grafana dashboards in deploy/grafana/dashboards/.

    python3 deploy/grafana/gen_dashboards.py

Edit this file rather than the JSON (or edit in the Grafana UI and export).
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "prometheus", "uid": "servmon-prom"}
JOB = "host,user,pid,kind,cmd,container"          # identity labels of a job series
HF = 'host=~"$host"'
HUF = 'host=~"$host",user=~"$user"'
GBH = "suffix: GiB·h"
H = "suffix: h"
GIB = 1 << 30


# ---------------------------------------------------------------- panel helpers
class Layout:
    def __init__(self):
        self.y, self.x, self.rowh = 0, 0, 0
        self.panels, self.id = [], 1

    def add(self, p, w, h):
        if self.x + w > 24:
            self.y += self.rowh
            self.x, self.rowh = 0, 0
        p["id"] = self.id
        self.id += 1
        p["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.rowh = max(self.rowh, h)
        self.panels.append(p)

    def row(self, title, collapsed=False):
        self.y += self.rowh
        self.x, self.rowh = 0, 0
        self.panels.append({"type": "row", "title": title, "id": self.id, "collapsed": collapsed,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.id += 1
        self.y += 1


def tgt(expr, legend="", ref="A", instant=False, table=False, interval=None):
    t = {"refId": ref, "expr": expr, "datasource": DS, "legendFormat": legend or "__auto"}
    if instant:
        t.update(instant=True, range=False)
    if table:
        t["format"] = "table"
    if interval:
        t["interval"] = interval
    return t


def timeseries(title, targets, unit="short", stack=False, desc="", fill=None, legend_calcs=("mean", "max"),
               min0=True, bars=False, interval=None, legend_right=True, decimals=None):
    custom = {"lineWidth": 2 if not stack else 1, "fillOpacity": fill if fill is not None else (60 if stack else 8),
              "showPoints": "never", "spanNulls": 60000, "gradientMode": "none",
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "axisSoftMin": 0 if min0 else None, "insertNulls": False}
    if bars:
        custom.update(drawStyle="bars", fillOpacity=80, lineWidth=1, barAlignment=-1)
    d = {"unit": unit, "custom": custom}
    if decimals is not None:
        d["decimals"] = decimals
    p = {"type": "timeseries", "title": title, "description": desc, "datasource": DS, "targets": targets,
         "fieldConfig": {"defaults": d, "overrides": []},
         "options": {"legend": {"displayMode": "table", "placement": "right" if legend_right else "bottom",
                                "calcs": list(legend_calcs), "showLegend": True,
                                "sortBy": "Mean" if "mean" in legend_calcs else None, "sortDesc": True},
                     "tooltip": {"mode": "multi", "sort": "desc"}}}
    if interval:
        p["interval"] = interval
    return p


def stat(title, expr, unit="short", desc="", thresholds=None, decimals=None, graph=True):
    d = {"unit": unit, "color": {"mode": "thresholds"},
         "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "text", "value": None}]}}
    if decimals is not None:
        d["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "datasource": DS,
            "targets": [tgt(expr, instant=not graph)],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
                        "colorMode": "value", "graphMode": "area" if graph else "none",
                        "textMode": "value", "justifyMode": "auto", "orientation": "auto"}}


def bargauge(title, expr, legend, unit="short", desc="", decimals=None, color="blue", maxv=None):
    d = {"unit": unit, "min": 0, "color": {"mode": "fixed", "fixedColor": color}}
    if decimals is not None:
        d["decimals"] = decimals
    if maxv is not None:
        d["max"] = maxv
    return {"type": "bargauge", "title": title, "description": desc, "datasource": DS,
            "targets": [tgt(expr, legend, instant=True)],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                        "valueMode": "text", "namePlacement": "left", "sizing": "manual",
                        "text": {"titleSize": 12, "valueSize": 14},
                        "minVizHeight": 16, "maxVizHeight": 24,
                        "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""}}}


def table(title, cols, sort=None, desc="", hide=(), link_user=True, label_order=None, filterable=True):
    """cols: list of (name, expr, unit, extra-field-props). Joined on their common labels."""
    targets, rename, overrides = [], {}, []
    for i, (name, expr, unit, extra) in enumerate(cols):
        ref = chr(ord("A") + i)
        targets.append(tgt(expr, ref=ref, instant=True, table=True))
        rename["Value #" + ref] = name
        props = [{"id": "unit", "value": unit}] + [{"id": k, "value": v} for k, v in (extra or {}).items()]
        overrides.append({"matcher": {"id": "byName", "options": name}, "properties": props})
    overrides.append({"matcher": {"id": "byName", "options": "cmd"},
                      "properties": [{"id": "custom.width", "value": 520}, {"id": "custom.inspect", "value": True}]})
    if link_user:
        overrides.append({"matcher": {"id": "byName", "options": "user"}, "properties": [{"id": "links", "value": [{
            "title": "Usage of ${__value.raw}",
            "url": "/d/servmon-accounting/usage-accounting?var-user=${__value.raw}&${host:queryparam}&${__url_time_range}"}]}]})
    order = {n: i for i, n in enumerate(list(label_order or ["host", "user", "kind", "pid", "gpu", "cmd"]) +
                                         [c[0] for c in cols])}
    excl = {"Time": True, "container": False}
    excl.update({h: True for h in hide})
    tr = [{"id": "merge", "options": {}},
          {"id": "organize", "options": {"excludeByName": excl, "renameByName": rename, "indexByName": order}}]
    if sort:
        tr.append({"id": "sortBy", "options": {"sort": [{"field": sort, "desc": True}]}})
    return {"type": "table", "title": title, "description": desc, "datasource": DS, "targets": targets,
            "transformations": tr,
            "fieldConfig": {"defaults": {"custom": {"filterable": filterable, "align": "auto",
                                                    "cellOptions": {"type": "auto"}}},
                            "overrides": overrides},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}}


def nolabel(expr):
    """Strip __name__ so instant-table queries merge on identical label sets."""
    return "sum without() (%s)" % expr


GAUGE_PCT = {"custom.cellOptions": {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"},
             "min": 0, "max": 100, "decimals": 0}


def var_query(name, q, label=None, multi=True, all_=True):
    return {"name": name, "label": label or name, "type": "query", "datasource": DS,
            "query": {"query": q, "refId": "v", "qryType": 1}, "definition": q, "refresh": 2, "sort": 1,
            "multi": multi, "includeAll": all_, "allValue": ".*" if all_ else None,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]} if all_ else {}}


def var_custom(name, label, options, default):
    q = ",".join("%s : %s" % (t, v) for t, v in options)
    opts = [{"text": t, "value": str(v), "selected": str(v) == str(default)} for t, v in options]
    cur = next(o for o in opts if o["selected"])
    return {"name": name, "label": label, "type": "custom", "query": q, "options": opts,
            "current": {"text": cur["text"], "value": cur["value"]}, "multi": False, "includeAll": False}


V_HOST = var_query("host", "label_values(servmon_up, host)")
V_USER = var_query("user", 'label_values(servmon_user_cpu_cores{host=~"$host"}, user)')
V_KIND = var_query("kind", 'label_values(servmon_job_cpu_cores{host=~"$host"}, kind)', label="workload")
V_STEP = {"name": "step", "type": "constant", "query": "15", "hide": 2,
          "description": "collector interval in seconds (must match config.toml)"}
V_IDLE = var_custom("min_idle", "idle for at least",
                    [("1m", 60), ("5m", 300), ("10m", 600), ("30m", 1800), ("1h", 3600), ("6h", 21600), ("1d", 86400)],
                    600)


def dashboard(uid, title, layout, variables, time_from="now-6h", refresh="15s", desc=""):
    return {"uid": uid, "title": title, "description": desc, "tags": ["servmon"], "timezone": "browser",
            "editable": True, "graphTooltip": 1, "schemaVersion": 39, "version": 1,
            "time": {"from": time_from, "to": "now"}, "refresh": refresh,
            "timepicker": {"refresh_intervals": ["5s", "15s", "30s", "1m", "5m", "15m", "1h"]},
            "templating": {"list": variables}, "annotations": {"list": []},
            "links": [{"type": "dashboards", "tags": ["servmon"], "asDropdown": False, "includeVars": True,
                       "keepTime": True, "title": "servmon"}],
            "panels": layout.panels}


def integral(metric, sel, by, scale="", fn="sum_over_time"):
    """Area under a gauge over the dashboard range, in <unit>-hours."""
    return "sum by(%s) (%s(%s{%s}[$__range])) * $step / 3600%s" % (by, fn, metric, sel, scale)


# ================================================================ 1. Overview
def overview():
    L = Layout()
    L.row("Fleet now")
    L.add(table("Servers", [
        ("up", nolabel('servmon_up{%s}' % HF), "short", {"custom.cellOptions": {"type": "color-text"},
            "mappings": [{"type": "value", "options": {"0": {"text": "DOWN", "color": "red"},
                                                        "1": {"text": "up", "color": "green"}}}]}),
        ("CPU busy", "100 * servmon_host_cpu_busy_ratio{%s}" % HF, "percent", GAUGE_PCT),
        ("load1", nolabel("servmon_host_load1{%s}" % HF), "short", {"decimals": 1}),
        ("cores", nolabel("servmon_host_ncpu{%s}" % HF), "short", {}),
        ("RAM used", "100 * servmon_host_mem_used_bytes{%s} / servmon_host_mem_total_bytes" % HF, "percent", GAUGE_PCT),
        ("RAM total", nolabel("servmon_host_mem_total_bytes{%s}" % HF), "bytes", {}),
        ("GPU util (avg)", "avg by(host) (servmon_gpu_util_percent{%s})" % HF, "percent", GAUGE_PCT),
        ("VRAM used", "100 * sum by(host) (servmon_gpu_mem_used_bytes{%s}) / sum by(host) (servmon_gpu_mem_total_bytes)" % HF, "percent", GAUGE_PCT),
        ("GPUs", "count by(host) (servmon_gpu_util_percent{%s})" % HF, "short", {}),
        ("active users", "count by(host) (servmon_user_cpu_cores{%s} > 0.1 or sum by(host,user) (servmon_user_gpu_mem_bytes{%s}) > 0)" % (HF, HF), "short", {}),
        ("logged-in users", "count by(host) (servmon_user_sessions{%s} > 0)" % HF, "short", {}),
        ("idle-held VRAM", "sum by(host) (servmon_user_idle_gpu_mem_bytes{%s})" % HF, "bytes", {}),
        ("idle-held RAM", "sum by(host) (servmon_user_idle_mem_bytes{%s})" % HF, "bytes", {}),
        ("GPU status", "sum by(host) (servmon_gpu_available{%s})" % HF, "short", {"custom.cellOptions": {"type": "color-text"},
            "mappings": [{"type": "value", "options": {"0": {"text": "nvidia-smi broken", "color": "orange"},
                                                        "1": {"text": "ok", "color": "green"}}}]}),
    ], link_user=False, desc="Current state of each server. idle-held = memory held by jobs that have been idle "
                              "longer than the collector's idle_after setting."), 24, 8)

    L.row("Load per server")
    L.add(timeseries("CPU busy", [tgt("100 * servmon_host_cpu_busy_ratio{%s}" % HF, "{{host}}")], "percent",
                     legend_calcs=("mean", "max", "lastNotNull")), 12, 8)
    L.add(timeseries("RAM used (excluding cache)", [tgt("100 * servmon_host_mem_used_bytes{%s} / servmon_host_mem_total_bytes" % HF, "{{host}}")],
                     "percent", legend_calcs=("mean", "max", "lastNotNull")), 12, 8)
    L.add(timeseries("GPU utilisation", [tgt("servmon_gpu_util_percent{%s}" % HF, "{{host}} gpu{{gpu}}")], "percent",
                     legend_calcs=("mean", "max", "lastNotNull")), 12, 8)
    L.add(timeseries("VRAM used", [tgt("100 * servmon_gpu_mem_used_bytes{%s} / servmon_gpu_mem_total_bytes" % HF, "{{host}} gpu{{gpu}}")],
                     "percent", legend_calcs=("mean", "max", "lastNotNull")), 12, 8)

    L.row("Who is using it (selected servers, stacked by user)")
    L.add(timeseries("CPU cores by user", [tgt("sum by(user) (servmon_user_cpu_cores{%s}) > 0.05" % HUF, "{{user}}")],
                     "short", stack=True, decimals=1), 12, 9)
    L.add(timeseries("RAM (private) by user", [tgt("sum by(user) (servmon_user_mem_bytes{%s}) > 256*1024*1024" % HUF, "{{user}}")],
                     "bytes", stack=True, desc="Anonymous RSS - memory the processes actually own (excludes page cache and shared libs)."), 12, 9)
    L.add(timeseries("GPU compute by user (GPU-equivalents)", [tgt("sum by(user) (servmon_user_gpu_sm_percent{%s}) / 100 > 0" % HUF, "{{user}}")],
                     "short", stack=True, decimals=2, desc="Sum of per-process SM utilisation / 100. 1.0 = one GPU fully busy."), 12, 9)
    L.add(timeseries("VRAM held by user", [tgt("sum by(user) (servmon_user_gpu_mem_bytes{%s}) > 0" % HUF, "{{user}}")],
                     "bytes", stack=True), 12, 9)

    L.row("GPUs right now")
    L.add(table("GPU processes", [
        ("VRAM", nolabel("servmon_job_gpu_mem_bytes{%s}" % HUF), "bytes", {"custom.cellOptions": {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}, "max": 96 * GIB}),
        ("SM %", nolabel("servmon_job_gpu_sm_percent{%s}" % HUF), "percent", GAUGE_PCT),
        ("GPU-idle for", "(servmon_job_gpu_mem_bytes{%s} * 0) + on(%s) group_left servmon_job_gpu_idle_seconds" % (HUF, JOB), "s",
         {"custom.cellOptions": {"type": "color-text"}, "thresholds": {"mode": "absolute", "steps": [
             {"color": "text", "value": None}, {"color": "orange", "value": 600}, {"color": "red", "value": 3600}]}}),
        ("CPU cores", "(servmon_job_gpu_mem_bytes{%s} * 0) + on(%s) group_left servmon_job_cpu_cores" % (HUF, JOB), "short", {"decimals": 2}),
    ], sort="VRAM", desc="Every process tree holding GPU memory, one row per GPU. GPU-idle for = how long it has held "
                         "VRAM with <1% SM."), 24, 9)
    L.add(bargauge("VRAM per GPU by user (now)",
                   "sort_desc(sum by(host,gpu,user) (servmon_user_gpu_mem_bytes{%s}) > 0)" % HUF,
                   "{{host}} gpu{{gpu}} · {{user}}", "bytes"), 12, 9)
    L.add(bargauge("SM utilisation per GPU by user (now)",
                   "sort_desc(sum by(host,gpu,user) (servmon_user_gpu_sm_percent{%s}) > 0)" % HUF,
                   "{{host}} gpu{{gpu}} · {{user}}", "percent", maxv=100, color="green"), 12, 9)

    L.row("Top jobs right now")
    L.add(table("Jobs (process trees) by resource", [
        ("CPU cores", nolabel("servmon_job_cpu_cores{%s,kind=~\"$kind\"}" % HUF), "short", {"decimals": 2}),
        ("RAM", nolabel("servmon_job_mem_bytes{%s,kind=~\"$kind\"}" % HUF), "bytes", {}),
        ("VRAM", "sum without(gpu) (servmon_job_gpu_mem_bytes{%s,kind=~\"$kind\"})" % HUF, "bytes", {}),
        ("SM %", "sum without(gpu) (servmon_job_gpu_sm_percent{%s,kind=~\"$kind\"})" % HUF, "percent", {"decimals": 0}),
        ("procs", nolabel("servmon_job_procs{%s,kind=~\"$kind\"}" % HUF), "short", {}),
        ("running for", "time() - servmon_job_start_time_seconds{%s,kind=~\"$kind\"}" % HUF, "s", {}),
        ("GPU-idle for", nolabel("servmon_job_gpu_idle_seconds{%s,kind=~\"$kind\"} > 0" % HUF), "s", {}),
        ("fully idle for", nolabel("servmon_job_cpu_idle_seconds{%s,kind=~\"$kind\"} > 0" % HUF), "s", {}),
    ], sort="RAM", desc="A job = a process plus all its descendants of the same user, rooted at the first process "
                        "below a shell/tmux/IDE. Only jobs using >=0.1 core, >=512MiB RAM or any VRAM are listed."), 24, 14)
    return dashboard("servmon-overview", "Servers · Overview (live)", L, [V_HOST, V_USER, V_KIND],
                     desc="Live state of all shared servers")


# ================================================================ 2. Usage accounting
def accounting():
    L = Layout()
    L.row("Totals for the selected period")
    L.add(stat("CPU core-hours", "sum(sum_over_time(servmon_user_cpu_cores{%s}[$__range])) * $step / 3600" % HUF, H, graph=False), 4, 4)
    L.add(stat("GPU compute-hours", "sum(sum_over_time(servmon_user_gpu_sm_percent{%s}[$__range])) / 100 * $step / 3600" % HUF, H, graph=False,
               desc="SM-utilisation-weighted: 1 h of one GPU at 100% = 1 GPU-hour."), 4, 4)
    L.add(stat("VRAM held", "sum(sum_over_time(servmon_user_gpu_mem_bytes{%s}[$__range])) * $step / 3600 / 2^30" % HUF, GBH, graph=False), 4, 4)
    L.add(stat("…of which idle", "sum(sum_over_time(servmon_user_idle_gpu_mem_bytes{%s}[$__range])) * $step / 3600 / 2^30" % HUF, GBH, graph=False,
               thresholds=[{"color": "text", "value": None}, {"color": "orange", "value": 100}]), 4, 4)
    L.add(stat("RAM held", "sum(sum_over_time(servmon_user_mem_bytes{%s}[$__range])) * $step / 3600 / 2^30" % HUF, GBH, graph=False), 4, 4)
    L.add(stat("…of which idle", "sum(sum_over_time(servmon_user_idle_mem_bytes{%s}[$__range])) * $step / 3600 / 2^30" % HUF, GBH, graph=False,
               thresholds=[{"color": "text", "value": None}, {"color": "orange", "value": 100}]), 4, 4)

    def user_cols(by):
        s = HUF
        return [
            ("CPU core-h", integral("servmon_user_cpu_cores", s, by), H, {"decimals": 1}),
            ("avg cores", "sum by(%s) (sum_over_time(servmon_user_cpu_cores{%s}[$__range])) * $step / ($__range_s)" % (by, s), "short", {"decimals": 2}),
            ("peak cores", "max by(%s) (max_over_time(servmon_user_cpu_cores{%s}[$__range]))" % (by, s), "short", {"decimals": 1}),
            ("RAM GiB-h", integral("servmon_user_mem_bytes", s, by, " / 2^30"), GBH, {"decimals": 1}),
            ("peak RAM", "max by(%s) (max_over_time(servmon_user_mem_bytes{%s}[$__range]))" % (by, s), "bytes", {}),
            ("GPU compute-h", integral("servmon_user_gpu_sm_percent", s, by, " / 100"), H, {"decimals": 2}),
            ("VRAM GiB-h", integral("servmon_user_gpu_mem_bytes", s, by, " / 2^30"), GBH, {"decimals": 1}),
            ("peak VRAM", "max by(%s) (max_over_time(sum by(%s) (servmon_user_gpu_mem_bytes{%s})[$__range:]))" % (by, by, s), "bytes", {}),
            ("idle VRAM GiB-h", integral("servmon_user_idle_gpu_mem_bytes", s, by, " / 2^30"), GBH, {"decimals": 1}),
            ("idle VRAM %", "100 * (%s) / (%s > 0)" % (integral("servmon_user_idle_gpu_mem_bytes", s, by),
                                                      integral("servmon_user_gpu_mem_bytes", s, by)), "percent",
             {"decimals": 0, "custom.cellOptions": {"type": "color-text"}, "thresholds": {"mode": "absolute", "steps": [
                 {"color": "text", "value": None}, {"color": "orange", "value": 25}, {"color": "red", "value": 50}]}}),
            ("idle RAM GiB-h", integral("servmon_user_idle_mem_bytes", s, by, " / 2^30"), GBH, {"decimals": 1}),
        ]

    L.row("Per user")
    L.add(table("Usage by user (all selected servers)", user_cols("user"), sort="GPU compute-h",
                desc="Integrals over the time range. GiB-h = GiB held × hours. idle VRAM % = share of the user's "
                     "VRAM-hours spent with the holding job GPU-idle."), 24, 10)
    L.add(table("Usage by server × user", user_cols("host,user"), sort="GPU compute-h"), 24, 10)

    L.add(bargauge("GPU compute-hours by user", "sort_desc(%s > 0)" % integral("servmon_user_gpu_sm_percent", HUF, "user", " / 100"),
                   "{{user}}", H, decimals=1, color="green"), 8, 10)
    L.add(bargauge("VRAM GiB-hours by user", "sort_desc(%s > 0)" % integral("servmon_user_gpu_mem_bytes", HUF, "user", " / 2^30"),
                   "{{user}}", GBH, decimals=1, color="purple"), 8, 10)
    L.add(bargauge("CPU core-hours by user", "sort_desc(%s > 0.1)" % integral("servmon_user_cpu_cores", HUF, "user"),
                   "{{user}}", H, decimals=1, color="blue"), 8, 10)

    L.row("Over time (bucketed; bucket size follows the zoom level, min 1h)")
    L.add(timeseries("GPU compute-hours per bucket", [tgt("sum by(user) (sum_over_time(servmon_user_gpu_sm_percent{%s}[$__interval])) / 100 * $step / 3600 > 0" % HUF, "{{user}}")],
                     H, stack=True, bars=True, interval="1h", legend_calcs=("sum",), decimals=1), 12, 9)
    L.add(timeseries("CPU core-hours per bucket", [tgt("sum by(user) (sum_over_time(servmon_user_cpu_cores{%s}[$__interval])) * $step / 3600 > 0.05" % HUF, "{{user}}")],
                     H, stack=True, bars=True, interval="1h", legend_calcs=("sum",), decimals=1), 12, 9)
    L.add(timeseries("VRAM GiB-hours per bucket", [tgt("sum by(user) (sum_over_time(servmon_user_gpu_mem_bytes{%s}[$__interval])) * $step / 3600 / 2^30 > 0" % HUF, "{{user}}")],
                     GBH, stack=True, bars=True, interval="1h", legend_calcs=("sum",), decimals=0), 12, 9)
    L.add(timeseries("Idle VRAM GiB-hours per bucket", [tgt("sum by(user) (sum_over_time(servmon_user_idle_gpu_mem_bytes{%s}[$__interval])) * $step / 3600 / 2^30 > 0" % HUF, "{{user}}")],
                     GBH, stack=True, bars=True, interval="1h", legend_calcs=("sum",), decimals=0), 12, 9)

    L.row("By workload type")
    J = '%s,kind=~"$kind"' % HUF
    L.add(bargauge("GPU compute-hours by workload", "sort_desc(%s > 0)" % integral("servmon_job_gpu_sm_percent", J, "kind", " / 100"),
                   "{{kind}}", H, decimals=1, color="green"), 8, 9)
    L.add(bargauge("VRAM GiB-hours by workload", "sort_desc(%s > 0)" % integral("servmon_job_gpu_mem_bytes", J, "kind", " / 2^30"),
                   "{{kind}}", GBH, decimals=1, color="purple"), 8, 9)
    L.add(bargauge("CPU core-hours by workload", "sort_desc(%s > 0.1)" % integral("servmon_job_cpu_cores", J, "kind"),
                   "{{kind}}", H, decimals=1, color="blue"), 8, 9)
    L.add(table("Workload × user", [
        ("CPU core-h", integral("servmon_job_cpu_cores", J, "kind,user"), H, {"decimals": 1}),
        ("RAM GiB-h", integral("servmon_job_mem_bytes", J, "kind,user", " / 2^30"), GBH, {"decimals": 1}),
        ("GPU compute-h", integral("servmon_job_gpu_sm_percent", J, "kind,user", " / 100"), H, {"decimals": 2}),
        ("VRAM GiB-h", integral("servmon_job_gpu_mem_bytes", J, "kind,user", " / 2^30"), GBH, {"decimals": 1}),
        ("idle VRAM GiB-h", integral("servmon_job_idle_gpu_mem_bytes", J, "kind,user", " / 2^30"), GBH, {"decimals": 1}),
    ], sort="GPU compute-h", label_order=["kind", "user"],
        desc="Only jobs above the export threshold are counted here, so totals are slightly below the per-user numbers."), 24, 9)

    L.row("Top jobs / programs in the period")
    by = JOB

    def top(key_expr):
        return lambda e: "(%s) and on(%s) topk(40, %s)" % (e, by, key_expr)

    gpu_key = integral("servmon_job_gpu_sm_percent", J, by, " / 100")
    vram_key = integral("servmon_job_gpu_mem_bytes", J, by, " / 2^30")
    cpu_key = integral("servmon_job_cpu_cores", J, by)

    def job_cols(f):
        return [
            ("GPU compute-h", f(integral("servmon_job_gpu_sm_percent", J, by, " / 100")), H, {"decimals": 2}),
            ("VRAM GiB-h", f(integral("servmon_job_gpu_mem_bytes", J, by, " / 2^30")), GBH, {"decimals": 1}),
            ("idle VRAM GiB-h", f(integral("servmon_job_idle_gpu_mem_bytes", J, by, " / 2^30")), GBH, {"decimals": 1}),
            ("CPU core-h", f(integral("servmon_job_cpu_cores", J, by)), H, {"decimals": 1}),
            ("peak RAM", f("max by(%s) (max_over_time(servmon_job_mem_bytes{%s}[$__range]))" % (by, J)), "bytes", {}),
            ("seen for", f("count by(%s) (count_over_time(servmon_job_cpu_cores{%s}[$__range])) * $step" % (by, J)), "s", {}),
        ]
    L.add(table("Top 40 jobs by GPU compute", job_cols(top(gpu_key)), sort="GPU compute-h"), 24, 10)
    L.add(table("Top 40 jobs by VRAM held", job_cols(top(vram_key)), sort="VRAM GiB-h"), 24, 10)
    L.add(table("Top 40 jobs by CPU", job_cols(top(cpu_key)), sort="CPU core-h"), 24, 10)
    return dashboard("servmon-accounting", "Servers · Usage accounting", L, [V_HOST, V_USER, V_KIND, V_STEP],
                     time_from="now-7d", refresh="", desc="Who used how much, over any time range")


# ================================================================ 3. Idle & waste
def idle():
    L = Layout()
    L.row("Right now")
    L.add(stat("VRAM held by GPU-idle jobs", "sum(servmon_user_idle_gpu_mem_bytes{%s})" % HUF, "bytes",
               thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 4 * GIB}, {"color": "red", "value": 24 * GIB}]), 6, 5)
    L.add(stat("RAM held by fully idle jobs", "sum(servmon_user_idle_mem_bytes{%s})" % HUF, "bytes",
               thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 16 * GIB}, {"color": "red", "value": 64 * GIB}]), 6, 5)
    L.add(stat("GPUs with VRAM held but <5% util",
               "count((servmon_gpu_util_percent{%s} < 5) and on(host,gpu) (servmon_gpu_mem_used_bytes > 2*2^30)) or vector(0)" % HF, "short",
               thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]), 6, 5)
    L.add(stat("/dev/shm files", "sum(servmon_user_shm_bytes{%s})" % HUF, "bytes",
               desc="RAM held by files in /dev/shm - survives the process that created it."), 6, 5)

    L.add(table("Jobs holding VRAM without using the GPU", [
        ("VRAM", "sum without(gpu) (servmon_job_gpu_mem_bytes{%s}) and on(%s) (servmon_job_gpu_idle_seconds >= $min_idle)" % (HUF, JOB), "bytes", {}),
        ("GPU-idle for", nolabel("servmon_job_gpu_idle_seconds{%s} >= $min_idle" % HUF), "s", {}),
        ("CPU cores", "servmon_job_cpu_cores{%s} + 0 and on(%s) (servmon_job_gpu_idle_seconds >= $min_idle)" % (HUF, JOB), "short", {"decimals": 2}),
        ("RAM", "servmon_job_mem_bytes{%s} + 0 and on(%s) (servmon_job_gpu_idle_seconds >= $min_idle)" % (HUF, JOB), "bytes", {}),
        ("running for", "time() - servmon_job_start_time_seconds{%s} and on(%s) (servmon_job_gpu_idle_seconds >= $min_idle)" % (HUF, JOB), "s", {}),
    ], sort="VRAM", desc="SM utilisation <1% continuously for at least the selected duration. The idle clock restarts "
                         "if the collector reconnects."), 24, 9)
    L.add(table("Jobs holding RAM while doing nothing (no CPU, no GPU)", [
        ("RAM", "servmon_job_mem_bytes{%s} + 0 and on(%s) (servmon_job_cpu_idle_seconds >= $min_idle)" % (HUF, JOB), "bytes", {}),
        ("idle for", nolabel("servmon_job_cpu_idle_seconds{%s} >= $min_idle" % HUF), "s", {}),
        ("swap", "servmon_job_swap_bytes{%s} + 0 and on(%s) (servmon_job_cpu_idle_seconds >= $min_idle)" % (HUF, JOB), "bytes", {}),
        ("procs", "servmon_job_procs{%s} + 0 and on(%s) (servmon_job_cpu_idle_seconds >= $min_idle)" % (HUF, JOB), "short", {}),
        ("running for", "time() - servmon_job_start_time_seconds{%s} and on(%s) (servmon_job_cpu_idle_seconds >= $min_idle)" % (HUF, JOB), "s", {}),
    ], sort="RAM", desc=">=1 GiB private RAM and <0.02 cores, continuously for at least the selected duration."), 24, 9)
    L.add(bargauge("/dev/shm by owner", "sort_desc(sum by(host,user) (servmon_user_shm_bytes{%s}) > 64*2^20)" % HUF,
                   "{{host}} · {{user}}", "bytes", color="orange"), 12, 7)
    L.add(table("GPUs: util vs memory", [
        ("util", nolabel("servmon_gpu_util_percent{%s}" % HF), "percent", GAUGE_PCT),
        ("VRAM used", nolabel("servmon_gpu_mem_used_bytes{%s}" % HF), "bytes", {}),
        ("VRAM idle-held", "sum by(host,gpu) (servmon_job_gpu_mem_bytes{%s} and on(%s) (servmon_job_idle_gpu_mem_bytes > 0))" % (HF, JOB), "bytes", {}),
        ("power", nolabel("servmon_gpu_power_watts{%s}" % HF), "watt", {"decimals": 0}),
    ], link_user=False, label_order=["host", "gpu", "name"]), 12, 7)

    L.row("Over time")
    L.add(timeseries("Idle-held VRAM by user", [tgt("sum by(user) (servmon_user_idle_gpu_mem_bytes{%s}) > 0" % HUF, "{{user}}")],
                     "bytes", stack=True), 12, 9)
    L.add(timeseries("Idle-held RAM by user", [tgt("sum by(user) (servmon_user_idle_mem_bytes{%s}) > 0" % HUF, "{{user}}")],
                     "bytes", stack=True), 12, 9)
    L.add(timeseries("Idle-held VRAM per GPU", [tgt("sum by(host,gpu) (servmon_job_gpu_mem_bytes{%s} and on(%s) (servmon_job_idle_gpu_mem_bytes > 0))" % (HF, JOB), "{{host}} gpu{{gpu}}")],
                     "bytes", stack=True), 12, 9)
    L.add(timeseries("/dev/shm by user", [tgt("sum by(user) (servmon_user_shm_bytes{%s}) > 64*2^20" % HUF, "{{user}}")],
                     "bytes", stack=True), 12, 9)

    L.row("Period totals")
    L.add(bargauge("Idle VRAM GiB-hours by user", "sort_desc(%s > 0)" % integral("servmon_user_idle_gpu_mem_bytes", HUF, "user", " / 2^30"),
                   "{{user}}", GBH, decimals=1, color="orange"), 8, 9)
    L.add(bargauge("Idle RAM GiB-hours by user", "sort_desc(%s > 0)" % integral("servmon_user_idle_mem_bytes", HUF, "user", " / 2^30"),
                   "{{user}}", GBH, decimals=1, color="orange"), 8, 9)
    L.add(bargauge("Idle VRAM % of VRAM held, by user",
                   "sort_desc(100 * (%s) / (%s > 2^30) > 0)" % (integral("servmon_user_idle_gpu_mem_bytes", HUF, "user"),
                                                               integral("servmon_user_gpu_mem_bytes", HUF, "user")),
                   "{{user}}", "percent", decimals=0, color="red", maxv=100), 8, 9)
    L.add(table("Worst idle jobs in the period", [
        ("idle VRAM GiB-h", integral("servmon_job_idle_gpu_mem_bytes", HUF, JOB, " / 2^30") + " > 0", GBH, {"decimals": 1}),
        ("idle RAM GiB-h", integral("servmon_job_idle_mem_bytes", HUF, JOB, " / 2^30") + " > 0", GBH, {"decimals": 1}),
        ("longest GPU-idle", "max by(%s) (max_over_time(servmon_job_gpu_idle_seconds{%s}[$__range])) > 0 and on(%s) (%s > 0)" % (
            JOB, HUF, JOB, integral("servmon_job_idle_gpu_mem_bytes", HUF, JOB)), "s", {}),
        ("longest fully idle", "max by(%s) (max_over_time(servmon_job_cpu_idle_seconds{%s}[$__range])) > 0 and on(%s) (%s > 0)" % (
            JOB, HUF, JOB, integral("servmon_job_idle_mem_bytes", HUF, JOB)), "s", {}),
    ], sort="idle VRAM GiB-h"), 24, 10)
    return dashboard("servmon-idle", "Servers · Idle & waste", L, [V_HOST, V_USER, V_IDLE, V_STEP],
                     time_from="now-24h", refresh="30s", desc="Who holds RAM/VRAM without using it")


# ================================================================ 4. Job explorer
def explorer():
    L = Layout()
    sel = '%s,kind=~"$kind",cmd=~"$cmd"' % HUF
    leg = "{{host}} {{user}} {{pid}} {{cmd}}"
    L.add(timeseries("CPU cores per job", [tgt("topk($top, servmon_job_cpu_cores{%s})" % sel, leg)], "short",
                     legend_right=False, decimals=2), 12, 15)
    L.add(timeseries("RAM per job", [tgt("topk($top, servmon_job_mem_bytes{%s})" % sel, leg)], "bytes", legend_right=False), 12, 15)
    L.add(timeseries("VRAM per job", [tgt("topk($top, sum without(gpu) (servmon_job_gpu_mem_bytes{%s}))" % sel, leg)], "bytes",
                     legend_right=False), 12, 15)
    L.add(timeseries("SM % per job", [tgt("topk($top, sum without(gpu) (servmon_job_gpu_sm_percent{%s}))" % sel, leg)], "percent",
                     legend_right=False), 12, 15)
    L.add(table("Matching jobs in the period", [
        ("CPU core-h", integral("servmon_job_cpu_cores", sel, JOB), H, {"decimals": 2}),
        ("peak RAM", "max by(%s) (max_over_time(servmon_job_mem_bytes{%s}[$__range]))" % (JOB, sel), "bytes", {}),
        ("GPU compute-h", integral("servmon_job_gpu_sm_percent", sel, JOB, " / 100"), H, {"decimals": 2}),
        ("peak VRAM", "max by(%s) (max_over_time(sum without(gpu) (servmon_job_gpu_mem_bytes{%s})[$__range:]))" % (JOB, sel), "bytes", {}),
        ("idle VRAM GiB-h", integral("servmon_job_idle_gpu_mem_bytes", sel, JOB, " / 2^30"), GBH, {"decimals": 1}),
        ("first seen", "min by(%s) (min_over_time(timestamp(servmon_job_cpu_cores{%s})[$__range:])) * 1000" % (JOB, sel), "dateTimeAsLocal", {}),
        ("last seen", "max by(%s) (max_over_time(timestamp(servmon_job_cpu_cores{%s})[$__range:])) * 1000" % (JOB, sel), "dateTimeAsLocal", {}),
    ], sort="CPU core-h"), 24, 16)
    V_CMD = {"name": "cmd", "label": "command regex", "type": "textbox", "query": ".*",
             "current": {"text": ".*", "value": ".*"}}
    V_TOP = var_custom("top", "top N", [("5", 5), ("10", 10), ("20", 20), ("50", 50)], 10)
    return dashboard("servmon-jobs", "Servers · Job explorer", L, [V_HOST, V_USER, V_KIND, V_CMD, V_TOP, V_STEP],
                     time_from="now-24h", refresh="30s", desc="Follow individual jobs / programs over time")


# ================================================================ 5. Server detail
def server():
    L = Layout()
    S = 'host=~"$host"'
    L.add(timeseries("CPU", [tgt("100 * servmon_host_cpu_busy_ratio{%s}" % S, "{{host}} busy"),
                             tgt("100 * servmon_host_cpu_iowait_ratio{%s}" % S, "{{host}} iowait", "B")], "percent"), 12, 8)
    L.add(timeseries("Load average (per core)", [tgt("servmon_host_load1{%s} / servmon_host_ncpu" % S, "{{host}}")], "percentunit"), 12, 8)
    L.add(timeseries("Memory", [tgt("servmon_host_mem_used_bytes{%s}" % S, "{{host}} used"),
                                tgt("servmon_host_mem_shmem_bytes{%s}" % S, "{{host}} of which shmem/tmpfs", "B"),
                                tgt("servmon_host_swap_used_bytes{%s}" % S, "{{host}} swap used", "C"),
                                tgt("servmon_host_mem_total_bytes{%s}" % S, "{{host}} total", "D")], "bytes"), 12, 8)
    L.add(timeseries("Disk used", [tgt("100 * servmon_disk_used_bytes{%s} / servmon_disk_total_bytes" % S, "{{host}} {{mount}}")],
                     "percent", legend_calcs=("lastNotNull",)), 12, 8)
    L.add(timeseries("GPU utilisation", [tgt("servmon_gpu_util_percent{%s}" % S, "{{host}} gpu{{gpu}}")], "percent"), 8, 8)
    L.add(timeseries("GPU memory-controller utilisation", [tgt("servmon_gpu_mem_util_percent{%s}" % S, "{{host}} gpu{{gpu}}")], "percent"), 8, 8)
    L.add(timeseries("VRAM used", [tgt("servmon_gpu_mem_used_bytes{%s}" % S, "{{host}} gpu{{gpu}}")], "bytes"), 8, 8)
    L.add(timeseries("GPU power", [tgt("servmon_gpu_power_watts{%s}" % S, "{{host}} gpu{{gpu}}")], "watt"), 8, 8)
    L.add(timeseries("GPU temperature", [tgt("servmon_gpu_temp_celsius{%s}" % S, "{{host}} gpu{{gpu}}")], "celsius", min0=False), 8, 8)
    L.add(timeseries("Logged-in sessions by user", [tgt("sum by(user) (servmon_user_sessions{%s}) > 0" % S, "{{user}}")], "short",
                     stack=True), 8, 8)
    L.add(timeseries("Collector: snapshot age / scan time", [tgt("servmon_snapshot_age_seconds{%s}" % S, "{{host}} age"),
                                                             tgt("servmon_scan_seconds{%s}" % S, "{{host}} scan", "B")], "s"), 12, 7)
    L.add(timeseries("Processes", [tgt("servmon_host_procs{%s}" % S, "{{host}}")], "short"), 12, 7)
    return dashboard("servmon-server", "Servers · Server detail", L, [var_query("host", "label_values(servmon_up, host)")],
                     desc="Hardware-level metrics per server")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for i, fn in enumerate([overview, accounting, idle, explorer, server], 1):
        d = fn()
        path = OUT / ("%02d-%s.json" % (i, d["uid"].split("-", 1)[1]))
        path.write_text(json.dumps(d, indent=1, ensure_ascii=False))
        print("wrote", path)
