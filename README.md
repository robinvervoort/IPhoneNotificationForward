# iPhone Notification Forward – iPhone notifications in Home Assistant

Forward every iPhone notification to Home Assistant using Apple's official
**notification forwarding** (`AccessoryNotifications`, iOS 26.5+, EU only).

```
           at home (Bluetooth)                       away (internet)
iPhone ──────────────────────────► USB BT dongle     iPhone ──HTTPS──► ha.robinvervoort.be
  │  Apple encrypts every           (Proxmox → HA VM)                   webhook automation
  │  notification for the bridge         │                                    │ HA event
  ▼                                      ▼                                    ▼
DeskBot app + 3 extensions      DeskBot Notification Bridge add-on (decrypts)
                                         │
                                         ├─ event  iphone_notification
                                         └─ sensor.iphone_last_notification
```

Notifications are end-to-end encrypted from iOS to the add-on (HPKE with the
post-quantum X-Wing KEM). The iOS transport extension, your router, and Home
Assistant's webhook only ever handle ciphertext.

| Folder | What |
|---|---|
| `ios/` | iPhone companion app (SwiftUI) + Data Provider, Transport Security and Transport extensions |
| `addon/deskbot_notify/` | Home Assistant add-on: BLE peripheral, key exchange, decryption, HA events/sensor |
| `repository.yaml` | Makes this repo installable as a Home Assistant add-on repository |

## Requirements

- iPhone on **iOS 26.5+**, Apple Account in an **EU** country, located in the EU
  (development builds work everywhere; this is Apple's rule for installs).
- A Mac with Xcode 26.5+ and an Apple Developer account (for the entitlements).
- A **USB Bluetooth dongle** passed through to the Home Assistant VM. Pick one
  that Home Assistant lists as supported (e.g. a Realtek RTL8761B-based dongle).
- Only one accessory can receive forwarded notifications at a time. Enabling
  this stops forwarding to an Apple Watch. Your Amazfit uses Bluetooth ANCS, a
  separate mechanism, so it should keep working.

## 1. Pass the Bluetooth dongle through (Proxmox)

1. Plug the dongle into the Proxmox host.
2. Proxmox UI → your HA VM → **Hardware → Add → USB Device → Use USB Vendor/Device ID** →
   pick the dongle → **Add**.
3. Reboot the VM. In HA: **Settings → System → Hardware → All hardware** should
   list `hci0`. HA may offer the Bluetooth integration; adding it is fine, it
   coexists with the add-on.

## 2. Install the add-on

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories** → add
   `https://github.com/robinvervoort/IPhoneNotificationForward` (the repo must be public, or
   copy `addon/deskbot_notify` to `/addons/` on the HA host as a local add-on).
2. Install **DeskBot Notification Bridge** and start it. The log should say
   `Advertising as 'DeskBot'`.

## 3. Webhook for delivery while away

Create this automation (Settings → Automations → ⋮ → Edit in YAML). Pick a
long random webhook ID; it is the only thing protecting the URL from spam
(the payload itself is encrypted and authenticated).

```yaml
alias: DeskBot – relay iPhone notifications from the internet
mode: queued
max: 50
triggers:
  - trigger: webhook
    webhook_id: "<your-random-webhook-id>"
    allowed_methods: [POST]
    local_only: false
actions:
  - event: deskbot_bridge_inbound
    event_data:
      data: "{{ trigger.json.data }}"
      session_id: "{{ trigger.json.session_id }}"
      push_token: "{{ trigger.json.push_token }}"
```

The webhook URL will be `https://ha.robinvervoort.be/api/webhook/<your-random-webhook-id>`.
Make sure your NGINX proxy forwards `/api/webhook/`, which it does by default.

## 4. Build and install the iPhone app

```sh
brew install xcodegen
cd ios && xcodegen && open DeskBot.xcodeproj
```

1. Set your team on all four targets (or `DEVELOPMENT_TEAM` in `project.yml`).
   Register the app group `group.be.robinvervoort.deskbot`.
2. Run on your iPhone.
3. In the app, stand near the dongle: **Pair bridge** → pick *DeskBot Bridge*.
4. **Allow notification forwarding** → choose all apps or a subset.
5. Enter the webhook ID under *Away from home* → **Save**.

iOS now runs the key exchange with the add-on (log: `Key exchange complete (xWing)`).
After that, notifications arrive over Bluetooth at home and over the
internet elsewhere.

## Using it in Home Assistant

Every notification fires an `iphone_notification` event:

```json
{"v": 1, "action": "add", "id": "…", "app": "WhatsApp", "title": "Mom",
 "body": "Dinner at 7?", "subtitle": null, "summary": null, "thread": "…",
 "date": "2026-10-31T18:02:11Z", "should_alert": true, "focus_suppressed": false,
 "transport": "bluetooth"}
```

`action` is `add`, `update`, `remove` (dismissed on the phone) or `remove_all`.

`sensor.iphone_last_notification` shows `App: Title`. Its attributes hold the full payload,
plus `active_count` and `active` (notifications still on the lock screen).

Example trigger:

```yaml
triggers:
  - trigger: event
    event_type: iphone_notification
    event_data:
      action: add
      app: WhatsApp
```

## Status and caveats

- The add-on's cryptography is unit-tested against the `cryptography` library's
  HPKE implementation and RFC 9180 vectors (`cd addon/deskbot_notify && pytest`).
- **The iOS code has not been compiled yet.** It was written against Apple's
  documentation, which is new and in places inconsistent. Expect to fix small
  API mismatches in Xcode. Things to check first:
  - `AccessoryTransportSession.EventHandler`: Apple documents both
    `messageReceived(_:completion:)` (used here) and `dataEventHandler(event:)`.
  - Whether the extensions need the `com.apple.developer.accessory-*`
    entitlements enabled for your App ID in the developer portal.
  - XcodeGen needs a version that supports the `extensionkit-extension` target
    type. Otherwise create three *Generic Extension* targets in Xcode and copy
    the Info.plist/entitlement values from `project.yml`.
- Apple's sample for the Bluetooth key context is ambiguous. The add-on tries
  both readings and remembers the one that authenticates.
- Apple's developer terms forbid passing forwarded notifications on to other apps
  or devices, or using them for profiling or ads. This is meant for your own
  bridge and your own Home Assistant; don't publish it as a relay service.
