# Deployment and operations

The same three processes (collector, Prometheus, Grafana) can run three ways. They all
use the same `config.toml`, `.env`, dashboards and `data/` directory, so you can switch
between them without losing history.

| | **Podman** (recommended on Linux) | Docker | Native |
|---|---|---|---|
| How | rootless pod, units managed by systemd (Quadlet) | `compose.yaml` | systemd user services running downloaded binaries |
| Needs | Podman ≥ 5 | Docker + compose plugin ≥ 2.24 | Python ≥ 3.11, `ssh`, and `cloudflared` if a host uses it |
| Root | never | daemon runs as root (or rootless Docker) | never |
| Install | `scripts/podman-install.sh` | `docker compose up -d --build` | `scripts/fetch-vendor.sh && scripts/install-services.sh` |

## Podman

```bash
scripts/setup.sh                 # once: .env (your uid/gid, SSH_DIR) and data/
scripts/podman-install.sh        # build the collector image, write the units, start
loginctl enable-linger $USER     # once: keep running while logged out and after reboots
```

The installer writes `~/.config/containers/systemd/servmon*.{pod,container}`. systemd
turns them into these units:

| Unit | Container |
|---|---|
| `servmon-pod.service` | the pod. It publishes `127.0.0.1:3300` (Grafana), `:9390` (Prometheus) and `:9877` (collector). |
| `servmon-collector.service` | `localhost/servmon-collector`, built from `deploy/docker/collector.Dockerfile` |
| `servmon-prometheus.service` | `docker.io/prom/prometheus` |
| `servmon-grafana.service` | `docker.io/grafana/grafana` |
| `servmon-tunnel.service` | optional `cloudflared`, only if `TUNNEL_TOKEN` is set in `.env` |

All containers share the pod's network, so they reach each other on `localhost`. No
container network is created. They run as your uid (`UserNS=keep-id`), so `data/` stays
owned by you. The collector sees only `config.toml`, `servmon/` (read-only), `SSH_DIR`
(read-only) and `data/archive`.

Re-run `scripts/podman-install.sh` after changing `.env`,
`deploy/grafana/grafana.env` or the Dockerfile, and after bumping image versions. It is
safe to run repeatedly. `scripts/podman-install.sh uninstall` removes the units and
keeps `data/`.

### Day to day

```bash
systemctl --user status 'servmon-*'            # status
journalctl --user -u servmon-collector -f      # SSH connects/reconnects, errors
systemctl --user restart servmon-collector     # after editing config.toml or servmon/*.py
python3 deploy/grafana/gen_dashboards.py       # after editing dashboards (Grafana reloads in 30 s)
python3 scripts/check_dashboards.py 1h         # run every dashboard query against Prometheus
curl -s 127.0.0.1:9877/api/snapshot | jq .     # latest raw snapshot from every server
systemctl --user stop servmon-pod              # stop everything (data/ is kept)
podman exec -it servmon-collector bash         # shell inside the collector container
```

## Docker

```bash
scripts/setup.sh
docker compose up -d --build
```

The setup is the same, except that the containers talk over a compose network instead
of a pod. Prometheus therefore uses `deploy/prometheus.docker.yml`. Day to day: `docker compose ps`,
`docker compose logs -f collector`, `docker compose restart collector`. `TUNNEL_TOKEN`
is not used here; add your own connector service if you need one.

## Native (no containers)

```bash
scripts/fetch-vendor.sh          # Prometheus + Grafana release tarballs, SHA-256 checked → vendor/
scripts/install-services.sh      # ~/.config/systemd/user/servmon-*.service, started now
loginctl enable-linger $USER
```

Unit names and day-to-day commands are the same as with Podman. `podman-install.sh`
replaces these units if you switch later.

## Switching between them

Stop the old setup first: two Prometheus instances can't share `data/prometheus`.
Podman ↔ native happens automatically (the installers replace each other's units). To or from
Docker, run `systemctl --user stop servmon-pod` or `docker compose down` yourself.

## Data

| Path | What | Size |
|---|---|---|
| `data/prometheus/` | all metrics (retention: `RETENTION_TIME` / `RETENTION_SIZE` in `.env`) | ≈ 20–40 MB/day for 4 servers |
| `data/grafana/` | Grafana users, preferences, edits made in the UI | small |
| `data/archive/<host>/<day>.jsonl.gz` | one raw snapshot per minute per server | ≈ 10 MB/day |

Back up by stopping the stack and copying `data/`. `data/archive` contains every user's
command lines, so treat it as private.

## Moving to another machine

Copy the repository and `data/`, then set up SSH access to the servers there (`SSH_DIR`;
see [adding-a-server.md](adding-a-server.md)). Finally run `scripts/setup.sh && scripts/podman-install.sh`.

## Publishing the repository

`.gitignore` excludes `data/` (metrics and other users' command lines), `vendor/`
(≈ 1.5 GB of binaries), `.env` (passwords, tunnel token), `deploy/grafana/grafana.env`
(auth settings, OAuth secrets) and `ssh/`. Before the first push:

```bash
git init && git add -A && git status --short     # check that only source files are listed
```

`config.toml` holds only SSH alias names, not hostnames or users. Those stay in your
`~/.ssh/config`.
