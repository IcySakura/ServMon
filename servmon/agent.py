#!/usr/bin/env python3
"""servmon agent.

Streamed to a server over SSH (`ssh host python3 -u - [args] < agent.py`) by the
collector. Needs no root and installs nothing: it reads /proc and nvidia-smi and
prints one JSON snapshot per interval to stdout until the SSH session ends.

What a snapshot contains:
  host   - CPU busy/iowait, memory, swap, load, disks
  gpus   - per-GPU utilisation (averaged over the interval), VRAM, power, temp
  users  - per-user totals over *all* their processes (CPU, RAM, VRAM, SM, idle-held)
  jobs   - per-"job" (a process tree rooted at the first non-shell ancestor of the
           same user) for jobs that are significant (CPU, RAM or any VRAM)
  shm    - /dev/shm bytes per owner (RAM held by files, outside any process)
Python >= 3.6, stdlib only.
"""
import argparse
import ctypes
import json
import os
import pwd
import re
import signal
import socket
import subprocess
import sys
import threading
import time

CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
MiB = 1 << 20

# ---------------------------------------------------------------- classification
# A process whose *parent* matches one of these is the root of its own job.
BOUNDARY_COMMS = {
    "bash", "sh", "dash", "zsh", "fish", "tcsh", "csh", "ksh", "tmux: server", "tmux",
    "screen", "SCREEN", "sshd", "sshd-session", "systemd", "init", "login", "su", "sudo",
    "cron", "containerd-shim", "containerd-shim-runc-v2", "dockerd", "conmon", "nohup",
    "setsid", "xargs", "env", "srun", "slurmstepd", "byobu",
}
BOUNDARY_CMD_RE = re.compile(
    r"jupyter[- ](lab|notebook|server)|\.vscode-server|\.cursor-server|\.windsurf-server|"
    r"JetBrains|remote-dev-server|code-server|\bclaude\b|/codex\b"
)

# First match wins. Applied to "comm + cmdline" of the job root.
KIND_RULES = [
    ("ide", r"\.vscode-server|\.cursor-server|\.windsurf-server|JetBrains|remote-dev-server|pycharm|code-server|pyright|pylance|copilot-language"),
    ("jupyter", r"ipykernel|jupyter"),
    ("agent", r"(^|[\s/])(claude|codex|gemini|aider|opencode)(\s|$)|@anthropic-ai|claude-code"),
    ("llm-serve", r"vllm|llama-server|llama\.cpp|ollama|sglang|text-generation|tritonserver|lmdeploy|tgi"),
    ("train", r"torchrun|deepspeed|accelerate launch|python.*(train|finetune|fine_tune|pretrain|sweep)"),
    ("eval", r"python.*(eval|infer|test|bench|predict|sample|rollout|server)"),
    ("python", r"python"),
    ("eda", r"vivado|vitis|quartus|xilinx|cadence|virtuoso|synopsys|\bvcs\b|xcelium|innovus|genus|modelsim|questa|verilator"),
    ("matlab", r"matlab"),
    ("build", r"(^|[\s/])(make|cmake|ninja|gcc|g\+\+|cc1|cc1plus|ld|clang|nvcc|cargo|rustc|bazel|ptxas)(\s|$)"),
    ("desktop", r"Xorg|Xwayland|gnome|gdm|xrdp|vnc|pulseaudio|pipewire|firefox|chrome"),
    ("shell", r"^(-?bash|-?zsh|-?fish|sh|tmux|screen|sshd|mosh)"),
]
KIND_RULES = [(k, re.compile(p, re.I)) for k, p in KIND_RULES]


def classify(comm, cmd, uid, container):
    s = comm + " " + cmd
    for kind, rx in KIND_RULES:
        if rx.search(s):
            return kind
    if container:
        return "container"
    if uid < 1000:
        return "system"
    return "other"


# ---------------------------------------------------------------- helpers
def read(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except (OSError, ValueError):
        return None


def set_pdeathsig():
    """Make children die with us (prctl PR_SET_PDEATHSIG)."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


_users = {}


def uname(uid):
    if uid not in _users:
        try:
            _users[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _users[uid] = str(uid)
    return _users[uid]


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# Values of secret-looking arguments are masked on the server, before anything is sent
# (cmdlines become Prometheus labels and can end up on a shared web page).
SECRET_RES = [
    # --token X, --api-key=X, -password X, --hf-token X  (not --max-tokens, --tokenizer)
    (re.compile(r"(?i)(\s--?[\w.-]*(?:token(?!s|iz)|secret|passw|api[_-]?key|access[_-]?key|credential)"
                r"[\w.-]*(?:=|\s+))(?!-)\S+"), r"\1***"),
    # HF_TOKEN=X, password=X, aws_secret_access_key:X
    (re.compile(r"(?i)(\b[\w.-]*(?:token(?!s|iz)|secret|passw|api[_-]?key|access[_-]?key|credential)"
                r"[\w.-]*[=:])(?!//)\S+"), r"\1***"),
    # scheme://user:password@host
    (re.compile(r"(\b[a-zA-Z][\w+.-]*://[^\s/:@]*:)[^\s@/]+@"), r"\1***@"),
    # JWTs and well-known key formats
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}(?:\.[\w-]*)?"), "***"),
    (re.compile(r"\b(?:sk-|hf_|ghp_|gho_|ghs_|github_pat_|xox[abpr]-|AKIA)[\w-]{12,}"), "***"),
]


def clean_cmd(raw, comm):
    """/proc/<pid>/cmdline bytes -> one-line, secret-redacted string."""
    cmd = re.sub(r"\s+", " ", raw.replace(b"\0", b" ").decode("utf-8", "replace")).strip()
    if not cmd:
        return "[%s]" % comm
    cmd = " " + cmd
    for rx, sub in SECRET_RES:
        cmd = rx.sub(sub, cmd)
    return cmd[1:]


def proc_identity(pid):
    """(uid, ppid, comm, cmd) of a live pid, or None."""
    st = read("/proc/%d/status" % pid)
    if not st:
        return None
    uid, ppid, comm = -1, 0, "?"
    for line in st.splitlines():
        k, _, v = line.partition(":")
        if k == "Uid":
            uid = int(v.split()[0])
        elif k == "PPid":
            ppid = int(v.split()[0])
        elif k == "Name":
            comm = v.strip()
    return uid, ppid, comm, clean_cmd(read("/proc/%d/cmdline" % pid, "rb") or b"", comm)


# ---------------------------------------------------------------- GPU monitors
class StreamReader(threading.Thread):
    """Runs a long-lived nvidia-smi command, restarting it if it dies."""

    def __init__(self, argv, on_line, retry=300):
        super().__init__(daemon=True)
        self.argv, self.on_line, self.retry = argv, on_line, retry
        self.error = None
        self.alive = False

    def run(self):
        while True:
            try:
                p = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     universal_newlines=True, bufsize=1, preexec_fn=set_pdeathsig)
            except OSError as e:
                self.error = str(e)
                time.sleep(self.retry)
                continue
            got = False
            tail = []
            for line in p.stdout:
                if line.strip():
                    tail = (tail + [line.strip()])[-3:]
                if self.on_line(line):
                    got = True
                    self.alive, self.error = True, None
            p.wait()
            self.alive = False
            if not got:
                self.error = " | ".join(tail)[:300] or "nvidia-smi exited (%s)" % p.returncode
                time.sleep(self.retry)
            else:
                time.sleep(5)


class GpuMonitor:
    QUERY = ["index", "uuid", "name", "utilization.gpu", "utilization.memory", "memory.used",
             "memory.total", "power.draw", "power.limit", "temperature.gpu"]

    def __init__(self):
        self.lock = threading.Lock()
        self.dev = {}         # idx -> dict(last values, util_sum, memutil_sum, n)
        self.uuid2idx = {}
        self.pm_cols = None
        self.pm = {}          # (gpu, pid) -> [sm_sum, mem_sum, fb_last, name, type]
        self.pm_ticks = {}    # gpu -> set(timestamps seen this interval)
        self.ident = {}       # pid -> proc_identity(), captured when first seen on a GPU
        self.ok = False
        self.q = StreamReader(["nvidia-smi", "--query-gpu=" + ",".join(self.QUERY),
                               "--format=csv,noheader,nounits", "-l", "1"], self._on_q)
        self.p = StreamReader(["nvidia-smi", "pmon", "-s", "um", "-o", "T", "-d", "1"], self._on_pmon)
        self.q.start()
        self.p.start()

    def _on_q(self, line):
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != len(self.QUERY) or not parts[0].isdigit():
            return False
        r = dict(zip(self.QUERY, parts))
        idx = int(r["index"])
        with self.lock:
            d = self.dev.setdefault(idx, {"util_sum": 0.0, "memutil_sum": 0.0, "n": 0})
            self.uuid2idx[r["uuid"]] = idx
            d.update(uuid=r["uuid"], name=r["name"], mem_used_mb=fnum(r["memory.used"]),
                     mem_total_mb=fnum(r["memory.total"]), power_w=fnum(r["power.draw"]),
                     power_limit_w=fnum(r["power.limit"]), temp_c=fnum(r["temperature.gpu"]))
            u, mu = fnum(r["utilization.gpu"]), fnum(r["utilization.memory"])
            if u is not None:
                d["util_sum"] += u
                d["memutil_sum"] += mu or 0.0
                d["n"] += 1
        return True

    def _on_pmon(self, line):
        if line.startswith("#"):
            if "pid" in line and self.pm_cols is None:
                self.pm_cols = line.lstrip("#").split()
            return False
        if not self.pm_cols:
            return False
        parts = line.split(None, len(self.pm_cols) - 1)
        if len(parts) < len(self.pm_cols) - 1:
            return False
        r = dict(zip(self.pm_cols, parts))
        try:
            gpu = int(r["gpu"])
        except (KeyError, ValueError):
            return False
        with self.lock:
            self.pm_ticks.setdefault(gpu, set()).add(r.get("Time", ""))
            pid = r.get("pid", "-")
            if pid.isdigit():
                pid = int(pid)
                if pid not in self.ident:
                    self.ident[pid] = proc_identity(pid)
                e = self.pm.setdefault((gpu, pid), [0.0, 0.0, None, "", "", 0])
                e[5] += 1
                e[0] += fnum(r.get("sm")) or 0.0
                e[1] += fnum(r.get("mem")) or 0.0
                fb = fnum(r.get("fb"))
                if fb is not None:
                    e[2] = fb
                e[3] = r.get("command", "").strip()
                e[4] = r.get("type", "")
        return True

    def snapshot(self):
        """Returns (gpus list, {(gpu,pid): {mem_mb, sm, memutil, name, type}}, error)."""
        apps = {}
        try:
            out = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory",
                                  "--format=csv,noheader,nounits"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20).stdout
            for line in out.splitlines():
                p = [x.strip() for x in line.split(",")]
                if len(p) == 3 and p[1].isdigit():
                    apps[(p[0], int(p[1]))] = fnum(p[2])
        except Exception:
            pass
        with self.lock:
            gpus = []
            for idx, d in sorted(self.dev.items()):
                n = d["n"]
                gpus.append({
                    "index": idx, "uuid": d.get("uuid"), "name": d.get("name"),
                    "util": d["util_sum"] / n if n else None,
                    "mem_util": d["memutil_sum"] / n if n else None,
                    "mem_used_mb": d.get("mem_used_mb"), "mem_total_mb": d.get("mem_total_mb"),
                    "power_w": d.get("power_w"), "power_limit_w": d.get("power_limit_w"),
                    "temp_c": d.get("temp_c"),
                })
                d["util_sum"] = d["memutil_sum"] = 0.0
                d["n"] = 0
            procs = {}
            for (gpu, pid), (sm, mu, fb, name, typ, seen) in self.pm.items():
                ticks = max(1, len(self.pm_ticks.get(gpu, ())))
                procs[(gpu, pid)] = {"mem_mb": fb, "sm": sm / ticks, "memutil": mu / ticks,
                                     "name": name, "type": typ, "frac": min(1.0, seen / ticks),
                                     "ident": self.ident.get(pid)}
            for (uuid, pid), mem in apps.items():
                gpu = self.uuid2idx.get(uuid)
                if gpu is None:
                    continue
                e = procs.setdefault((gpu, pid), {"sm": 0.0, "memutil": 0.0, "name": "", "type": "C",
                                                  "frac": 1.0, "ident": self.ident.get(pid)})
                e["mem_mb"] = mem
            live = {pid for (_, pid) in self.pm}
            self.ident = {p: v for p, v in self.ident.items() if p in live}
            self.pm, self.pm_ticks = {}, {}
            err = None if (self.q.alive or gpus) else (self.q.error or "no data from nvidia-smi")
            if not self.q.alive:
                gpus = []
        return gpus, procs, err


# ---------------------------------------------------------------- /proc scanning
class ProcScanner:
    def __init__(self):
        self.prev_ticks = {}      # (pid, start) -> cpu ticks
        self.prev_time = None
        self.cache = {}           # (pid, start) -> (cmd, container)
        self.btime = 0
        for line in (read("/proc/stat") or "").splitlines():
            if line.startswith("btime"):
                self.btime = int(line.split()[1])

    @staticmethod
    def _container(pid):
        cg = read("/proc/%d/cgroup" % pid) or ""
        m = re.search(r"(?:docker|cri-containerd|libpod)[-/]([0-9a-f]{12})", cg)
        return m.group(1) if m else ""

    def scan(self):
        now = time.monotonic()
        procs = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            st = read("/proc/%d/stat" % pid)
            if not st:
                continue
            try:
                r = st.rindex(")")
                comm = st[st.index("(") + 1:r]
                f = st[r + 2:].split()
                ppid, ticks, start = int(f[1]), int(f[11]) + int(f[12]), int(f[19])
            except (ValueError, IndexError):
                continue
            if ppid == 2 or pid == 2:
                continue  # kernel threads
            status = read("/proc/%d/status" % pid) or ""
            uid, rss, anon, shmem, swap = 0, 0, 0, 0, 0
            for line in status.splitlines():
                k, _, v = line.partition(":")
                if k == "Uid":
                    uid = int(v.split()[0])
                elif k == "VmRSS":
                    rss = int(v.split()[0]) * 1024
                elif k == "RssAnon":
                    anon = int(v.split()[0]) * 1024
                elif k == "RssShmem":
                    shmem = int(v.split()[0]) * 1024
                elif k == "VmSwap":
                    swap = int(v.split()[0]) * 1024
            key = (pid, start)
            if key not in self.cache:
                self.cache[key] = (clean_cmd(read("/proc/%d/cmdline" % pid, "rb") or b"", comm),
                                   self._container(pid))
            cmd, ctr = self.cache[key]
            if key in self.prev_ticks:
                dt_ticks = ticks - self.prev_ticks[key]
            elif self.prev_time is not None and self.btime and \
                    self.btime + start / CLK > time.time() - (now - self.prev_time):
                dt_ticks = ticks  # born during this interval
            else:
                dt_ticks = 0
            procs[pid] = {"pid": pid, "ppid": ppid, "comm": comm, "uid": uid, "start": start,
                          "ticks": ticks, "dticks": max(0, dt_ticks), "rss": rss, "anon": anon,
                          "shmem": shmem, "swap": swap, "cmd": cmd, "ctr": ctr}
        dt = (now - self.prev_time) if self.prev_time else None
        self.prev_ticks = {(p["pid"], p["start"]): p["ticks"] for p in procs.values()}
        self.prev_time = now
        live = set(self.prev_ticks)
        if len(self.cache) > 4 * len(live) + 1000:
            self.cache = {k: v for k, v in self.cache.items() if k in live}
        for p in procs.values():
            p["cpu"] = (p["dticks"] / CLK / dt) if dt else 0.0
        return procs, dt

    @staticmethod
    def job_roots(procs):
        roots = {}

        def is_boundary(p):
            return p["comm"] in BOUNDARY_COMMS or bool(BOUNDARY_CMD_RE.search(p["cmd"]))

        def root(pid):
            chain = []
            cur = pid
            while True:
                if cur in roots:
                    r = roots[cur]
                    break
                chain.append(cur)
                p = procs[cur]
                par = procs.get(p["ppid"])
                if par is None or par["pid"] <= 1 or par["uid"] != p["uid"] or is_boundary(par) \
                        or len(chain) > 64:
                    r = cur
                    break
                cur = par["pid"]
            for c in chain:
                roots[c] = r
            return r

        for pid in procs:
            root(pid)
        return roots


# ---------------------------------------------------------------- host metrics
class HostScanner:
    def __init__(self):
        self.prev_cpu = None
        self.disks = []
        self.disks_at = 0

    def cpu(self):
        line = (read("/proc/stat") or "cpu 0").splitlines()[0].split()[1:]
        v = [int(x) for x in line[:8]] + [0] * (8 - len(line[:8]))
        total, idle, iowait = sum(v), v[3], v[4]
        out = {"busy": None, "iowait": None}
        if self.prev_cpu:
            dt = total - self.prev_cpu[0]
            if dt > 0:
                out["busy"] = 1 - (idle + iowait - self.prev_cpu[1] - self.prev_cpu[2]) / dt
                out["iowait"] = (iowait - self.prev_cpu[2]) / dt
        self.prev_cpu = (total, idle, iowait)
        return out

    def scan(self):
        h = {"ncpu": os.cpu_count()}
        h.update(self.cpu())
        mi = {}
        for line in (read("/proc/meminfo") or "").splitlines():
            k, _, v = line.partition(":")
            mi[k] = int(v.split()[0]) * 1024 if v.split() else 0
        h.update(mem_total=mi.get("MemTotal"), mem_available=mi.get("MemAvailable"),
                 mem_shmem=mi.get("Shmem"), mem_cached=mi.get("Cached", 0) + mi.get("Buffers", 0),
                 swap_total=mi.get("SwapTotal"), swap_free=mi.get("SwapFree"))
        la = (read("/proc/loadavg") or "0 0 0").split()
        h.update(load1=float(la[0]), load5=float(la[1]), load15=float(la[2]))
        h["uptime"] = float((read("/proc/uptime") or "0").split()[0])
        if time.time() - self.disks_at > 60:
            self.disks_at = time.time()
            seen, disks = set(), []
            for line in (read("/proc/mounts") or "").splitlines():
                p = line.split()
                if len(p) < 3 or p[2] not in ("ext4", "xfs", "btrfs", "zfs", "f2fs", "ext3"):
                    continue
                if p[0] in seen:
                    continue
                seen.add(p[0])
                try:
                    s = os.statvfs(p[1])
                except OSError:
                    continue
                disks.append({"mount": p[1], "total": s.f_blocks * s.f_frsize,
                              "used": (s.f_blocks - s.f_bfree) * s.f_frsize})
            self.disks = disks
        h["disks"] = self.disks
        return h


def scan_shm():
    by = {}
    for root, dirs, files in os.walk("/dev/shm", onerror=lambda e: None):
        for fn in files:
            try:
                s = os.lstat(os.path.join(root, fn))
            except OSError:
                continue
            by[s.st_uid] = by.get(s.st_uid, 0) + s.st_blocks * 512
    return {uname(u): b for u, b in by.items()}


def sessions():
    """Login sessions per user: `who` (utmp), falling back to logind when utmp is empty."""
    out = {}
    try:
        txt = subprocess.run(["who"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=10).stdout
        for line in txt.splitlines():
            if line.split():
                u = line.split()[0]
                out[u] = out.get(u, 0) + 1
    except Exception:
        pass
    if out:
        return out
    try:
        txt = subprocess.run(["loginctl", "list-sessions", "--no-legend"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10).stdout
        for line in txt.splitlines():
            f = line.split()
            if len(f) >= 3 and "manager" not in f and "background" not in f:
                out[f[2]] = out.get(f[2], 0) + 1
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- main loop
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=15)
    ap.add_argument("--job-min-cpu", type=float, default=0.1, help="cores")
    ap.add_argument("--job-min-mem-mb", type=float, default=512, help="anon RSS")
    ap.add_argument("--idle-cpu", type=float, default=0.02, help="cores; below = CPU-idle")
    ap.add_argument("--idle-sm", type=float, default=1.0, help="SM %%; below = GPU-idle")
    ap.add_argument("--idle-gpu-mem-mb", type=float, default=256)
    ap.add_argument("--idle-ram-mb", type=float, default=1024)
    ap.add_argument("--idle-after", type=float, default=600, help="seconds before a job counts as idle")
    ap.add_argument("--no-gpu", action="store_true")
    a = ap.parse_args()

    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, lambda *_: os._exit(0))
    parent = os.getppid()
    gpu = None if a.no_gpu else GpuMonitor()
    ps, hs = ProcScanner(), HostScanner()
    ps.scan()
    hs.scan()
    streak = {}      # (root pid, start) -> {"gpu": s, "ram": s}
    shm, shm_at = {}, 0
    hostname = socket.gethostname()
    next_t = time.monotonic() + a.interval

    while True:
        time.sleep(max(0.0, next_t - time.monotonic()))
        next_t += a.interval
        if os.getppid() != parent:
            os._exit(0)  # ssh session went away
        t0 = time.time()
        procs, dt = ps.scan()
        dt = dt or a.interval
        host = hs.scan()
        gpus, gprocs, gerr = gpu.snapshot() if gpu else ([], {}, "disabled")
        if t0 - shm_at > 60:
            shm, shm_at = scan_shm(), t0
        roots = ps.job_roots(procs)

        # attach GPU usage to processes. A GPU process that already exited (short-lived
        # workers) is identified from what was captured when pmon first saw it; it joins
        # its parent's job if the parent is still alive, and its VRAM is scaled by the
        # fraction of the interval it was alive for.
        for (g, pid), e in gprocs.items():
            if pid not in procs:
                if not e.get("mem_mb"):
                    continue
                ident = e.get("ident") or proc_identity(pid)
                if ident:
                    uid, ppid, comm, cmd = ident
                else:
                    uid, ppid, comm, cmd = -1, 0, e["name"] or "?", e["name"] or "?"
                e = dict(e, mem_mb=e["mem_mb"] * e.get("frac", 1.0))
                procs[pid] = {"pid": pid, "ppid": ppid, "comm": comm, "uid": uid, "start": 0, "rss": 0,
                              "anon": 0, "shmem": 0, "swap": 0, "cpu": 0.0, "cmd": cmd, "ctr": ""}
                par = procs.get(ppid)
                roots[pid] = roots.get(ppid, pid) if (par and par["uid"] == uid and uid >= 0) else pid
            procs[pid].setdefault("gpu", {})[g] = e

        jobs, users = {}, {}
        for pid, p in procs.items():
            r = roots[pid]
            j = jobs.get(r)
            if j is None:
                rp = procs[r]
                j = jobs[r] = {
                    "pid": r, "start": rp["start"], "uid": rp["uid"],
                    "user": uname(rp["uid"]) if rp["uid"] >= 0 else "(unknown)",
                    "comm": rp["comm"], "cmd": rp["cmd"][:300], "ctr": rp["ctr"],
                    "kind": classify(rp["comm"], rp["cmd"], rp["uid"] if rp["uid"] >= 0 else 0, rp["ctr"]),
                    "started": (ps.btime + rp["start"] / CLK) if rp["start"] else None,
                    "nproc": 0, "cpu": 0.0, "rss": 0, "anon": 0, "shmem": 0, "swap": 0, "gpu": {},
                    "_dom": None, "_domkey": (-1, -1),
                }
            j["nproc"] += 1
            dk = (sum((e.get("mem_mb") or 0) for e in p.get("gpu", {}).values()), p["anon"])
            if pid != r and dk > j["_domkey"]:
                j["_dom"], j["_domkey"] = p, dk
            j["cpu"] += p["cpu"]
            for k in ("rss", "anon", "shmem", "swap"):
                j[k] += p[k]
            for g, e in p.get("gpu", {}).items():
                jg = j["gpu"].setdefault(str(g), {"mem": 0, "sm": 0.0, "memutil": 0.0})
                jg["mem"] += int((e.get("mem_mb") or 0) * MiB)
                jg["sm"] += e.get("sm") or 0.0
                jg["memutil"] += e.get("memutil") or 0.0

        # idle streaks & per-user totals
        new_streak = {}
        out_jobs = []
        for r, j in jobs.items():
            d = j.pop("_dom")
            j.pop("_domkey")
            if d is not None and j["kind"] in ("shell", "other"):
                k = classify(d["comm"], d["cmd"], max(d["uid"], 0), d["ctr"])
                if k not in ("shell", "other", "system"):
                    j["kind"] = k
            key = (r, j["start"])
            s = streak.get(key, {"gpu": 0.0, "ram": 0.0})
            gmem = sum(x["mem"] for x in j["gpu"].values())
            gsm = sum(x["sm"] for x in j["gpu"].values())
            gpu_idle = gmem >= a.idle_gpu_mem_mb * MiB and gsm < a.idle_sm
            ram_idle = j["anon"] >= a.idle_ram_mb * MiB and j["cpu"] < a.idle_cpu and gsm < a.idle_sm
            s = {"gpu": s["gpu"] + dt if gpu_idle else 0.0, "ram": s["ram"] + dt if ram_idle else 0.0}
            new_streak[key] = s
            j["gpu_idle_s"], j["ram_idle_s"] = s["gpu"], s["ram"]
            j["idle_gpu_mem"] = gmem if s["gpu"] >= a.idle_after else 0
            j["idle_anon"] = j["anon"] if s["ram"] >= a.idle_after else 0

            u = users.setdefault(j["user"], {"cpu": 0.0, "rss": 0, "anon": 0, "swap": 0, "nproc": 0,
                                             "njobs": 0, "gpu": {}, "idle_gpu_mem": 0, "idle_anon": 0})
            u["cpu"] += j["cpu"]
            for k in ("rss", "anon", "swap", "nproc", "idle_gpu_mem", "idle_anon"):
                u[k] += j[k]
            u["njobs"] += 1
            for g, x in j["gpu"].items():
                ug = u["gpu"].setdefault(g, {"mem": 0, "sm": 0.0})
                ug["mem"] += x["mem"]
                ug["sm"] += x["sm"]
            if j["cpu"] >= a.job_min_cpu or j["anon"] >= a.job_min_mem_mb * MiB or gmem > 0:
                del j["uid"]
                out_jobs.append(j)
        streak = new_streak

        sess = sessions()
        for name, n in sess.items():
            users.setdefault(name, {"cpu": 0.0, "rss": 0, "anon": 0, "swap": 0, "nproc": 0, "njobs": 0,
                                    "gpu": {}, "idle_gpu_mem": 0, "idle_anon": 0})
        for name, u in users.items():
            u["sessions"] = sess.get(name, 0)
            u["shm"] = shm.get(name, 0)
        for name, b in shm.items():
            if name not in users:
                users[name] = {"cpu": 0.0, "rss": 0, "anon": 0, "swap": 0, "nproc": 0, "njobs": 0,
                               "gpu": {}, "idle_gpu_mem": 0, "idle_anon": 0, "sessions": 0, "shm": b}

        snap = {"ts": t0, "hostname": hostname, "interval": dt, "host": host, "gpus": gpus,
                "gpu_error": gerr, "users": users, "jobs": out_jobs,
                "scan_seconds": time.time() - t0, "nprocs": len(procs)}
        sys.stdout.write(json.dumps(snap, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        os._exit(0)
