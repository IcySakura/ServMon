# servmon: who is using the shared servers, and how

Tracks CPU, RAM, GPU compute, VRAM and *idle-held* resources on shared Linux/NVIDIA
servers. Usage is attributed per server, per user, per job (process tree) and per workload
type. Grafana dashboards show it live or over any time range.

> [!NOTE]
> **About this project.** Most of the code and docs were written by an AI coding assistant,
> directed and reviewed by a human. It has only been tested on one setup: a few Linux
> servers with NVIDIA GPUs, watched from one Linux machine running rootless Podman. The native setup was used before that; the Docker setup (`compose.yaml`) has not
> been tested with Docker itself. Expect rough edges
> elsewhere, and check the code before trusting it with sensitive data.

```
 server A ─┐   ssh (one long-lived session each)        monitoring machine (127.0.0.1 only)
 server B ─┼──────────────────────────────▶  collector :9877 ──▶ Prometheus :9390 ──▶ Grafana :3300
 server C ─┤  agent.py streamed over stdin,     │
 server D ─┘  prints a JSON snapshot / 15 s     └──▶ data/archive/<host>/<day>.jsonl.gz
```

* **Nothing is installed on the servers** and no root is needed. `servmon/agent.py` is
  piped into `python3 -` over SSH. It reads `/proc` and `nvidia-smi`, and exits when the SSH
  session ends. Cost per server: ~0.2 s of CPU every 15 s, plus one `nvidia-smi pmon` and one
  `nvidia-smi --query-gpu -l 1` process.
* **Everything else runs in one rootless Podman pod on the monitoring machine:** the
  collector (stdlib Python + OpenSSH + cloudflared), Prometheus (storage, 2 years / 40 GB)
  and Grafana (dashboards), managed by systemd. Docker and a no-container setup are also
  supported.
* **Why custom code:** off-the-shelf exporters (node_exporter, dcgm-exporter,
  process-exporter) need a daemon and an open port on every server, and none of them
  attributes GPU SM/VRAM to users and process trees.

## Quick start

```bash
# 1. Servers: one [[host]] block each in config.toml, using SSH aliases from ~/.ssh/config
#    that work non-interactively (docs/adding-a-server.md).
# 2. Start (Podman >= 5; for Docker see docs/deployment.md):
scripts/setup.sh                 # creates .env and data/
scripts/podman-install.sh        # builds the collector image, starts the pod under systemd
loginctl enable-linger $USER     # keep running while logged out
# 3. Open http://127.0.0.1:3300  (admin login: admin / GRAFANA_ADMIN_PASSWORD from .env)
```

To view it from another machine over SSH, run `ssh -L 3300:127.0.0.1:3300 <this-machine>`.
For other ways, and what to check before exposing it, see
[docs/remote-access.md](docs/remote-access.md).

| Guide | |
|---|---|
| [docs/adding-a-server.md](docs/adding-a-server.md) | Watching a new server: requirements, SSH setup (direct, jump host, Cloudflare tunnel), testing, troubleshooting |
| [docs/deployment.md](docs/deployment.md) | Podman (default), Docker or native; day-to-day commands, data and backups, moving machines, publishing the repo |
| [docs/remote-access.md](docs/remote-access.md) | Ways to reach the dashboards from elsewhere (SSH, VPN, reverse proxy, tunnel), login options and a security checklist |

## Dashboards

| Dashboard | Use it for |
|---|---|
| **Overview (live)** | Fleet table, per-server CPU/RAM/GPU/VRAM, stacked usage by user, every GPU process right now, top jobs right now. Auto-refreshes every 15 s. |
| **Usage accounting** | Pick any time range. Shows per-user and per-server×user CPU core-hours, RAM GiB-hours, GPU compute-hours, VRAM GiB-hours, peaks, and idle share; usage per hour/day bucket; usage by workload type; top-40 jobs by GPU, VRAM and CPU. Clicking a user anywhere opens this dashboard filtered to them. |
| **Idle & waste** | Jobs holding VRAM with ~0 % SM, jobs holding RAM with no CPU or GPU activity, `/dev/shm` hogs, GPUs with memory used but low utilisation, and period totals of idle-held GiB-hours. |
| **Job explorer** | Follow individual programs over time; filter by command regex (e.g. `train.*\.py`, `vllm`, `ipykernel`). |
| **Server detail** | Hardware view: CPU/iowait, load, memory and shmem, disks, GPU util/mem-util/VRAM/power/temp, login sessions, collector health. |

All dashboards share `host`, `user` and `workload` filters, and the links in the top bar
keep the current filters and time range.

## Definitions

* **Job:** a process plus all descendants owned by the same user, rooted at the first process
  below a shell, tmux/screen, sshd, IDE server (VS Code, JetBrains), Jupyter server, or
  container shim. 16 dataloader workers under one `train.py` are therefore one job. Each
  Jupyter kernel is its own job. Labels: `host, user, pid (of root), kind, cmd (root cmdline,
  200 chars), container`.
* **Workload kind:** a regex on the root's command line (`KIND_RULES` in `servmon/agent.py`):
  `train, eval, python, jupyter, ide, agent, llm-serve, eda, matlab, build, desktop, shell,
  container, system, other`. If the root is a bare wrapper script, the job's biggest child
  decides the kind.
* **RAM** = anonymous RSS (`RssAnon`), i.e. memory the process owns. Total RSS
  (`servmon_*_rss_bytes`) is also exported, but it double-counts shared libraries and page cache.
* **GPU compute** = per-process SM % from `nvidia-smi pmon`, sampled every second and averaged
  over the 15 s interval. 1 GPU compute-hour = one GPU at 100 % SM for an hour.
* **GPU-idle job:** holds ≥ 256 MiB VRAM with < 1 % SM.
  **Fully idle job:** holds ≥ 1 GiB RAM with < 0.02 cores and no SM.
  After 10 min continuously idle, the job's memory counts as *idle-held*. Thresholds are in
  `config.toml` `[agent]`. The idle clock lives in the agent, so it restarts if the SSH
  session reconnects.
* **Short-lived GPU processes** (e.g. workers that live a few seconds) are identified when
  `nvidia-smi pmon` first sees them. If they exit before the `/proc` scan, they still count
  toward their parent's job (or their own), with VRAM scaled by how long they were alive.
  `(unknown)` appears only if a process vanished before it could be identified at all.
* Jobs are exported only if they use ≥ 0.1 core, ≥ 512 MiB RAM, or any VRAM. Per-user totals
  include every process.
* **Command lines are redacted on the server:** values of secret-looking arguments
  (`--token X`, `--api-key=X`, `HF_TOKEN=X`, `password=X`, `scheme://user:pass@`, JWTs,
  `sk-…`/`hf_…`/`ghp_…` keys) are replaced by `***` before they leave the server
  (`SECRET_RES` in `servmon/agent.py`).

## Layout

```
config.toml                  servers, interval, idle thresholds
.env.example                 deployment settings (copied to .env by scripts/setup.sh)
compose.yaml                 Docker alternative to scripts/podman-install.sh
servmon/agent.py             runs on each server (streamed over SSH)
servmon/collector.py         SSH sessions → /metrics + archive
deploy/docker/               collector image (Podman and Docker)
deploy/prometheus*.yml       scrape config (pod and native / Docker)
deploy/grafana/              grafana.ini, grafana.env.example (your overrides), provisioning,
                             gen_dashboards.py → dashboards/*.json
scripts/                     setup.sh, podman-install.sh, check_dashboards.py;
                             native: fetch-vendor.sh, install-services.sh, run-*.sh
docs/                        guides (see above)
data/                        Prometheus TSDB, Grafana DB, raw archive   (git-ignored)
vendor/                      native-mode binaries                       (git-ignored)
```
