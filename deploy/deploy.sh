#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build a wheel and install it on the PBX host.
#
# The target is configured in deploy.local.env (not tracked). DEPLOY_RUN must start a
# shell on the target that reads its script from stdin, e.g.
#   DEPLOY_RUN="ssh root@pbx.example.org bash -s"
#   DEPLOY_RUN="ssh root@hypervisor.example.org pct exec 123 -- bash -s"   # container
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f deploy.local.env ] || { echo "missing deploy.local.env (see deploy/deploy.sh)" >&2; exit 1; }
# shellcheck disable=SC1091
. ./deploy.local.env
: "${DEPLOY_RUN:?set DEPLOY_RUN in deploy.local.env}"

python -m pytest -q
python scripts/export_openapi.py --check

rm -rf dist && python -m pip wheel -q --no-deps -w dist .

# self-extracting install script: bundle travels base64-encoded inside the script
{
  echo 'set -euo pipefail; d=$(mktemp -d /tmp/pbx-api-deploy.XXXXXX); trap "rm -rf $d" EXIT'
  echo 'base64 -d > "$d/bundle.tar" <<"PBX_API_BUNDLE"'
  tar -c dist/*.whl deploy/install.sh deploy/pbx-api.service config.example.yaml | base64
  echo 'PBX_API_BUNDLE'
  echo 'tar -x -C "$d" -f "$d/bundle.tar" && bash "$d/deploy/install.sh" "$d"'
} | $DEPLOY_RUN
