"""The Smart Door Devices Hub integration - see sdd_client.py for the
underlying Smart Door Devices (SDD) cloud API client it's built on.

Published under the display name "Smart Door Devices Hub" but kept on its
original domain, "bnd_smart_hub", for backwards compatibility with existing
installs - renaming a config_entries domain has no automatic migration path
in Home Assistant, so changing it would orphan every existing config entry
and entity.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import BnDSmartHubCoordinator

PLATFORMS = ["cover", "light"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = BnDSmartHubCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
