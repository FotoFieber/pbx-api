#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Install or upgrade pbx-api on the PBX host. Run as root:
#   install.sh <directory containing pbx_api-*.whl and deploy/>
# Idempotent: keeps an existing /etc/pbx-api/config.yaml and secrets.
set -euo pipefail

src=${1:?usage: install.sh <bundle dir>}
prefix=/opt/pbx-api
etc=/etc/pbx-api

if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  apt-get update -qq && apt-get install -y -qq python3-venv
fi

id pbx-api >/dev/null 2>&1 || useradd --system --home-dir "$prefix" --shell /usr/sbin/nologin pbx-api

install -d -m 755 "$prefix"
[ -x "$prefix/venv/bin/python" ] || python3 -m venv "$prefix/venv"
"$prefix/venv/bin/pip" install -q --upgrade pip
wheel=$(ls -1t "$src"/dist/pbx_api-*.whl | head -1)
"$prefix/venv/bin/pip" install -q --upgrade "$wheel"

install -d -m 750 -o root -g pbx-api "$etc"
if [ ! -f "$etc/config.yaml" ]; then
  install -m 640 -o root -g pbx-api "$src/config.example.yaml" "$etc/config.yaml"
  echo "created $etc/config.yaml from example - edit it before starting"
fi

install -m 644 "$src/deploy/pbx-api.service" /etc/systemd/system/pbx-api.service
systemctl daemon-reload
systemctl enable pbx-api.service >/dev/null
if systemctl is-active -q pbx-api.service; then
  systemctl restart pbx-api.service
fi
echo "installed $(basename "$wheel")"
