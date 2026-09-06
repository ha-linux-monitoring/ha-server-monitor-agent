#!/bin/bash
# Usage: ./publish.sh <user>@<host> [repo-path]
#
# Builds ha-server-monitor-agent on the target host from this project's
# GitHub source (not from this local checkout) and publishes the result
# into a custom local pacman repository on that host. Does NOT install
# anything -- installing/upgrading is a separate, standard `pacman -S` /
# `pacman -Syu` step once the repo is registered in /etc/pacman.conf (see
# README.md).
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: $0 <user>@<host> [repo-path]" >&2
    exit 1
fi

TARGET="$1"
REPO_PATH="${2:-/srv/pacman-repo/ha-linux-monitoring}"
REPO_NAME="ha-linux-monitoring"
SRC_URL="https://github.com/ha-linux-monitoring/ha-server-monitor-agent.git"

ssh "$TARGET" bash -s -- "$REPO_PATH" "$REPO_NAME" "$SRC_URL" <<'REMOTE'
set -euo pipefail
repo_path="$1"
repo_name="$2"
src_url="$3"
src_dir="$HOME/ha-server-monitor-agent-src"

if [ -d "$src_dir/.git" ]; then
    git -C "$src_dir" fetch origin
    git -C "$src_dir" reset --hard origin/main
else
    git clone "$src_url" "$src_dir"
fi

cd "$src_dir"
makepkg -f

pkgfile="$(makepkg --packagelist)"
mkdir -p "$repo_path"
cp -f "$pkgfile" "$repo_path/"
repo-add "$repo_path/$repo_name.db.tar.gz" "$repo_path/$(basename "$pkgfile")"
REMOTE

cat <<EOF

Built and published to $TARGET:$REPO_PATH (repo '$REPO_NAME').

First time only -- register the repo in /etc/pacman.conf:

    [ha-linux-monitoring]
    SigLevel = Optional TrustAll
    Server = file://$REPO_PATH

then 'sudo pacman -Sy'. After that, install/upgrade is plain pacman:

    sudo pacman -S ha-server-monitor-agent   # first install
    sudo pacman -Syu                          # future upgrades
EOF
