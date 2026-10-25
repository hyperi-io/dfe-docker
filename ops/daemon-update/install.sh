#!/usr/bin/env bash
# STAGED installer for the dfe-docker self-update daemon. Run as root on the VM.
#
#   sudo DFE_DOCKER_DIR=/opt/dfe-docker DFE_UPDATE_USER=dfe ./install.sh
#
# Renders the service unit with the checkout path + service user, installs the
# unit + timer, and enables the timer. Idempotent: re-running re-renders and
# reloads. Requires: docker, oras, python3, and the service user in the docker
# group with a working .env (run `make init` in the checkout once first).
#
# Also sets the box up so an operator in the service user's group works without
# sudo: a polkit rule for starting the units, and share_checkout.py for the files.
#
#   DFE_OPERATOR_USER=ubuntu   add this login to the group (default: none)
#   DFE_UPDATE_ENABLE_TIMER=0  install everything but leave the timer as it is
#   DFE_UPDATE_SHARE_CHECKOUT=0  skip the checkout conversion
set -euo pipefail

DFE_DOCKER_DIR="${DFE_DOCKER_DIR:-/opt/dfe-docker}"
DFE_UPDATE_USER="${DFE_UPDATE_USER:-dfe}"
DFE_UPDATE_GROUP="${DFE_UPDATE_GROUP:-$(id -gn "${DFE_UPDATE_USER}")}"
DFE_UPDATE_ENABLE_TIMER="${DFE_UPDATE_ENABLE_TIMER:-1}"
DFE_UPDATE_SHARE_CHECKOUT="${DFE_UPDATE_SHARE_CHECKOUT:-1}"
DFE_OPERATOR_USER="${DFE_OPERATOR_USER:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system
POLKIT_DIR=/etc/polkit-1/rules.d

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

# The proof unit is not shipped here, but when the box has one its files need the same umask.
if [[ -f "${UNIT_DIR}/dfe-docker-proof.service" ]]; then
  mkdir -p "${UNIT_DIR}/dfe-docker-proof.service.d"
  printf '[Service]\nUMask=0002\n' >"${UNIT_DIR}/dfe-docker-proof.service.d/20-shared-checkout.conf"
fi

if [[ -d "${POLKIT_DIR}" ]]; then
  sed -e "s#__DFE_GROUP__#${DFE_UPDATE_GROUP}#g" \
    "${HERE}/dfe-docker-update.rules" >"${POLKIT_DIR}/50-dfe-docker-update.rules"
  chmod 0644 "${POLKIT_DIR}/50-dfe-docker-update.rules"
else
  echo "no ${POLKIT_DIR}: members of ${DFE_UPDATE_GROUP} will need sudo to start the units" >&2
fi

if [[ "${DFE_UPDATE_SHARE_CHECKOUT}" != "0" ]]; then
  share_args=(--repo-dir "${DFE_DOCKER_DIR}" --group "${DFE_UPDATE_GROUP}")
  if [[ -n "${DFE_OPERATOR_USER}" ]]; then
    share_args+=(--operator "${DFE_OPERATOR_USER}")
  fi
  python3 "${HERE}/share_checkout.py" "${share_args[@]}"
fi

systemctl daemon-reload
if [[ "${DFE_UPDATE_ENABLE_TIMER}" != "0" ]]; then
  systemctl enable --now dfe-docker-update.timer
fi

echo "installed. Check: systemctl status dfe-docker-update.timer"
echo "start an update without sudo, as a member of ${DFE_UPDATE_GROUP}:  systemctl start dfe-docker-update.service"
echo "dry-run once now:  sudo -u ${DFE_UPDATE_USER} DFE_DOCKER_DIR=${DFE_DOCKER_DIR} python3 ${DFE_DOCKER_DIR}/ops/daemon-update/self_update.py --dry-run"
