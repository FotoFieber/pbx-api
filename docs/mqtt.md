# MQTT interface

pbx-api publishes the same domain objects as the REST API (camelCase JSON, `null`
fields omitted). `<base>` is `mqtt.base_topic` (default `pbx`).

| Topic | Retained | Payload |
|---|---|---|
| `<base>/status` | yes | `online` / `offline` (last will) |
| `<base>/extension/<id>/state` | yes | `Extension` incl. `status`, `registered`, `currentCall` |
| `<base>/line/<id>/state` | yes | `Line` incl. `registered` |
| `<base>/calls/active` | yes | `{"count": n, "calls": [ActiveCall]}` |
| `<base>/calls/recent` | yes | `{"count": n, "calls": [Call]}` – newest `mqtt.recent_limit` calls |
| `<base>/calls/summary/today` | yes | `CallSummary` for the current day |
| `<base>/events/call` | no | `{"event_type": "ringing" \| "answered" \| "ended" \| "missed", "call": ActiveCall \| Call}` |

`ringing` and `answered` carry an `ActiveCall`; `ended` and `missed` carry the
finished `Call`, published once its CDR has been written. Schemas: see
`components.schemas` in [`openapi.yaml`](../openapi.yaml).

## Home Assistant discovery

With `mqtt.enabled`, entities are announced under `<discovery_prefix>` (default
`homeassistant`) and grouped into one device (`mqtt.device_name`):

| Entity | Source |
|---|---|
| `sensor` per extension (enum: idle, ringing, in_call, on_hold, busy, unavailable, unknown), attributes = `Extension` | extension state |
| `binary_sensor` per extension and per line: registered (diagnostic) | extension/line state |
| `sensor` Calls today, Missed calls today | summary |
| `sensor` Recent calls (state = count, attribute `calls`) | recent |
| `sensor` Active calls (state = count, attribute `calls`) | active |
| `event` Call (event types ringing, answered, ended, missed) | events |

Entities of extensions or lines that disappear from the PBX configuration are
removed automatically on the next discovery refresh.
