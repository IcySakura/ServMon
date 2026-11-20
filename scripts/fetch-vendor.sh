#!/usr/bin/env bash
# Only needed for the native (non-Docker) deployment: downloads the official
# Prometheus and Grafana linux-amd64 releases into vendor/ and checks their SHA-256.
set -euo pipefail
cd "$(dirname "$0")/.."
PV=3.16.0
GV=13.3.1
mkdir -p vendor && cd vendor
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

f=prometheus-$PV.linux-amd64.tar.gz
curl -fL --retry 3 -o "$tmp/$f" "https://github.com/prometheus/prometheus/releases/download/v$PV/$f"
curl -fsSL "https://github.com/prometheus/prometheus/releases/download/v$PV/sha256sums.txt" | grep " $f\$" > "$tmp/p.sha"
(cd "$tmp" && sha256sum -c p.sha)
rm -rf prometheus && mkdir prometheus && tar -xzf "$tmp/$f" -C prometheus --strip-components=1

f=grafana-$GV.linux-amd64.tar.gz
curl -fL --retry 3 -o "$tmp/$f" "https://dl.grafana.com/oss/release/$f"
echo "$(curl -fsSL "https://dl.grafana.com/oss/release/$f.sha256")  $f" > "$tmp/g.sha"
(cd "$tmp" && sha256sum -c g.sha)
rm -rf grafana && mkdir grafana && tar -xzf "$tmp/$f" -C grafana --strip-components=1
echo "vendor/ ready: $(prometheus/prometheus --version 2>/dev/null | head -1), grafana $GV"
