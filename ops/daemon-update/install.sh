#!/usr/bin/env bash
#
# Project:   dfe-docker
# File:      ops/daemon-update/install.sh
# Purpose:   Install the self-update units on a VM, as root. Re-running is safe
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# DFE_DOCKER_DIR              The checkout (default: /opt/dfe-docker)
# DFE_OPERATOR_USER           A login to add to the group (default: none)
# DFE_UPDATE_ENABLE_TIMER     0 leaves the timer as it is
# DFE_UPDATE_GROUP            The sharing group (default: the user's primary)
# DFE_UPDATE_USER             The service user (default: dfe)

set -euo pipefail

: "${DFE_DOCKER_DIR:=/opt/dfe-docker}"
: "${DFE_OPERATOR_USER:=}"
: "${DFE_UPDATE_ENABLE_TIMER:=1}"
: "${DFE_UPDATE_USER:=dfe}"
: "${DFE_UPDATE_GROUP:=$(id -gn "${DFE_UPDATE_USER}")}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly HERE
readonly POLKIT_DIR=/etc/polkit-1/rules.d
readonly UNIT_DIR=/etc/systemd/system

die() {
  echo "${1}" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "install.sh must run as root"
[[ -f "${DFE_DOCKER_DIR}/Makefile" ]] || die "${DFE_DOCKER_DIR} has no Makefile, so it is no checkout"

sed -e "s#__DFE_DOCKER_DIR__#${DFE_DOCKER_DIR}#g" -e "s#__DFE_UPDATE_USER__#${DFE_UPDATE_USER}#g" \
  "${HERE}/dfe-docker-update.service" >"${UNIT_DIR}/dfe-docker-update.service"
install -m 0644 "${HERE}/dfe-docker-update.timer" "${UNIT_DIR}/dfe-docker-update.timer"

# The proof unit ships elsewhere, but its files need the same umask
if [[ -f "${UNIT_DIR}/dfe-docker-proof.service" ]]; then
  mkdir -p "${UNIT_DIR}/dfe-docker-proof.service.d"
  printf '[Service]\nUMask=0002\n' >"${UNIT_DIR}/dfe-docker-proof.service.d/20-shared-checkout.conf"
fi

if [[ -d "${POLKIT_DIR}" ]]; then
  sed -e "s#__DFE_GROUP__#${DFE_UPDATE_GROUP}#g" "${HERE}/dfe-docker-update.rules" >"${POLKIT_DIR}/50-dfe-docker-update.rules"
  chmod 0644 "${POLKIT_DIR}/50-dfe-docker-update.rules"
else
  echo "no ${POLKIT_DIR}: members of ${DFE_UPDATE_GROUP} will need sudo to start the units" >&2
fi

# Leaves certs/ alone: scripts/proxy_tls.py keeps console.key 0600 for its owner
tree=(find "${DFE_DOCKER_DIR}" -path "${DFE_DOCKER_DIR}/certs" -prune -o)
"${tree[@]}" ! -type l -exec chgrp "${DFE_UPDATE_GROUP}" {} +
"${tree[@]}" -type d -exec chmod g+rwxs {} +
"${tree[@]}" -type f -exec chmod g+rwX {} +
find "${DFE_DOCKER_DIR}" -type f ! -name .env.example \( -path "${DFE_DOCKER_DIR}/.env*" -o -path "${DFE_DOCKER_DIR}/access-summary.md" -o -path "${DFE_DOCKER_DIR}/env/*" \) -exec chmod o-rwx {} +

# A tree copied without its git history has no .git
if [[ -e "${DFE_DOCKER_DIR}/.git" ]]; then
  git config --system --fixed-value --replace-all safe.directory "${DFE_DOCKER_DIR}" "${DFE_DOCKER_DIR}"
  runuser -u "${DFE_UPDATE_USER}" -- git -C "${DFE_DOCKER_DIR}" config core.sharedRepository group
fi

if [[ -n "${DFE_OPERATOR_USER}" ]]; then
  usermod -aG "${DFE_UPDATE_GROUP}" "${DFE_OPERATOR_USER}"
fi

systemctl daemon-reload
if [[ "${DFE_UPDATE_ENABLE_TIMER}" != "0" ]]; then
  systemctl enable --now dfe-docker-update.timer
fi

echo "installed. Dry-run: sudo -u ${DFE_UPDATE_USER} python3 ${DFE_DOCKER_DIR}/ops/daemon-update/self_update.py --dry-run"
