# SPDX-License-Identifier: Apache-2.0
import argparse
import os

from pbx_api import setup_cmd
from pbx_api.config import load_config


def make_args(tmp_path, **kw):
    p = argparse.ArgumentParser()
    setup_cmd.add_arguments(p)
    args = p.parse_args(["--etc", str(tmp_path)])
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def test_generated_config_is_valid_and_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "chown", lambda *a: None)
    s = setup_cmd.Setup(make_args(tmp_path, mqtt_host="192.0.2.10", allow_from=["192.0.2.20", "198.51.100.0/24"]))
    monkeypatch.setattr(s, "gid", lambda: os.getgid())
    s.config_dir()
    path = s.config_file()

    cfg = load_config(path)
    assert cfg.mqtt.enabled and cfg.mqtt.host == "192.0.2.10"
    assert cfg.ami.enabled and cfg.ami.secret_file == f"{tmp_path}/ami.secret"
    assert cfg.server.port == 8930
    assert cfg.server.allowed_clients == ["192.0.2.20/32", "198.51.100.0/24"]
    assert len(cfg.server.tokens()) == 1
    assert oct(path.stat().st_mode & 0o777) == "0o640"

    # second run keeps the existing file and secrets
    token = (tmp_path / "tokens").read_text()
    path.write_text(path.read_text() + "# edited\n")
    s.config_dir()
    s.config_file()
    assert path.read_text().endswith("# edited\n")
    assert (tmp_path / "tokens").read_text() == token


def test_setup_without_allow_from_writes_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "chown", lambda *a: None)
    s = setup_cmd.Setup(make_args(tmp_path))
    monkeypatch.setattr(s, "gid", lambda: os.getgid())
    s.config_dir()
    assert load_config(s.config_file()).server.allowed_clients == []


def test_setup_rejects_invalid_allow_from(tmp_path):
    s = setup_cmd.Setup(make_args(tmp_path, allow_from=["192.0.2.10/24"]))
    import pytest

    with pytest.raises(setup_cmd.SetupError, match="--allow-from"):
        s.allowed_clients()


def test_unit_template_placeholders():
    from importlib import resources

    unit = resources.files("pbx_api.resources").joinpath("pbx-api.service").read_text()
    for placeholder in ("@USER@", "@PBX_API_BIN@", "@CONFIG@"):
        assert placeholder in unit
