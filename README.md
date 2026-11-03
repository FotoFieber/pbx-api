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
Access: client IP allow list and static bearer tokens (see [Access control](#access-control)).
Errors use RFC 9457 problem details.

MQTT topics: [docs/mqtt.md](docs/mqtt.md). Home Assistant entities, an example
dashboard and automation: [docs/home-assistant.md](docs/home-assistant.md).

## Access control

Two independent checks protect the REST API:

1. **Client IP allow list** – `server.allowed_clients` in `config.yaml`. Each entry is a
   single IP address or a subnet in CIDR notation; IPv4 and IPv6 can be mixed:

   ```yaml
   server:
     allowed_clients:
       - 192.0.2.10          # one host
       - 198.51.100.0/24     # a whole subnet
       - 2001:db8::/32       # an IPv6 subnet
   ```

   Requests from other addresses get `403` on every endpoint, including
   `/v1/health`, `/docs` and `/openapi.json`, before any token is looked at.
   Loopback (`127.0.0.0/8`, `::1`) is always allowed. An empty list means no
   restriction (a warning is logged). A subnet with host bits set (`192.0.2.10/24`)
   is rejected as a likely typo. The client address is the TCP peer;
   `X-Forwarded-For` is ignored.
2. **Bearer token** – one of the tokens in `server.tokens_file`; required for all
   endpoints except `/v1/health`.

Both are re-read on `systemctl reload pbx-api` without a restart; an invalid
configuration file is rejected and the running settings stay in place. A host
firewall (e.g. the FreePBX firewall) can add a third layer in front.

## Requirements

* FreePBX 16/17 with Asterisk 18+ (tested with FreePBX 17 / Asterisk 22), CDR in MariaDB
* Python 3.11+ on the PBX host
* Optional: MQTT broker

## Installation

pbx-api is not published on PyPI; it is installed from this Git repository. In the
commands below, `<repo-url>` is the clone URL of this repository and `v0.1.2` a release
tag (see the tags / releases page of the repository).

On the PBX host, as root:

```sh
apt install python3-venv git                 # git is preinstalled on FreePBX 17
python3 -m venv /opt/pbx-api

# pip clones the repository at the given tag and installs it into the virtualenv
/opt/pbx-api/bin/pip install "git+<repo-url>@v0.1.2"

/opt/pbx-api/bin/pbx-api setup --allow-from 192.0.2.10 --allow-from 198.51.100.0/24
                                             # add --mqtt-host <broker> to enable MQTT
/opt/pbx-api/bin/pbx-api check
systemctl start pbx-api
```

Alternatively check out the source yourself, e.g. to review it first or without
network access from the PBX host to the repository:

```sh
git clone --branch v0.1.2 <repo-url> /usr/local/src/pbx-api
/opt/pbx-api/bin/pip install /usr/local/src/pbx-api
```

The runtime dependencies (FastAPI, aiomysql, panoramisk, aiomqtt, …) are downloaded
from PyPI during installation.

`pbx-api setup` is idempotent and shows its plan with `--dry-run`. It

* creates the system user `pbx-api` and `/etc/pbx-api/` (mode 750) with generated
  secrets (`tokens`, `db.secret`, `ami.secret`; existing files are never overwritten)
  and `config.yaml` (see [`config.example.yaml`](config.example.yaml) for all options),
* creates the local database user `pbx_api@localhost` with `SELECT` on exactly the
  tables the service reads (`--skip-db` to do it yourself),
* creates the AMI user `pbx_api`: permit `127.0.0.1` only, read `system,call,reporting`,
  write `reporting` – AMI authorizes actions by the *write* class, and `reporting`
  allows only informational actions such as `DeviceStateList` and `CoreShowChannels`
  (`--skip-ami`; on plain Asterisk it prints a `manager_custom.conf` snippet instead),
* installs and enables `pbx-api.service` for the current virtualenv (`--skip-service`).

For MQTT, put the broker password into `/etc/pbx-api/mqtt.secret` (group `pbx-api`,
mode 640). Clients use a token from `/etc/pbx-api/tokens` as bearer token.

Upgrade to a newer tag (or `git pull` in your checkout and install it again):

```sh
/opt/pbx-api/bin/pip install "git+<repo-url>@v0.2.0"
/opt/pbx-api/bin/pbx-api setup                # idempotent; picks up new unit/grants
systemctl restart pbx-api
```

`systemctl reload pbx-api` re-reads the overrides and the FreePBX configuration.

## Development

```sh
.venv/bin/pytest                      # unit, API and contract tests
.venv/bin/python scripts/export_openapi.py   # regenerate the contract after API changes
PATH=.venv/bin:$PATH scripts/check_licenses.sh  # dependency license allowlist
git config core.hooksPath .githooks   # leak and docs check on commit, leak check on push
```

The architecture is documented in [ARCHITECTURE.md](ARCHITECTURE.md) (arc42).
Documentation is updated together with the code: the pre-commit hook rejects a
commit that changes `src/`, `scripts/`, `.githooks/`, `pyproject.toml` or
`config.example.yaml` without staging `ARCHITECTURE.md`, `README.md`, `docs/` or
`openapi.yaml`. For a change without documentation impact, confirm with
`PBX_API_DOCS_CHECKED=1 git commit …`.

Test data is synthetic. The pre-commit hook rejects secrets and anything matching a
personal denylist kept outside the repository (`~/.config/pbx-api/denylist`, one
regex per line) – use it for your own IP ranges, numbers and names.
[gitleaks](https://github.com/gitleaks/gitleaks) is used when installed (8.16 from the
Ubuntu archive or a current release).

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party
dependencies are all under permissive licenses, listed in
[THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES).

FreePBX and Asterisk are trademarks of Sangoma Technologies; Home Assistant is a
trademark of the Open Home Foundation. This project is independent and not affiliated
with or endorsed by them.
