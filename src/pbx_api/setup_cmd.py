# SPDX-License-Identifier: Apache-2.0
"""``pbx-api setup``: prepare the PBX host for the service. Idempotent.

Runs on the PBX host as root. Steps (each can be skipped):

1. system user for the service
2. configuration directory, secrets (generated, never overwritten) and config file
3. local read-only database user with SELECT on the tables the service reads
4. AMI user restricted to 127.0.0.1 (FreePBX: via its manager table + reload)
5. systemd unit
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import secrets
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .adapters.freepbx_db import READ_TABLES

AMI_READ = "system,call,reporting"
AMI_WRITE = "reporting"  # informational actions only (DeviceStateList, CoreShowChannels)


class SetupError(RuntimeError):
    pass


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--etc", default="/etc/pbx-api", help="configuration directory")
    p.add_argument("--user", default="pbx-api", help="system user running the service")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8930)
    p.add_argument("--db-socket", default="/run/mysqld/mysqld.sock")
    p.add_argument("--db-user", default="pbx_api")
    p.add_argument("--config-database", default="asterisk")
    p.add_argument("--cdr-database", default="asteriskcdrdb")
    p.add_argument("--ami-user", default="pbx_api")
    p.add_argument("--mqtt-host", help="enable MQTT with this broker (only used when creating the config)")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--mqtt-user", default="pbx-api")
    p.add_argument("--skip-db", action="store_true", help="do not create/update the database user")
    p.add_argument("--skip-ami", action="store_true", help="do not create/update the AMI user")
    p.add_argument("--skip-service", action="store_true", help="do not install the systemd unit")
    p.add_argument("--dry-run", action="store_true", help="only show what would be done")


class Setup:
    def __init__(self, args: argparse.Namespace) -> None:
        self.a = args
        self.etc = Path(args.etc)
        self.dry = args.dry_run

    # ---- helpers ------------------------------------------------------------------

    def say(self, msg: str) -> None:
        print(("[dry-run] " if self.dry else "") + msg)

    def run(self, cmd: list[str], stdin: str | None = None) -> str:
        if self.dry:
            self.say("would run: " + " ".join(cmd))
            return ""
        res = subprocess.run(cmd, input=stdin, text=True, capture_output=True, check=False)
        if res.returncode != 0:
            raise SetupError(f"{' '.join(cmd)} failed: {res.stderr.strip() or res.stdout.strip()}")
        return res.stdout

    def mysql(self, sql: str) -> str:
        return self.run(["mysql", "--batch", "--skip-column-names", f"--socket={self.a.db_socket}"], stdin=sql)

    def gid(self) -> int:
        return grp.getgrnam(self.a.user).gr_gid if not self.dry else 0

    def secret(self, name: str, factory=lambda: secrets.token_urlsafe(24)) -> str:
        path = self.etc / name
        if path.exists():
            return path.read_text(encoding="utf-8").splitlines()[0].strip()
        value = factory()
        self.say(f"create secret {path}")
        if not self.dry:
            path.write_text(value + "\n", encoding="utf-8")
            os.chown(path, 0, self.gid())
            path.chmod(0o640)
        return value

    # ---- steps --------------------------------------------------------------------

    def system_user(self) -> None:
        try:
            pwd.getpwnam(self.a.user)
        except KeyError:
            self.say(f"create system user {self.a.user}")
            self.run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", self.a.user])

    def config_dir(self) -> None:
        if not self.etc.exists():
            self.say(f"create {self.etc}")
            if not self.dry:
                self.etc.mkdir(mode=0o750, parents=True)
        if not self.dry:
            os.chown(self.etc, 0, self.gid())
            self.etc.chmod(0o750)
        self.secret("tokens", lambda: secrets.token_hex(24))

    def config_file(self) -> Path:
        path = self.etc / "config.yaml"
        if path.exists():
            self.say(f"keep existing {path}")
            return path
        mqtt = bool(self.a.mqtt_host)
        text = resources.files("pbx_api.resources").joinpath("config.template.yaml").read_text(encoding="utf-8")
        values = {
            "@BIND@": self.a.bind,
            "@PORT@": str(self.a.port),
            "@ETC@": str(self.etc),
            "@DB_SOCKET@": self.a.db_socket,
            "@DB_USER@": self.a.db_user,
            "@AMI_ENABLED@": "false" if self.a.skip_ami else "true",
            "@AMI_USER@": self.a.ami_user,
            "@MQTT_ENABLED@": "true" if mqtt else "false",
            "@MQTT_HOST@": self.a.mqtt_host or "localhost",
            "@MQTT_PORT@": str(self.a.mqtt_port),
            "@MQTT_USER@": self.a.mqtt_user,
        }
        for key, value in values.items():
            text = text.replace(key, value)
        self.say(f"create {path}")
        if not self.dry:
            path.write_text(text, encoding="utf-8")
            os.chown(path, 0, self.gid())
            path.chmod(0o640)
        if mqtt and not (self.etc / "mqtt.secret").exists():
            print(f"  -> put the MQTT password for '{self.a.mqtt_user}' into {self.etc / 'mqtt.secret'}")
        return path

    def database_user(self) -> None:
        password = self.secret("db.secret")
        user = f"'{self.a.db_user}'@'localhost'"
        dbs = {"config": self.a.config_database, "cdr": self.a.cdr_database}
        wanted = [(dbs[role], t) for role, tables in READ_TABLES.items() for t in tables]
        existing = set()
        if not self.dry:
            out = self.mysql(
                "SELECT CONCAT(table_schema, '.', table_name) FROM information_schema.tables WHERE "
                + " OR ".join(f"(table_schema = '{d}' AND table_name = '{t}')" for d, t in wanted)
            )
            existing = set(out.split())
        sql = [
            f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY '{password}';",
            f"ALTER USER {user} IDENTIFIED BY '{password}';",
        ]
        for d, t in wanted:
            if self.dry or f"{d}.{t}" in existing:
                sql.append(f"GRANT SELECT ON `{d}`.`{t}` TO {user};")
            else:
                print(f"  note: table {d}.{t} not present (module not installed?) - skipped")
        self.say(f"database user {self.a.db_user}@localhost with SELECT on {len(sql) - 2} tables")
        self.mysql("\n".join(sql) + "\n")

    def ami_user(self) -> None:
        secret = self.secret("ami.secret")
        db = self.a.config_database
        has_table = self.dry or self.mysql(
            f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{db}' AND table_name='manager'"
        ).strip() == "1"
        if not has_table or not shutil.which("fwconsole"):
            print(
                "  FreePBX manager table not found. Create the AMI user yourself, e.g. in "
                "manager_custom.conf:\n"
                f"  [{self.a.ami_user}]\n  secret = <content of {self.etc / 'ami.secret'}>\n"
                "  deny = 0.0.0.0/0.0.0.0\n  permit = 127.0.0.1/255.255.255.255\n"
                f"  read = {AMI_READ}\n  write = {AMI_WRITE}"
            )
            return
        name = self.a.ami_user
        self.say(f"AMI user {name} (permit 127.0.0.1, read {AMI_READ}, write {AMI_WRITE})")
        self.mysql(
            f"DELETE FROM `{db}`.manager WHERE name = '{name}';\n"
            f"INSERT INTO `{db}`.manager (name, secret, deny, permit, `read`, `write`, writetimeout) "
            f"VALUES ('{name}', '{secret}', '0.0.0.0/0.0.0.0', '127.0.0.1/255.255.255.255', "
            f"'{AMI_READ}', '{AMI_WRITE}', 5000);\n"
        )
        self.run(["fwconsole", "reload"])
        if shutil.which("asterisk"):
            self.run(["asterisk", "-rx", "manager reload"])

    def systemd_unit(self, config: Path) -> None:
        exe = Path(sys.prefix) / "bin" / "pbx-api"
        unit = resources.files("pbx_api.resources").joinpath("pbx-api.service").read_text(encoding="utf-8")
        unit = unit.replace("@USER@", self.a.user).replace("@PBX_API_BIN@", str(exe)).replace("@CONFIG@", str(config))
        path = Path("/etc/systemd/system/pbx-api.service")
        self.say(f"install {path} (ExecStart {exe})")
        if not self.dry:
            path.write_text(unit, encoding="utf-8")
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable", "pbx-api.service"])

    def __call__(self) -> int:
        if os.geteuid() != 0 and not self.dry:
            raise SetupError("setup must run as root (or use --dry-run)")
        self.system_user()
        self.config_dir()
        config = self.config_file()
        if not self.a.skip_db:
            self.database_user()
        if not self.a.skip_ami:
            self.ami_user()
        if not self.a.skip_service:
            self.systemd_unit(config)
        print(
            "\nDone. Next steps:\n"
            f"  review {config}\n"
            "  pbx-api check --config " + str(config) + "\n"
            "  systemctl restart pbx-api\n"
            f"  API token for clients: {self.etc / 'tokens'}"
        )
        return 0


def main(args: argparse.Namespace) -> int:
    try:
        return Setup(args)()
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
