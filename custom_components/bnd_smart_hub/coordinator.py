"""DataUpdateCoordinator polling getDevices() for all entities in this entry.

Each refresh is a real live appv3/message -> appv3/poll round trip (see
sdd_client.call_and_wait()) - typically 1-2s, occasionally longer, never
instant. DEFAULT_UPDATE_INTERVAL is deliberately conservative (30s) since
there's no need to hammer the API just to keep entity state fresh; a command
(open/close/light) always triggers its own immediate refresh regardless of
this interval.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import sdd_client
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class BnDSmartHubCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Holds the credential set for one hub and polls its device list."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=entry.options.get("update_interval", DEFAULT_UPDATE_INTERVAL.total_seconds())),
        )
        self.entry = entry
        data = entry.data
        self.bsid: str = data["bsid"]
        self.phone_id: str = data["phoneId"]
        self.phone_secret: str = data["phoneSecret"]
        self.phone_key: str = data["phoneKey"]
        self.session_key: str = data["sessionKey"]

    async def _async_update_data(self) -> dict[str, dict]:
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
