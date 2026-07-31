"""DataUpdateCoordinator polling getDevices() for all entities in this entry.

Each refresh is a real live appv3/message -> appv3/poll round trip (see
sdd_client.call_and_wait()) - typically 1-2s, occasionally longer, never
instant. How often that happens follows a configurable day/night schedule
(see helpers.current_poll_interval_minutes() and the Options flow in
config_flow.py) rather than a single fixed interval - a physically idle
garage door doesn't need checking as often overnight. A command
(open/close/light) always triggers its own immediate refresh regardless of
the schedule.

Every refresh also checks whether it's time to proactively re-authenticate
(TOKEN_REFRESH_INTERVAL, 24h) - nothing in testing has shown the session key
actually expiring, so this is defensive/"for good measure" rather than a fix
for a known failure, and soft-fails (log + retry next cycle) rather than
breaking the whole update if the re-auth attempt itself fails.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import helpers, sdd_client
from .const import (
    CONF_DAY_END,
    CONF_DAY_INTERVAL_MINUTES,
    CONF_DAY_START,
    CONF_NIGHT_INTERVAL_MINUTES,
    DEFAULT_DAY_END,
    DEFAULT_DAY_INTERVAL_MINUTES,
    DEFAULT_DAY_START,
    DEFAULT_NIGHT_INTERVAL_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_INTERVAL = timedelta(hours=24)


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

    def _scheduled_interval(self) -> timedelta:
        """The poll interval for right now, per the configured day/night
        schedule - re-read from options fresh each call, so an options
        change or a day/night boundary crossing takes effect on the very
        next refresh without needing a reload.
        """
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
        return {device["deviceId"]: device for device in devices}

    async def async_send_command(self, device_id: str, command: str) -> None:
        """Send a device command and immediately refresh state to match."""
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
            raise HomeAssistantError(f"Error sending {command!r} to {device_id}: {err}") from err
        await self.async_request_refresh()
