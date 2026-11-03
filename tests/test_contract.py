# SPDX-License-Identifier: Apache-2.0
"""The checked-in openapi.yaml is the contract; the code must match it exactly."""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _render() -> str:
    spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts" / "export_openapi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.render()


def test_contract_matches_code():
    committed = (ROOT / "openapi.yaml").read_text(encoding="utf-8")
    assert committed == _render(), "API changed: review the diff, then run scripts/export_openapi.py"


def test_contract_basics():
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    assert spec["info"]["license"]["identifier"] == "Apache-2.0"
    paths = set(spec["paths"])
    assert {"/v1/calls", "/v1/calls/{call_id}", "/v1/calls/summary", "/v1/extensions", "/v1/lines"} <= paths
    call = spec["components"]["schemas"]["Call"]["properties"]
    assert {"startedAt", "direction", "outcome", "caller", "callee", "line", "answeredBy"} <= set(call)
