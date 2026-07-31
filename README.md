# B&D Smart Hub for Home Assistant

A Home Assistant custom integration for the B&D Smart Hub garage door controller (SDO-6 motor + Smart Hub, hub model SDO9V1), talking to the same cloud API the official "B&D Smart Garage Access" app uses. No local network access to the hub is required - everything goes over the same WAN/cloud protocol the app itself uses.

**Status: early / unpublished.** Built and tested against one real hub; not yet submitted to the HACS default store. Install as a HACS custom repository (or by copying `custom_components/bnd_smart_hub/` into your `config/custom_components/` directory) if you want to try it now.

## What this gives you

- A `cover` entity for the garage door (`device_class: garage`) - open/close/stop.
- A `light` entity for the garage light - on/off.

Because these are standard Home Assistant entity types, they work with everything that already understands `cover`/`light` entities - including the Home Assistant iOS Companion app's Apple CarPlay "common controls" widget, Siri/HomeKit (via Home Assistant's HomeKit Bridge integration), automations, dashboards, etc. There's nothing CarPlay-specific in this integration's code; it just needs to expose well-behaved standard entities.

## Setup

1. In Home Assistant: Settings → Devices & Services → Add Integration → "B&D Smart Hub".
2. You'll need a registration/join code from the B&D app (wherever it shows the "add remote access" screen for a new device) and your B&D account password. The integration uses these once, to pair a brand-new client identity and bootstrap its own credentials (`app/remoteregister` → `app/v3migrate` → `auth`) - it never needs your account password again after setup, and never touches the account you use in the official app itself (this is a separate, independent client registration).
3. Credentials are stored in Home Assistant's own config entry storage, not in any file inside this integration.

## Known limitations

- Only one hub/device has been tested against so far.
- Door position is reported as closed/not-closed only - there's no confirmed data yet for what a partially-open position value looks like, so `current_cover_position` (a 0-100% value) isn't implemented, only `is_closed`/`is_opening`/`is_closing`.
- Polls for state every 30s by default; commands (open/close/light) always trigger an immediate refresh regardless.
- No re-authentication flow yet if a session ever needs refreshing - the credentials this integration uses (device identity + session key) haven't shown any expiry in testing so far, but if that ever changes, re-adding the integration is the current workaround.

## Attribution

The WAN API this integration talks to isn't officially documented - it was reverse-engineered from the official Android app via static analysis and live traffic capture. This repo doesn't include that research; it only implements the resulting protocol.
