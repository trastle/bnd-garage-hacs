# B&D Smart Hub for Home Assistant

A Home Assistant custom integration for the B&D Smart Hub garage door controller (SDO-6 motor + Smart Hub, hub model SDO9V1), talking to the same cloud API the official "B&D Smart Garage Access" app uses. No local network access to the hub is required - everything goes over the same WAN/cloud protocol the app itself uses.

**Status: early / unpublished.** Built and tested against one real hub; not yet submitted to the HACS default store. Install as a HACS custom repository (or by copying `custom_components/bnd_smart_hub/` into your `config/custom_components/` directory) if you want to try it now.

## What this gives you

- A `cover` entity for the garage door (`device_class: garage`) - open/close/stop.
- A `light` entity for the garage light - on/off.

Because these are standard Home Assistant entity types, they work with everything that already understands `cover`/`light` entities - including the Home Assistant iOS Companion app's Apple CarPlay "common controls" widget, Siri/HomeKit (via Home Assistant's HomeKit Bridge integration), automations, dashboards, etc. There's nothing CarPlay-specific in this integration's code; it just needs to expose well-behaved standard entities.

## Setup

1. In Home Assistant: Settings → Devices & Services → Add Integration → "B&D Smart Hub".
2. You'll need a registration/join code from the B&D app (wherever it shows the "add remote access" screen for a new device) and your B&D account password. The integration uses these to pair a brand-new client identity and bootstrap its own credentials (`app/remoteregister` → `app/v3migrate` → `auth`) - this is a separate, independent client registration from the account you use in the official app itself.
3. **Your account password is stored** as part of the config entry, alongside the resulting device credentials - not written to any file inside this integration, but it does sit in Home Assistant's own config storage (plaintext JSON on disk, protected only by OS file permissions, the same as several other cloud integrations that need standing credentials). This is a deliberate tradeoff: the integration needs it to proactively re-authenticate every 24h (see below), matching how the real app itself behaves - it resends the actual account password on every login too, rather than treating it as a one-time secret.

## Polling schedule

State (door position, light) is polled on a day/night schedule rather than one fixed interval - by default, every 3 minutes from 6am to 10pm and every 15 minutes overnight, on the theory that a physically idle garage door doesn't need checking as often while everyone's asleep. Configurable per-hub afterward via Settings → Devices & Services → B&D Smart Hub → Configure (day/night start times and both intervals). A command sent through Home Assistant (open/close/light) always triggers an immediate refresh regardless of the schedule.

Every refresh also checks whether it's been 24h since the last authentication and, if so, silently re-authenticates and stores the fresh session key. This is defensive/"for good measure" rather than a fix for a known problem - nothing in testing has shown the session key actually expiring - so a failed refresh attempt is logged and retried on the next cycle rather than treated as an error. The 24h timer isn't persisted across a Home Assistant restart; if HA restarts more often than every 24h, the timer effectively resets each time (a real gap, but a low-stakes one for what this is trying to guard against).

## Known limitations

- Only one hub/device has been tested against so far.
- Door position is reported as closed/not-closed only - there's no confirmed data yet for what a partially-open position value looks like, so `current_cover_position` (a 0-100% value) isn't implemented, only `is_closed`/`is_opening`/`is_closing`.

## Attribution

The WAN API this integration talks to isn't officially documented - it was reverse-engineered from the official Android app via static analysis and live traffic capture. This repo doesn't include that research; it only implements the resulting protocol.
