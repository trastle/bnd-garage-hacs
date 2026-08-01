# Smart Door Devices Hub for Home Assistant

A Home Assistant custom integration for garage door hubs built on the **Smart Door Devices (SDD)** platform - the shared cloud backend behind several garage door brands' apps, including B&D's "Smart Garage Access" (see **Brands using SDD** below). No local network access to the hub is required - everything goes over the same WAN/cloud protocol those apps use.

**Status: early / unpublished.** Not yet submitted to the HACS default store - install as a HACS custom repository for now.

## Brands using SDD

These apps all talk to the same `smartdoordevices.com` backend under the hood - same API, same envelope format, same account system - all published by Automatic Technology Australia Pty Ltd (ATA):

| Brand | Region | App |
|---|---|---|
| B&D | Australia | [B&D Smart Garage Access](https://apps.apple.com/au/app/b-d-smart-garage-access/id1098490196) |
| ATA / Automatic Technology | Australia | [Automatic Technology](https://apps.apple.com/au/app/automatic-technology/id1057041188) |
| ATA / Automatic Technology America | United States | [Automatic Technology America](https://play.google.com/store/apps/details?id=com.ata.controlladoor) |
| Dominator | New Zealand | [Dominator Smart Garage Control](https://play.google.com/store/apps/details?id=nz.com.dominator.controlladoor) |
| Garador | New Zealand | [Garador Reliable Smart Garages](https://apps.apple.com/nz/app/garador/id1138615088) |

This integration has only been built and tested against a B&D hub (see **Tested hardware** below) - it should work unmodified against any of the others, since they share the same backend, but that's not yet confirmed. If you try it on a different brand, please open an issue with the result either way.

## Installation

1. In HACS: the "⋮" menu → **Custom repositories** → add this repository's URL, category "Integration".
2. Find "Smart Door Devices Hub" in the HACS list and click **Download**.
3. Restart Home Assistant.

(Without HACS: copy `custom_components/bnd_smart_hub/` into your `config/custom_components/` directory instead, then restart.)

## Setup

1. Settings → Devices & Services → Add Integration → **Smart Door Devices Hub**.
2. Enter a registration/join code from your app (B&D, ATA, Dominator, Garador - wherever it shows the "add remote access" screen for a new device) and your account password for that app. This pairs a brand-new, independent client identity - separate from the account you use in the app itself.
3. **Your account password is stored** as part of the resulting config entry, alongside the device credentials it produces - not written to any separate file, but it does sit in Home Assistant's own config storage (plaintext JSON on disk, protected only by OS file permissions, same as several other cloud integrations that need standing credentials). This is a deliberate tradeoff: the integration needs it to proactively re-authenticate every 24h (see below), the same way the app itself resends the actual account password on every login rather than treating it as a one-time secret.

## What you get

- A `cover` entity for the garage door (`device_class: garage`) - open/close/stop.
- A `light` entity for the garage light - on/off, if your hub reports light support.

Because these are standard Home Assistant entity types, they work with everything that already understands `cover`/`light` entities - including the Home Assistant iOS Companion app's Apple CarPlay "common controls" widget, Lock Screen and Control Center widgets, Siri/HomeKit (via Home Assistant's HomeKit Bridge integration), automations, dashboards, and so on. There's nothing CarPlay-specific in this integration's code; it just needs to expose well-behaved standard entities.

## Using it

Sending a command (open/close/stop/light) gives immediate feedback rather than waiting for the next scheduled poll to happen to land:

- The entity shows "Opening"/"Closing"/the new light state instantly, before the command even reaches the network, then gets confirmed (or corrected) by the next real poll.
- Polling switches to every 3s for up to 60s after a command, instead of waiting out the day/night schedule, so the real state catches up quickly - it stops early once nothing's left mid-transition.
- A second command to the same device within 5s of the last one is rejected, to guard against accidental double-taps. Stop is exempt from this, so a door can always be interrupted immediately.

### Polling schedule

Outside of the fast-polling burst above, state (door position, light) is checked on a day/night schedule rather than one fixed interval - by default, every 3 minutes from 6am to 10pm and every 15 minutes overnight, on the theory that a physically idle garage door doesn't need checking as often while everyone's asleep. Configurable per-hub via Settings → Devices & Services → Smart Door Devices Hub → **Configure** (day/night start times and both intervals).

### Session refresh

Every refresh also checks whether it's been 24h since the last authentication and, if so, silently re-authenticates and stores a fresh session key - defensive/"for good measure" rather than a fix for a known problem, since nothing in testing has shown the session key actually expiring. A failed refresh attempt is logged and retried on the next cycle rather than treated as an error. The 24h timer isn't persisted across a Home Assistant restart, so it effectively resets on every restart (a real gap, but a low-stakes one for what this is guarding against).

## Tested hardware

| Motor | Hub model | Firmware | Status |
|---|---|---|---|
| B&D SDO-6 (sectional/panel door) | SDO9V1 | 1.31 | Working - primary hardware this integration is developed and tested against |

Only one combination has been confirmed so far. If you try this on a different motor/hub/firmware combination, please open an issue with the result (working or not) so this table can grow.

## Known limitations

- Door position is reported as closed/not-closed only - there's no confirmed data yet for what a partially-open position value looks like, so `current_cover_position` (a 0-100% value) isn't implemented, only `is_closed`/`is_opening`/`is_closing`.

## Attribution

The SDD WAN API this integration talks to isn't officially documented - it was reverse-engineered from one of the apps in the brand family above via static analysis and live traffic capture.
