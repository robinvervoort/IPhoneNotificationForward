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
