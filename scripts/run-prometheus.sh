#!/usr/bin/env bash
# Retention: 2 years or 40 GB, whichever comes first (~25-40 MB/day for 4 servers).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/prometheus
exec vendor/prometheus/prometheus \
  --config.file=deploy/prometheus.yml \
  --storage.tsdb.path=data/prometheus \
  --storage.tsdb.retention.time=2y \
  --storage.tsdb.retention.size=40GB \
  --web.listen-address=127.0.0.1:9390 \
  --web.enable-admin-api \
  --query.timeout=2m
