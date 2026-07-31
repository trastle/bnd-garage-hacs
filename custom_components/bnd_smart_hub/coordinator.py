"""DataUpdateCoordinator polling getDevices() for all entities in this entry.

Each refresh is a real live appv3/message -> appv3/poll round trip (see
sdd_client.call_and_wait()) - typically 1-2s, occasionally longer, never
instant. How often that happens follows a configurable day/night schedule
(see helpers.current_poll_interval_minutes() and the Options flow in
config_flow.py) rather than a single fixed interval - a physically idle
garage door doesn't need checking as often overnight. A command
(open/close/light) always triggers its own immediate refresh regardless of
the schedule.
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


class BnDSmartHubCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Holds the credential set for one hub and polls its device list."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=self._scheduled_interval())
        data = entry.data
        self.bsid: str = data["bsid"]
        self.phone_id: str = data["phoneId"]
        self.phone_secret: str = data["phoneSecret"]
        self.phone_key: str = data["phoneKey"]
        self.session_key: str = data["sessionKey"]

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

    async def _async_update_data(self) -> dict[str, dict]:
        # Recomputed on every refresh (not just at startup) so a day/night
        # boundary crossing or an options change is picked up for the *next*
        # scheduled call - DataUpdateCoordinator reads self.update_interval
        # again each time it reschedules itself.
        self.update_interval = self._scheduled_interval()
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
