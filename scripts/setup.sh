#!/usr/bin/env bash
# One-time setup: creates .env (from .env.example, with your uid/gid) and the data
# directories, so they are owned by you rather than by root.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  sed -e "s/^SERVMON_UID=.*/SERVMON_UID=$(id -u)/" -e "s/^SERVMON_GID=.*/SERVMON_GID=$(id -g)/" -e "s|^SSH_DIR=.*|SSH_DIR=$HOME/.ssh|" .env.example > .env
  chmod 600 .env
  echo "wrote .env (uid $(id -u), gid $(id -g))"
fi
set -a; . ./.env; set +a
DATA="${SERVMON_DATA:-./data}"
mkdir -p "$DATA/prometheus" "$DATA/grafana" "$DATA/archive"
echo "data directory: $DATA"
touch deploy/grafana/grafana.env
echo "next: scripts/podman-install.sh   (Docker: docker compose up -d --build)"
