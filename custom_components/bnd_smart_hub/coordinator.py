"""DataUpdateCoordinator polling getDevices() for all entities in this entry.

Each refresh is a real live appv3/message -> appv3/poll round trip (see
sdd_client.call_and_wait()) - typically 1-2s, occasionally longer, never
instant. How often that happens follows a configurable day/night schedule
(see helpers.current_poll_interval_minutes() and the Options flow in
config_flow.py) rather than a single fixed interval - a physically idle
garage door doesn't need checking as often overnight.

Sending a command (async_send_command()) does three things beyond the plain
API call, all aimed at the same UX gap: tapping "Open" on a widget/dashboard
gave no feedback until the next scheduled poll happened to land, which could
be minutes away:

  1. Optimistic state - an overlay (see _OPTIMISTIC_OVERLAY) is merged onto
     the affected device's data and pushed to entities immediately, before
     the command even reaches the network, so the UI shows "Opening"/
     "Closing"/the new light state right away. device_data() is what
     entities should read through instead of self.data directly. The real
     next poll always wins - _async_update_data() drops all optimistic
     overlays as soon as fresh real data arrives, whether or not it agrees.
  2. Fast polling - once a command is sent, _scheduled_interval() switches
     to FAST_POLL_INTERVAL for up to FAST_POLL_DURATION, instead of waiting
     out the normal day/night schedule, so the real state catches up
     quickly. Ends early, before the full window elapses, once no device is
     reporting mid-transition (is_opening/is_closing) - a light toggle has
     nothing to wait on, so this typically cuts the burst down to a single
     extra poll for that case.
  3. Cooldown - COMMAND_COOLDOWN blocks a second command to the same device
     within 5s of the last one (guards against accidental double-taps on a
     laggy connection). STOP is deliberately exempt from being blocked by
     this - a door mid-transition needs to be stoppable immediately, even
     right after the command that started it.

Every refresh also checks whether it's time to proactively re-authenticate
(TOKEN_REFRESH_INTERVAL, 24h) - nothing in testing has shown the session key
actually expiring, so this is defensive/"for good measure" rather than a fix
for a known failure, and soft-fails (log + retry next cycle) rather than
breaking the whole update if the re-auth attempt itself fails.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import helpers, sdd_client
from .const import (
    COMMAND_CODE_CLOSE,
    COMMAND_CODE_OPEN,
    CONF_DAY_END,
    CONF_DAY_INTERVAL_MINUTES,
    CONF_DAY_START,
    CONF_NIGHT_INTERVAL_MINUTES,
    DEFAULT_DAY_END,
    DEFAULT_DAY_INTERVAL_MINUTES,
    DEFAULT_DAY_START,
    DEFAULT_NIGHT_INTERVAL_MINUTES,
    DEVICE_COMMAND_CLOSE,
    DEVICE_COMMAND_LIGHT_OFF,
    DEVICE_COMMAND_LIGHT_ON,
    DEVICE_COMMAND_OPEN,
    DEVICE_COMMAND_STOP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_INTERVAL = timedelta(hours=24)
COMMAND_COOLDOWN = timedelta(seconds=5)
FAST_POLL_INTERVAL = timedelta(seconds=3)
FAST_POLL_DURATION = timedelta(seconds=60)

# What to optimistically merge onto a device's data the instant a command is
# sent - see the module docstring's "Optimistic state". STOP has no entry:
# we don't know what position it'll actually stop at, so there's nothing
# honest to show until the next real poll: it still triggers fast polling.
_OPTIMISTIC_OVERLAY: dict[str, dict] = {
    DEVICE_COMMAND_OPEN: {"pendingCommand": COMMAND_CODE_OPEN},
    DEVICE_COMMAND_CLOSE: {"pendingCommand": COMMAND_CODE_CLOSE},
    DEVICE_COMMAND_LIGHT_ON: {"lightOn": True},
    DEVICE_COMMAND_LIGHT_OFF: {"lightOn": False},
}


class BnDSmartHubCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Holds the credential set for one hub and polls its device list."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=self._scheduled_interval())
        data = entry.data
        self.bsid: str = data["bsid"]
        self.phone_id: str = data["phoneId"]
        self.phone_secret: str = data["phoneSecret"]
        self.phone_password: str = data["phonePassword"]
        self.phone_key: str = data["phoneKey"]
        self.session_key: str = data["sessionKey"]
        self.account_password: str = data["accountPassword"]
        # Assume the just-completed config flow auth counts as the first
        # refresh, so the first proactive one lands ~24h from setup, not
        # immediately. Not persisted across a Home Assistant restart - if HA
        # restarts more often than every 24h, the timer effectively resets
        # each time. Acceptable for a defensive/"for good measure" refresh,
        # not worth the complexity of persisting it for now.
        self._last_token_refresh = dt_util.utcnow()
        # device_id -> overlay dict; see device_data() and the module docstring
        self._optimistic: dict[str, dict] = {}
        # device_id -> when its last command was sent, for COMMAND_COOLDOWN
        self._last_command_at: dict[str, datetime] = {}
        # set by async_send_command(), read by _scheduled_interval(); None
        # means "no command-driven fast poll in progress right now"
        self._fast_poll_until: datetime | None = None

    def device_data(self, device_id: str) -> dict:
        """Real device data with any pending optimistic overlay merged on
        top. Entities should read through this, not self.data directly, so
        a just-sent command shows up immediately instead of waiting for the
        next real poll to confirm it.
        """
        device = self.data[device_id]
        overlay = self._optimistic.get(device_id)
        return {**device, **overlay} if overlay else device

    def _scheduled_interval(self) -> timedelta:
        """The poll interval for right now, per the configured day/night
        schedule - re-read from options fresh each call, so an options
        change or a day/night boundary crossing takes effect on the very
        next refresh without needing a reload. Overridden by FAST_POLL_INTERVAL
        while a command-driven fast-poll window is active (see
        async_send_command() and the module docstring).
        """
        if self._fast_poll_until is not None and dt_util.utcnow() < self._fast_poll_until:
            return FAST_POLL_INTERVAL
        options = self.entry.options
        day_start = helpers.parse_time_string(options.get(CONF_DAY_START, DEFAULT_DAY_START))
        day_end = helpers.parse_time_string(options.get(CONF_DAY_END, DEFAULT_DAY_END))
        day_minutes = options.get(CONF_DAY_INTERVAL_MINUTES, DEFAULT_DAY_INTERVAL_MINUTES)
        night_minutes = options.get(CONF_NIGHT_INTERVAL_MINUTES, DEFAULT_NIGHT_INTERVAL_MINUTES)
        minutes = helpers.current_poll_interval_minutes(
            dt_util.now().time(), day_start, day_end, day_minutes, night_minutes
        )
        return timedelta(minutes=minutes)

    async def _async_refresh_token_if_due(self) -> None:
        if dt_util.utcnow() - self._last_token_refresh < TOKEN_REFRESH_INTERVAL:
            return
        try:
            auth_result = await self.hass.async_add_executor_job(
                sdd_client.authenticate,
                self.bsid,
                self.phone_id,
                self.phone_secret,
                self.phone_password,
                self.account_password,
                False,  # temporary
                self.phone_key,
            )
        except sdd_client.SddError as err:
            _LOGGER.warning("Proactive B&D Smart Hub token refresh failed, will retry next cycle: %s", err)
            return
        session_key = auth_result.get("data", {}).get("key")
        if not session_key:
            _LOGGER.warning("Proactive token refresh got a response with no session key, will retry next cycle")
            return
        self.session_key = session_key
        self._last_token_refresh = dt_util.utcnow()
        self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, "sessionKey": session_key})
        _LOGGER.debug("Proactively refreshed the B&D Smart Hub session key")

    async def _async_update_data(self) -> dict[str, dict]:
        # Recomputed on every refresh (not just at startup) so a day/night
        # boundary crossing or an options change is picked up for the *next*
        # scheduled call - DataUpdateCoordinator reads self.update_interval
        # again each time it reschedules itself.
        self.update_interval = self._scheduled_interval()
        await self._async_refresh_token_if_due()
        try:
            response = await self.hass.async_add_executor_job(
                sdd_client.get_devices,
                self.bsid,
                self.phone_id,
                self.phone_secret,
                self.session_key,
                self.phone_key,
            )
        except sdd_client.SddError as err:
            raise UpdateFailed(f"Error talking to the B&D Smart Hub API: {err}") from err
        devices = response.get("data", [])
        data = {device["deviceId"]: device for device in devices}

        # Real data has now arrived for every device, so any optimistic
        # overlay has either been confirmed or superseded - drop it either
        # way rather than risk it lingering past what's actually true.
        self._optimistic.clear()
        if self._fast_poll_until is not None and not any(
            helpers.is_opening(device) or helpers.is_closing(device) for device in data.values()
        ):
            # nothing left mid-transition - no reason to keep polling fast
            # for the rest of the window
            self._fast_poll_until = None

        return data

    async def async_send_command(self, device_id: str, command: str) -> None:
        """Send a device command and immediately refresh state to match.

        See the module docstring for the optimistic-state/fast-poll/cooldown
        behavior this adds on top of the plain API call.
        """
        now = dt_util.utcnow()
        last_command_at = self._last_command_at.get(device_id)
        if (
            command != DEVICE_COMMAND_STOP
            and last_command_at is not None
            and now - last_command_at < COMMAND_COOLDOWN
        ):
            remaining = (COMMAND_COOLDOWN - (now - last_command_at)).total_seconds()
            raise HomeAssistantError(
                f"Please wait {remaining:.0f}s before sending another command to this device"
            )
        self._last_command_at[device_id] = now

        overlay = _OPTIMISTIC_OVERLAY.get(command)
        if overlay:
            self._optimistic[device_id] = overlay
            self.async_update_listeners()  # instant UI feedback, ahead of the network round trip

        self._fast_poll_until = now + FAST_POLL_DURATION

        try:
            await self.hass.async_add_executor_job(
                sdd_client.send_device_command,
                self.bsid,
                self.phone_id,
                self.phone_secret,
                device_id,
                command,
                self.session_key,
                self.phone_key,
            )
        except sdd_client.SddError as err:
            self._optimistic.pop(device_id, None)
            raise HomeAssistantError(f"Error sending {command!r} to {device_id}: {err}") from err
        await self.async_request_refresh()
