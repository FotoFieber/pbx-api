# pbx-api

A small service that runs on a FreePBX/Asterisk host and offers the phone system as
a **business interface**: calls as people experience them, extensions, external
lines and ring groups – via a REST API with an OpenAPI contract, and as an MQTT
projection with Home Assistant discovery.

It hides the CDR/AMI details: a "call" merges all its legs (ring groups, follow-me,
loopback routes, gateways that deliver one call twice), knows its direction, outcome,
the line used, who rang and who answered.

```
Home Assistant ── MQTT ──┐                PBX host
REST clients ───── HTTP ─┴─► pbx-api ──unix socket──► MariaDB (read only)
                                  └──── AMI 127.0.0.1 ──► Asterisk
```

Nothing about a concrete installation is configured in code: extensions, lines
(trunks and their numbers), DIDs and their destinations, ring groups, outbound routes
and the phone book are discovered from FreePBX at start-up, periodically and on
`SIGHUP`. A YAML file only holds connection settings and optional display overrides.

## REST API (v1)

| Endpoint | Purpose |
|---|---|
| `GET /v1/calls` | History, newest first. Filters: `from`, `to`, `direction`, `outcome`, `extension`, `line`; paging via `before` = `nextBefore` |
| `GET /v1/calls/{id}` | One call |
| `GET /v1/calls/summary` | Counts for a period (default today), `group_by=day\|line\|extension` |
| `GET /v1/calls/active` | Calls in progress (needs AMI) |
| `GET /v1/extensions[/{id}]` | Extensions with live status and registration |
| `GET /v1/lines[/{id}]` | External lines: label, own number, registration, inbound destination, routes |
| `GET /v1/ring-groups` | Ring groups |
| `GET /v1/health` | Health (public) |

The contract is [`openapi.yaml`](openapi.yaml); Swagger UI is served at `/docs`.
Authentication: static bearer tokens. Errors use RFC 9457 problem details.

MQTT topics and Home Assistant entities: [docs/mqtt.md](docs/mqtt.md).

## Requirements

* FreePBX 16/17 with Asterisk 18+ (tested with FreePBX 17 / Asterisk 22), CDR in MariaDB
* Python 3.11+ on the PBX host
* Optional: MQTT broker

## FreePBX preparation

Create a local, read-only database user (adjust the password):

```sql
CREATE USER 'pbx_api'@'localhost' IDENTIFIED BY '...';
GRANT SELECT ON asteriskcdrdb.cdr TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.users TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.devices TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.trunks TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.incoming TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.ringgroups TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.outbound_routes TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.outbound_route_trunks TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.contactmanager_group_entries TO 'pbx_api'@'localhost';
GRANT SELECT ON asterisk.contactmanager_entry_numbers TO 'pbx_api'@'localhost';
```

Create an AMI user in *Settings → Asterisk Manager Users*: permit `127.0.0.1/255.255.255.255`
only, read `system,call,reporting`, no write permissions.

## Installation

```sh
# on a development machine
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
echo 'DEPLOY_RUN="ssh root@pbx.example.org"' > deploy.local.env
PATH=.venv/bin:$PATH deploy/deploy.sh
```

`deploy/install.sh` creates the system user `pbx-api`, a virtualenv in
`/opt/pbx-api`, `/etc/pbx-api/config.yaml` (from [`config.example.yaml`](config.example.yaml))
and the systemd unit `pbx-api.service`. Put secrets into the files referenced by the
config (mode 640, group `pbx-api`), then `systemctl start pbx-api`.
`systemctl reload pbx-api` re-reads the overrides and the FreePBX configuration.

## Development

```sh
.venv/bin/pytest                      # unit, API and contract tests
.venv/bin/python scripts/export_openapi.py   # regenerate the contract after API changes
PATH=.venv/bin:$PATH scripts/check_licenses.sh  # dependency license allowlist
git config core.hooksPath .githooks   # leak check on commit/push
```

Test data is synthetic. The pre-commit hook rejects secrets and anything matching a
personal denylist kept outside the repository (`~/.config/pbx-api/denylist`, one
regex per line) – use it for your own IP ranges, numbers and names.
[gitleaks](https://github.com/gitleaks/gitleaks) is used when installed.

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party
dependencies are all under permissive licenses, listed in
[THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES).

FreePBX and Asterisk are trademarks of Sangoma Technologies; Home Assistant is a
trademark of the Open Home Foundation. This project is independent and not affiliated
with or endorsed by them.
