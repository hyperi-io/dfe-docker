#!/usr/bin/env bash
# STAGED installer for the dfe-docker self-update daemon. Run as root on the VM.
#
#   sudo DFE_DOCKER_DIR=/opt/dfe-docker DFE_UPDATE_USER=dfe ./install.sh
#
# Renders the service unit with the checkout path + service user, installs the
# unit + timer, and enables the timer. Idempotent: re-running re-renders and
# reloads. Requires: docker, oras, python3, and the service user in the docker
# group with a working .env (run `make init` in the checkout once first).
set -euo pipefail

DFE_DOCKER_DIR="${DFE_DOCKER_DIR:-/opt/dfe-docker}"
DFE_UPDATE_USER="${DFE_UPDATE_USER:-dfe}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system

if [[ "${EUID}" -ne 0 ]]; then
  echo "install.sh must run as root (systemd unit install)" >&2
  exit 1
fi
if [[ ! -f "${DFE_DOCKER_DIR}/Makefile" ]]; then
  echo "DFE_DOCKER_DIR=${DFE_DOCKER_DIR} is not a dfe-docker checkout (no Makefile)" >&2
  exit 1
fi

sed -e "s#__DFE_DOCKER_DIR__#${DFE_DOCKER_DIR}#g" \
    -e "s#__DFE_UPDATE_USER__#${DFE_UPDATE_USER}#g" \
    "${HERE}/dfe-docker-update.service" >"${UNIT_DIR}/dfe-docker-update.service"
install -m 0644 "${HERE}/dfe-docker-update.timer" "${UNIT_DIR}/dfe-docker-update.timer"

systemctl daemon-reload
systemctl enable --now dfe-docker-update.timer

echo "installed. Check: systemctl status dfe-docker-update.timer"
echo "dry-run once now:  sudo -u ${DFE_UPDATE_USER} DFE_DOCKER_DIR=${DFE_DOCKER_DIR} python3 ${DFE_DOCKER_DIR}/ops/daemon-update/self_update.py --dry-run"
