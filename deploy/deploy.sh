#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build a wheel and install it on the PBX host.
#
# The target is configured in deploy.local.env (not tracked), e.g.
#   DEPLOY_RUN="ssh root@pbx.example.org"
# DEPLOY_RUN must run a shell command on the target and pass stdin through.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f deploy.local.env ] || { echo "missing deploy.local.env (see deploy/deploy.sh)" >&2; exit 1; }
# shellcheck disable=SC1091
. ./deploy.local.env
: "${DEPLOY_RUN:?set DEPLOY_RUN in deploy.local.env}"

python -m pytest -q
python scripts/export_openapi.py --check

rm -rf dist && python -m pip wheel -q --no-deps -w dist .
remote_dir=/tmp/pbx-api-deploy
tar -c dist/*.whl deploy/install.sh deploy/pbx-api.service config.example.yaml \
  | $DEPLOY_RUN "rm -rf $remote_dir && mkdir -p $remote_dir && tar -x -C $remote_dir && bash $remote_dir/deploy/install.sh $remote_dir && rm -rf $remote_dir"
