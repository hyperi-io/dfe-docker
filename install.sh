#!/bin/sh
#  Project:      dfe-docker
#  File:         install.sh
#  Purpose:      Install the DFE stack on a host that has docker and curl, with no git, make or python
#  Language:     POSIX sh
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

# POSIX sh, because `curl | sh` runs on a host with no python or make.
# DFE_VERSION, DFE_PROFILE, DFE_DIR and DFE_RELEASE_URL (a file:// URL works) override the defaults below.

set -eu
umask 022

DFE_RELEASE_URL=${DFE_RELEASE_URL:-https://github.com/hyperi-io/dfe-docker/releases}
DFE_VERSION=${DFE_VERSION:-latest}
DFE_PROFILE=${DFE_PROFILE:-single-kafka}
TARGET=${DFE_DIR:-dfe}

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
else
    die "needs curl or wget to download the release, and neither is installed"
fi
if command -v sha256sum >/dev/null 2>&1; then
    sha256() { sha256sum "$1" | cut -d ' ' -f 1; }
elif command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$1" | cut -d ' ' -f 1; }
else
    die "needs sha256sum or shasum to check the download, and neither is installed"
fi
command -v docker >/dev/null 2>&1 || die "needs docker, which is not installed"
docker compose version >/dev/null 2>&1 || die "needs docker compose v2, and 'docker compose version' fails"
command -v tar >/dev/null 2>&1 || die "needs tar, which is not installed"
[ -r /dev/urandom ] || die "needs /dev/urandom to mint the secrets"
[ ! -e "$TARGET" ] || die "./$TARGET exists. Remove it, or stop the stack there: cd $TARGET && docker compose --profile '*' down"

case $DFE_VERSION in
    latest) BASE=$DFE_RELEASE_URL/latest/download ;;
    v*) BASE=$DFE_RELEASE_URL/download/$DFE_VERSION ;;
    *) BASE=$DFE_RELEASE_URL/download/v$DFE_VERSION ;;
esac

WORK=$(mktemp -d "${TMPDIR:-/tmp}/dfe-install.XXXXXX")
MADE=
# A directory this run made and could not finish is removed, so a retry is not refused for it
cleanup() {
    status=$?
    rm -rf "$WORK"
    if [ "$status" -ne 0 ] && [ -n "$MADE" ]; then
        cd / && rm -rf "$MADE"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

say "Downloading the DFE $DFE_VERSION release"
fetch "$BASE/SHA256SUMS" "$WORK/SHA256SUMS" || die "could not download $BASE/SHA256SUMS"
BUNDLE=$(sed -n 's/^[0-9a-f]\{64\}  \(dfe-docker-.*\.tar\.gz\)$/\1/p' "$WORK/SHA256SUMS" | head -n 1)
[ -n "$BUNDLE" ] || die "SHA256SUMS names no dfe-docker-*.tar.gz bundle"
WANT=$(grep "  $BUNDLE\$" "$WORK/SHA256SUMS" | cut -d ' ' -f 1)
fetch "$BASE/$BUNDLE" "$WORK/$BUNDLE" || die "could not download $BASE/$BUNDLE"
[ "$(sha256 "$WORK/$BUNDLE")" = "$WANT" ] || die "$BUNDLE does not match its sha256 in SHA256SUMS, so it was not unpacked"

mkdir "$TARGET"
case $TARGET in
    /*) MADE=$TARGET ;;
    *) MADE=$PWD/$TARGET ;;
esac
tar -xzf "$WORK/$BUNDLE" -C "$WORK"
cp -R "$WORK/dfe/." "$TARGET/"
cd "$TARGET"

if [ ! -r "install/profiles/$DFE_PROFILE.conf" ]; then
    NAMES=
    for conf in install/profiles/*.conf; do
        NAMES="$NAMES $(basename "$conf" .conf)"
    done
    die "no profile $DFE_PROFILE. Choose one of:$NAMES"
fi

# The kind and length come from install/secrets.list, which init.py's rules generate
random() {
    case $1 in
        hex) od -An -v -N "$(($2 / 2))" -tx1 /dev/urandom | tr -d ' \n' ;;
        *) LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$2" ;;
    esac
}

set_key() {
    if grep -q "^#\{0,1\} \{0,1\}$2=" "$1"; then
        sed "s|^#\{0,1\} \{0,1\}$2=.*|$2=$3|" "$1" >"$1.new" && mv "$1.new" "$1"
    else
        printf '%s=%s\n' "$2" "$3" >>"$1"
    fi
}

umask 027
cp .env.example .env
mkdir env
for template in env.example/*.env; do
    cp "$template" "env/$(basename "$template")"
done
while read -r file key kind length; do
    case $file in
        .env) set_key .env "$key" "$(random "$kind" "$length")" ;;
        *) set_key "env/$file" "$key" "$(random "$kind" "$length")" ;;
    esac
done <install/secrets.list
# dfe-engine writes env/<app>.custom.env here, as the group compose adds it to
chmod g+ws env

{
    printf '\n## Written by install.sh\n'
    cat install/stack.env
    cat "install/profiles/$DFE_PROFILE.env"
    printf 'DFE_PROFILE=%s\n' "$DFE_PROFILE"
    printf 'DFE_DEV_UID=%s\nDFE_DEV_GID=%s\n' "$(id -u)" "$(id -g)"
    printf 'DFE_UI_BIND_HOST=127.0.0.1\n'
} >>.env
# shellcheck disable=SC1090
. "./install/profiles/$DFE_PROFILE.conf"
# Compose reads these two from .env, so a plain `docker compose` in ./dfe runs this stack
FILES=docker-compose.yml
if grep -q '^DFE_OTEL_RESOLVED=true$' "install/profiles/$DFE_PROFILE.env"; then
    FILES=$FILES:compose/container-logs.yml
fi
printf 'COMPOSE_FILE=%s\nCOMPOSE_PROFILES=%s\n' "$FILES" "$PROFILES" >>.env

# From here the directory holds the compose project, which a failed start leaves for `docker compose down`
MADE=
say "Starting the $DFE_PROFILE stack, which pulls its images first"
# SERVICES is a list of service names, so it has to split
# shellcheck disable=SC2086
docker compose up -d --wait --wait-timeout 900 $SERVICES

ADMIN=$(sed -n 's/^DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD=//p' env/engine.env | head -n 1)
say ""
say "DFE is up. The console is at http://localhost:3000"
say "  login     admin"
if [ -t 1 ]; then
    say "  password  $ADMIN"
else
    say "  password  DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD in $PWD/env/engine.env"
fi
say "  stop      cd \"$PWD\" && docker compose --profile '*' down"
say "  remove    cd \"$PWD\" && docker compose --profile '*' down -v"
say "The admin password is issued with a forced change at first login."
