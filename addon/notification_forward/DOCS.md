# iPhone Notification Forward

Acts as the Bluetooth "accessory" for Apple's iPhone notification forwarding
and turns every forwarded notification into a Home Assistant event and sensor.

See the repository README for the full setup (Bluetooth dongle passthrough,
webhook automation, iPhone app).

## Options

| Option | Default | Meaning |
|---|---|---|
| `adapter` | `hci0` | Bluetooth adapter to advertise on |
| `device_name` | `NotifFwd` | Advertised name (keep it short, max ~8 chars) |
| `event_type` | `iphone_notification` | Event fired per notification |
| `sensor_entity` | `sensor.iphone_last_notification` | Sensor updated with the latest notification |
| `inbound_event_type` | `notification_forward_inbound` | Event your webhook automation fires for internet deliveries |
| `log_level` | `info` | `debug`, `info`, `warning`, `error` |
| `hci_trace` | `false` | Low-level Bluetooth pairing trace (like `btmon`). Also needs `host_network: true` and `privileged: [NET_RAW]` in `config.yaml`, which are off by default |
| `summaries` | Zabbix example | Alert summaries over the last 7 days, see below |

### Alert summaries

Each entry counts the notifications that match it over the last 7 days and
groups them by the named groups of its `pattern`:

| Field | Meaning |
|---|---|
| `name` | Name of the summary; gives `sensor.iphone_<name>_alerts_7_days` |
| `app` | Only notifications from an app whose name contains this (optional) |
| `sender` | Only notifications whose title (mail sender) contains this (optional) |
| `pattern` | Regex on the subtitle (mail subject), or the title when there is none. Named groups such as `(?P<host>…)` become `top_host` lists |
| `exclude` | Regex on subject and body; matching notifications are skipped (optional) |

The default counts Zabbix problem mails in Outlook by `host` and `problem`,
skipping "Escalation canceled" re-sends.

## Entities

With the Mosquitto broker app installed, the bridge publishes one device,
**iPhone Notification Forward**, via MQTT discovery:

| Entity | What |
|---|---|
| `sensor.iphone_last_notification` | Last notification: `App: Title`, full notification as attributes |
| `event.iphone_notification` | One event per notification change (`add`, `update`, `remove`, `remove_all`) |
| `sensor.iphone_notifications_total` | Count of new notifications |
| `sensor.iphone_notifications_<app>` | Count per app, created on the app's first notification |
| `sensor.iphone_notifications_today` | New notifications today |
| `sensor.iphone_notifications_7_days` | New notifications in the last 7 days; attributes `top_apps` (20 noisiest apps) and `per_day` |
| `sensor.iphone_<name>_alerts_7_days` | Per alert summary: matches in the last 7 days; attributes `top_<group>`, `per_day` and `recent` (last 10) |

The 7-day numbers are kept in `/data/stats.json`. On the first start of 0.3.0
they are filled from Home Assistant's history of the last-notification sensor.
Home Assistant's own health-check push (`group: nf-healthcheck`) only fires the
event and is not counted.

Counters are `total_increasing`, so Home Assistant keeps hourly/daily/monthly
statistics (use a *Statistics graph* card). Counts are stored in
`/data/counts.json`. Without MQTT the bridge falls back to a plain
`sensor.iphone_last_notification` (no device, no counters). The
`iphone_notification` bus event is fired either way.

## Keys

The bridge's long-term keys and session secrets live in `/data/state.json`,
which is included in add-on backups. To start over, uninstall and reinstall the
add-on, then remove and re-pair the bridge in the iPhone app.

## Troubleshooting

- `Could not locate bluetooth adapter`: the dongle isn't passed through to the VM.
- `unknown key id … (re-pair …)`: an internet delivery arrived for keys the
  add-on doesn't have. Open the iPhone app near the dongle so iOS redoes the
  key exchange.
- `could not decrypt Bluetooth message`: same fix. If it persists, set
  `log_level: debug` and open an issue with the log.
