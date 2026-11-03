# Home Assistant

pbx-api announces its entities through MQTT discovery, so Home Assistant only needs
the MQTT integration connected to the same broker. No custom integration, YAML
sensors or polling are required.

## Entities

With the default `mqtt.device_name: PBX`, Home Assistant creates one device "PBX"
with these entities (the object id after `pbx_` comes from the extension or line
name):

| Entity | State | Attributes |
|---|---|---|
| `sensor.pbx_<extension>` | `idle`, `ringing`, `in_call`, `on_hold`, `busy`, `unavailable`, `unknown` | the `Extension` object: `id`, `name`, `technology`, `registered`, `currentCall` |
| `binary_sensor.pbx_<extension>_registered` | `on` / `off` | – |
| `binary_sensor.pbx_line_<label>` | `on` / `off` (registered) | the `Line` object |
| `sensor.pbx_calls_today` | number of calls today | totals: `inbound`, `outbound`, `internal`, `answered`, `missedInbound`, `talkSec` |
| `sensor.pbx_missed_calls_today` | missed inbound calls today | – |
| `sensor.pbx_recent_calls` | number of calls in the list | `calls`: newest `mqtt.recent_limit` `Call` objects |
| `sensor.pbx_active_calls` | calls in progress | `calls`: list of `ActiveCall` |
| `event.pbx_call` | time of the last event | `event_type` (`ringing`, `answered`, `ended`, `missed`), `call` |

Payload fields are camelCase as in the REST API ([`openapi.yaml`](../openapi.yaml)).
Fields without a value are omitted, e.g. `caller.name` for unknown numbers or
`answeredBy` for unanswered calls.

## Example dashboard

[`examples/home-assistant-dashboard.yaml`](examples/home-assistant-dashboard.yaml) is a
complete sections dashboard with only built-in cards:

* **Live** – every extension with its status, plus a card that appears while calls
  are in progress (caller → callee, line, ringing extensions).
* **Today** – calls and missed calls today, last call event.
* **Status** – pbx-api online, registration of extensions and lines.
* **Calls** – the call list: direction/outcome icon, time, from, to, line, duration.

Extensions and lines are discovered from the entities, so the dashboard needs no
changes when the PBX configuration changes. To install it, create a new dashboard,
open *Edit dashboard → Raw configuration editor* and paste the file. If you use
another `mqtt.device_name`, replace the `pbx_` prefix accordingly.

```
Live                         Today                     Status
┌─────────────────────────┐  ┌───────────┬──────────┐  ┌──────────────────────────┐
│ ☎ Reception (100)  idle │  │ Calls 5   │ Missed 1 │  │ ✔ pbx-api        online  │
│ ☎ Office (101)  in call │  ├───────────┴──────────┤  │ ✔ Line Main  registered  │
└─────────────────────────┘  │ Last event: ended    │  └──────────────────────────┘
                             └──────────────────────┘
Calls
┌──────────────────────────────────────────────────────────────────────────────┐
│    Time              From         To          Line        Duration           │
│ ↙  2030-01-10 11:00  +1 555 0199  Office      Main line       0:15           │
│ ✖  2030-01-10 12:00  +1 555 0199  Reception   Main line     missed           │
│ ↗  2030-01-10 14:00  Office       +1 555 0188 Provider        0:33           │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Writing your own templates

Home Assistant renders markdown-card templates in **strict** mode in the browser: an
attribute access on a missing field (`call.caller.name`) aborts the whole card and it
stays empty. Read optional fields with `.get()`:

```jinja
{{ call.get('caller', {}).get('name') or call.get('caller', {}).get('number') }}
{{ call.get('line', {}).get('label', '–') }}
```

Test templates in *Developer tools → Template* – the editor there is not strict, so
also check the rendered dashboard.

## Example automation: missed calls

[`examples/home-assistant-missed-call.yaml`](examples/home-assistant-missed-call.yaml)
sends a notification for every missed inbound call, using the `missed` event of
`event.pbx_call`. Other ideas with the same event entity: pause media players on
`ringing`, flash a light for calls on a specific line (`call.line.id`), or log calls
to a to-do list.

## REST from Home Assistant

Live state and the call list come via MQTT. For history queries or summaries over
arbitrary periods, call the REST API, e.g. with a RESTful sensor; the client IP of
Home Assistant must be in `server.allowed_clients` and a token from
`server.tokens_file` is required:

```yaml
sensor:
  - platform: rest
    name: PBX calls this month
    resource: http://pbx.example.org:8930/v1/calls/summary?from=2030-01-01T00:00:00Z
    headers:
      Authorization: !secret pbx_api_bearer   # "Bearer <token>"
    value_template: "{{ value_json.totals.total }}"
    scan_interval: 3600
```
