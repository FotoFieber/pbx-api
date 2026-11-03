# SPDX-License-Identifier: Apache-2.0
"""Configuration loaded from YAML. Secrets are read from separate files."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .access import IPNetwork, parse_entry
from .domain.topology import Overrides


def read_secret(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8").strip()


class ServerConfig(BaseModel):
    bind: str = "127.0.0.1"
    port: int = 8930
    tokens_file: str | None = Field(default=None, description="One bearer token per line")
    allowed_clients: list[str] = Field(
        default_factory=list,
        description="IP addresses and/or CIDR subnets allowed to use the API; empty = no restriction",
    )

    @field_validator("allowed_clients", mode="before")
    @classmethod
    def _validate_clients(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        out = []
        for entry in value:
            try:
                out.append(str(parse_entry(str(entry))))
            except ValueError as exc:
                raise ValueError(f"server.allowed_clients: invalid entry {entry!r}: {exc}") from None
        return out

    def client_networks(self) -> list[IPNetwork]:
        return [parse_entry(e) for e in self.allowed_clients]

    def tokens(self) -> list[str]:
        if not self.tokens_file:
            return []
        lines = Path(self.tokens_file).read_text(encoding="utf-8").splitlines()
        return [t.strip() for t in lines if t.strip() and not t.startswith("#")]


class DbConfig(BaseModel):
    unix_socket: str | None = "/run/mysqld/mysqld.sock"
    host: str = "localhost"
    port: int = 3306
    user: str = "pbx_api"
    password_file: str | None = None
    config_database: str = "asterisk"
    cdr_database: str = "asteriskcdrdb"


class FreePbxConfig(BaseModel):
    db: DbConfig = DbConfig()
    timezone: str | None = Field(default=None, description="Time zone of CDR timestamps; default: system")


class AmiConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 5038
    user: str = "pbx_api"
    secret_file: str | None = None


class MqttConfig(BaseModel):
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    user: str | None = None
    password_file: str | None = None
    tls: bool = False
    base_topic: str = "pbx"
    discovery_prefix: str = "homeassistant"
    node_id: str = "pbx"
    device_name: str = "PBX"
    recent_limit: int = 20


class DiscoveryConfig(BaseModel):
    refresh_sec: int = 300
    phonebook_match_digits: int = 9


class CallsConfig(BaseModel):
    dedup_window_sec: int = 2
    max_page_size: int = 200


class LineOverride(BaseModel):
    label: str | None = None


class ExtensionOverride(BaseModel):
    name: str | None = None


class OverridesConfig(BaseModel):
    default_line_label: str = Field(default="name", pattern="^(name|number)$")
    lines: dict[str, LineOverride] = Field(default_factory=dict)
    extensions: dict[str, ExtensionOverride] = Field(default_factory=dict)
    hide_lines: list[str] = Field(default_factory=list)
    hide_extensions: list[str] = Field(default_factory=list)

    def to_domain(self) -> Overrides:
        return Overrides(
            line_labels={k: v.label for k, v in self.lines.items() if v.label},
            extension_names={k: v.name for k, v in self.extensions.items() if v.name},
            hidden_extensions={str(x) for x in self.hide_extensions},
            hidden_lines={str(x) for x in self.hide_lines},
            default_line_label=self.default_line_label,
        )


class Config(BaseModel):
    server: ServerConfig = ServerConfig()
    freepbx: FreePbxConfig = FreePbxConfig()
    ami: AmiConfig = AmiConfig()
    mqtt: MqttConfig = MqttConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    calls: CallsConfig = CallsConfig()
    overrides: OverridesConfig = OverridesConfig()


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    # YAML keys of overrides may be numbers (extension ids); normalise to strings
    ov = data.get("overrides") or {}
    for key in ("lines", "extensions"):
        if isinstance(ov.get(key), dict):
            ov[key] = {str(k): v for k, v in ov[key].items()}
    return Config.model_validate(data)
