#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Pre-commit: a commit that changes code or configuration must also update the
# documentation (ARCHITECTURE.md, README.md, docs/ or openapi.yaml).
#
# If a change really has no documentation impact, confirm it explicitly:
#   PBX_API_DOCS_CHECKED=1 git commit ...
set -euo pipefail

[ "${PBX_API_DOCS_CHECKED:-}" = "1" ] && exit 0

staged=$(git diff --cached --name-only --diff-filter=ACMRD)
code=$(grep -E '^(src/|scripts/|\.githooks/|pyproject\.toml$|config\.example\.yaml$)' <<< "$staged" || true)
docs=$(grep -E '^(ARCHITECTURE\.md|README\.md|docs/|openapi\.yaml)' <<< "$staged" || true)

if [ -n "$code" ] && [ -z "$docs" ]; then
  {
    echo "docs-check: code or configuration changed, but no documentation is staged:"
    sed 's/^/  /' <<< "$code"
    echo
    echo "Update ARCHITECTURE.md, README.md, docs/ or openapi.yaml as needed and stage it."
    echo "If the change has no documentation impact: PBX_API_DOCS_CHECKED=1 git commit ..."
  } >&2
  exit 1
fi
