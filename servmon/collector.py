#!/usr/bin/env python3
"""servmon collector.

Keeps one long-lived SSH session per server, each running servmon/agent.py
(streamed over stdin, nothing installed remotely). The latest snapshot of every
server is exposed in Prometheus text format on /metrics; raw snapshots are also
archived as JSONL. Stdlib only (Python >= 3.11 for tomllib).

    python3 servmon/collector.py [-c config.toml]
"""
import argparse
import gzip
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = Path(__file__).resolve().parent / "agent.py"
log = logging.getLogger("servmon")
MiB = 1 << 20


# ---------------------------------------------------------------- per-host stream
class HostStream(threading.Thread):
    def __init__(self, name, ssh, python, agent_args, interval, archive):
        super().__init__(daemon=True, name="host-" + name)
        self.name, self.ssh, self.python = name, ssh, python
        self.agent_args, self.interval, self.archive = agent_args, interval, archive
        self.latest = None
        self.received_at = 0.0
        self.last_error = ""
        self.connects = 0
        self.lock = threading.Lock()

    def run(self):
        fails = 0
        while True:
            started = time.time()
            try:
                self._session()
            except Exception as e:  # noqa: BLE001
                self.last_error = repr(e)
                log.exception("%s: session crashed", self.name)
            if time.time() - started > 300:
                fails = 0
            fails += 1
            delay = min(300, 5 * 2 ** min(fails, 6))
            log.warning("%s: disconnected (%s); retry in %ss", self.name, self.last_error[-200:], delay)
            time.sleep(delay)

    def _session(self):
        remote = " ".join([self.python, "-u", "-", "--interval", str(self.interval)] + self.agent_args)
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", self.ssh, remote]
        log.info("%s: connecting (%s)", self.name, self.ssh)
        self.connects += 1
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=1, text=True)
        p.stdin.write(AGENT.read_text())
        p.stdin.close()
        errs = []

        def drain():
            for line in p.stderr:
                errs.append(line.rstrip())
                del errs[:-20]
                log.info("%s: stderr: %s", self.name, line.rstrip()[:300])
        threading.Thread(target=drain, daemon=True).start()
        for line in p.stdout:
            try:
                snap = json.loads(line)
            except ValueError:
                log.warning("%s: bad line: %s", self.name, line[:200])
                continue
            with self.lock:
                self.latest, self.received_at = snap, time.time()
                self.last_error = ""
            if self.archive:
                self.archive.write(self.name, snap)
        p.wait()
        time.sleep(0.2)
        self.last_error = (errs[-1] if errs else "") or "ssh exited with %s" % p.returncode

    def get(self):
        with self.lock:
            return self.latest, self.received_at


# ---------------------------------------------------------------- archive
class Archive:
    """data/archive/<host>/<YYYY-MM-DD>.jsonl, gzip'd once the day is over."""

    def __init__(self, root, every):
        self.root, self.every = Path(root), every
        self.last = {}
        self.lock = threading.Lock()
        self.compress_old()

    def compress_old(self):
        today = time.strftime("%Y-%m-%d")
        for f in self.root.glob("*/*.jsonl"):
            if f.stem < today:
                with open(f, "rb") as src, gzip.open(str(f) + ".gz", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                f.unlink()

    def write(self, host, snap):
        ts = snap.get("ts", time.time())
        if ts - self.last.get(host, 0) < self.every - 1:
            return
        self.last[host] = ts
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        d = self.root / host
        d.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with open(d / (day + ".jsonl"), "a") as f:
                f.write(json.dumps(snap, separators=(",", ":")) + "\n")
            if day != getattr(self, "_day", day):
                self.compress_old()
            self._day = day


# ---------------------------------------------------------------- exposition
def esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


class Metrics:
    def __init__(self):
        self.out = {}   # name -> (help, type, [lines])

    def add(self, name, labels, value, help_="", typ="gauge"):
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        m = self.out.setdefault(name, (help_, typ, []))
        lab = ",".join('%s="%s"' % (k, esc(x)) for k, x in labels.items())
        m[2].append("%s{%s} %s" % (name, lab, repr(v)))

    def render(self):
        buf = []
        for name, (h, t, lines) in self.out.items():
            if h:
                buf.append("# HELP %s %s" % (name, h))
            buf.append("# TYPE %s %s" % (name, t))
            buf.extend(lines)
        return "\n".join(buf) + "\n"


def render_host(m, name, snap, age, stale):
    H = {"host": name}
    m.add("servmon_up", H, 0 if stale else 1, "1 if a fresh snapshot is available")
    m.add("servmon_snapshot_age_seconds", H, age if snap else None, "seconds since last snapshot")
    if stale or not snap:
        return
    m.add("servmon_scan_seconds", H, snap.get("scan_seconds"), "agent time spent per snapshot")
    h = snap["host"]
    m.add("servmon_host_procs", H, snap.get("nprocs"))
    m.add("servmon_host_ncpu", H, h.get("ncpu"), "logical CPUs")
    m.add("servmon_host_cpu_busy_ratio", H, h.get("busy"), "fraction of CPU time not idle/iowait")
    m.add("servmon_host_cpu_iowait_ratio", H, h.get("iowait"))
    m.add("servmon_host_mem_total_bytes", H, h.get("mem_total"))
    m.add("servmon_host_mem_available_bytes", H, h.get("mem_available"))
    if h.get("mem_total") is not None and h.get("mem_available") is not None:
        m.add("servmon_host_mem_used_bytes", H, h["mem_total"] - h["mem_available"])
    m.add("servmon_host_mem_shmem_bytes", H, h.get("mem_shmem"), "tmpfs/shared memory")
    m.add("servmon_host_mem_cache_bytes", H, h.get("mem_cached"))
    m.add("servmon_host_swap_total_bytes", H, h.get("swap_total"))
    if h.get("swap_total") is not None:
        m.add("servmon_host_swap_used_bytes", H, h["swap_total"] - (h.get("swap_free") or 0))
    for k in ("load1", "load5", "load15"):
        m.add("servmon_host_" + k, H, h.get(k))
    m.add("servmon_host_uptime_seconds", H, h.get("uptime"))
    for d in h.get("disks", []):
        L = dict(H, mount=d["mount"])
        m.add("servmon_disk_total_bytes", L, d["total"])
        m.add("servmon_disk_used_bytes", L, d["used"])

    m.add("servmon_gpu_available", H, 0 if snap.get("gpu_error") else 1, "1 if nvidia-smi works")
    if snap.get("gpu_error"):
        m.add("servmon_gpu_error_info", dict(H, error=snap["gpu_error"][:200]), 1)
    for g in snap.get("gpus", []):
        L = dict(H, gpu=str(g["index"]), name=g.get("name") or "")
        m.add("servmon_gpu_util_percent", L, g.get("util"), "GPU busy %, averaged over the interval")
        m.add("servmon_gpu_mem_util_percent", L, g.get("mem_util"), "memory controller busy %")
        if g.get("mem_used_mb") is not None:
            m.add("servmon_gpu_mem_used_bytes", L, g["mem_used_mb"] * MiB)
        if g.get("mem_total_mb") is not None:
            m.add("servmon_gpu_mem_total_bytes", L, g["mem_total_mb"] * MiB)
        m.add("servmon_gpu_power_watts", L, g.get("power_w"))
        m.add("servmon_gpu_power_limit_watts", L, g.get("power_limit_w"))
        m.add("servmon_gpu_temp_celsius", L, g.get("temp_c"))

    for user, u in snap.get("users", {}).items():
        if (u["cpu"] < 0.01 and u["anon"] < 64 * MiB and not u.get("gpu") and u.get("shm", 0) < 64 * MiB
                and not u.get("sessions")):
            continue  # idle system accounts etc.: keep the series count (and tables) small
        L = dict(H, user=user)
        m.add("servmon_user_cpu_cores", L, u["cpu"], "CPU cores used by all of the user's processes")
        m.add("servmon_user_mem_bytes", L, u["anon"], "anonymous (private) RSS")
        m.add("servmon_user_rss_bytes", L, u["rss"], "total RSS (double-counts shared pages)")
        m.add("servmon_user_swap_bytes", L, u["swap"])
        m.add("servmon_user_procs", L, u["nproc"])
        m.add("servmon_user_jobs", L, u["njobs"])
        m.add("servmon_user_sessions", L, u.get("sessions", 0), "login sessions (who)")
        m.add("servmon_user_shm_bytes", L, u.get("shm", 0), "bytes of /dev/shm files owned")
        m.add("servmon_user_idle_gpu_mem_bytes", L, u["idle_gpu_mem"], "VRAM held by GPU-idle jobs")
        m.add("servmon_user_idle_mem_bytes", L, u["idle_anon"], "RAM held by fully idle jobs")
        for g, x in u.get("gpu", {}).items():
            LG = dict(L, gpu=g)
            m.add("servmon_user_gpu_mem_bytes", LG, x["mem"])
            m.add("servmon_user_gpu_sm_percent", LG, x["sm"], "SM utilisation attributed to user")

    for j in snap.get("jobs", []):
        L = dict(H, user=j["user"], pid=str(j["pid"]), kind=j["kind"], cmd=j["cmd"][:200],
                 container=j.get("ctr") or "")
        m.add("servmon_job_cpu_cores", L, j["cpu"], "CPU cores used by a job (process tree)")
        m.add("servmon_job_mem_bytes", L, j["anon"], "anonymous (private) RSS of a job")
        m.add("servmon_job_rss_bytes", L, j["rss"])
        m.add("servmon_job_swap_bytes", L, j["swap"])
        m.add("servmon_job_procs", L, j["nproc"])
        m.add("servmon_job_start_time_seconds", L, j.get("started"))
        m.add("servmon_job_gpu_idle_seconds", L, j["gpu_idle_s"], "how long the job held VRAM with ~0 SM")
        m.add("servmon_job_cpu_idle_seconds", L, j["ram_idle_s"], "how long the job held RAM fully idle")
        m.add("servmon_job_idle_gpu_mem_bytes", L, j["idle_gpu_mem"])
        m.add("servmon_job_idle_mem_bytes", L, j["idle_anon"])
        for g, x in j.get("gpu", {}).items():
            LG = dict(L, gpu=g)
            m.add("servmon_job_gpu_mem_bytes", LG, x["mem"])
            m.add("servmon_job_gpu_sm_percent", LG, x["sm"])


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(ROOT / "config.toml"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # as PID 1 in a container, SIGTERM is ignored unless handled
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    cfg = tomllib.loads(Path(args.config).read_text())
    c = cfg.get("collector", {})
    interval = float(c.get("interval", 15))
    agent_args = []
    for k, v in cfg.get("agent", {}).items():
        agent_args += ["--" + k.replace("_", "-"), str(v)]
    archive = None
    if c.get("archive_dir"):
        archive = Archive(ROOT / c["archive_dir"], float(c.get("archive_every", 60)))
    hosts = []
    for h in cfg.get("host", []):
        hs = HostStream(h["name"], h.get("ssh", h["name"]), h.get("python", "python3"),
                        agent_args + (["--no-gpu"] if h.get("no_gpu") else []), interval, archive)
        hs.start()
        hosts.append(hs)

    stale_after = 3 * interval + 15

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            now = time.time()
            if self.path.startswith("/metrics"):
                m = Metrics()
                for hs in hosts:
                    snap, at = hs.get()
                    age = now - at if snap else None
                    render_host(m, hs.name, snap, age, snap is None or age > stale_after)
                    m.add("servmon_ssh_connects_total", {"host": hs.name}, hs.connects, typ="counter")
                    if hs.last_error:
                        m.add("servmon_collector_error_info", {"host": hs.name, "error": hs.last_error[-200:]}, 1)
                self._send(m.render(), "text/plain; version=0.0.4")
            elif self.path.startswith("/api/snapshot"):
                out = {}
                for hs in hosts:
                    snap, at = hs.get()
                    out[hs.name] = {"received_at": at, "error": hs.last_error, "snapshot": snap}
                self._send(json.dumps(out), "application/json")
            elif self.path.startswith("/healthz"):
                self._send("ok\n", "text/plain")
            else:
                self.send_error(404)

    # SERVMON_LISTEN overrides config.toml (the container listens on 0.0.0.0)
    host, port = (os.environ.get("SERVMON_LISTEN") or c.get("listen", "127.0.0.1:9877")).rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    log.info("listening on http://%s:%s/metrics for %d hosts", host, port, len(hosts))
    srv.serve_forever()


if __name__ == "__main__":
    main()
