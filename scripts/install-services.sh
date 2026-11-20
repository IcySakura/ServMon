#!/usr/bin/env bash
# Installs and starts servmon as systemd *user* services (no root needed).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
# replaces a Podman install (same unit names)
if [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd/servmon.pod" ]]; then
  "$ROOT/scripts/podman-install.sh" uninstall
fi
mkdir -p "$UNIT_DIR"
for svc in collector prometheus grafana; do
  cat > "$UNIT_DIR/servmon-$svc.service" <<UNIT
[Unit]
Description=servmon $svc
After=network-online.target
PartOf=servmon.target

[Service]
ExecStart=$ROOT/scripts/run-$svc.sh
Restart=always
RestartSec=10

[Install]
WantedBy=servmon.target
UNIT
done
cat > "$UNIT_DIR/servmon.target" <<UNIT
[Unit]
Description=servmon (collector + prometheus + grafana)
Wants=servmon-collector.service servmon-prometheus.service servmon-grafana.service

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now servmon.target servmon-collector.service servmon-prometheus.service servmon-grafana.service
echo
echo "Started. Grafana: http://127.0.0.1:3300   Prometheus: http://127.0.0.1:9390"
echo "To keep collecting while you are logged out:  loginctl enable-linger $USER"
