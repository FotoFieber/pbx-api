#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Fail if any runtime dependency is not under a permissive license, and refresh
# THIRD_PARTY_LICENSES. Run inside the project virtualenv.
set -euo pipefail
cd "$(dirname "$0")/.."

ALLOW="MIT;MIT License;BSD License;BSD-2-Clause;BSD-3-Clause;3-Clause BSD License;Apache-2.0;Apache Software License;Apache License 2.0;ISC;ISC License (ISCL);PSF-2.0;Python Software Foundation License;EPL-2.0 OR BSD-3-Clause"

# runtime dependency closure of pbx-api (excluding pbx-api itself)
mapfile -t PKGS < <(python - <<'PY'
import importlib.metadata as md, re
seen, todo = set(), ["pbx-api"]
while todo:
    name = todo.pop()
    key = re.sub(r"[-_.]+", "-", name).lower()
    if key in seen:
        continue
    seen.add(key)
    try:
        reqs = md.requires(name) or []
    except md.PackageNotFoundError:
        continue
    for r in reqs:
        if "extra ==" in r:
            continue
        todo.append(re.split(r"[ ;<>=!~\[(]", r, maxsplit=1)[0])
seen.discard("pbx-api")
print("\n".join(sorted(seen)))
PY
)

pip-licenses --packages "${PKGS[@]}" --allow-only="$ALLOW" --partial-match >/dev/null
pip-licenses --packages "${PKGS[@]}" --format=plain-vertical --with-license-file --no-license-path \
  --with-urls > THIRD_PARTY_LICENSES
echo "OK: ${#PKGS[@]} runtime dependencies, all permissive. THIRD_PARTY_LICENSES updated."
