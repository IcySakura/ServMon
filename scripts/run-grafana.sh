#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export SERVMON_ROOT="$PWD"
export PROM_URL="${PROM_URL:-http://127.0.0.1:9390}"
# your Grafana settings (deploy/grafana/grafana.env.example)
# (KEY=value lines, taken literally like podman/docker env files)
if [[ -f deploy/grafana/grafana.env ]]; then
  while IFS= read -r line; do
    if [[ $line =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then export "$line"; fi
  done < deploy/grafana/grafana.env
fi
mkdir -p data/grafana/log data/grafana/plugins
exec vendor/grafana/bin/grafana server \
  --homepath "$PWD/vendor/grafana" \
  --config "$PWD/deploy/grafana/grafana.ini"
