"""Cover entity for the garage door, driven by the Smart Door Devices (SDD)
cloud API - see sdd_client.py.

This is the entity type Home Assistant's iOS Companion app surfaces in its
Apple CarPlay "common controls" widget - device_class=GARAGE covers are one
of the recognized types there. Nothing else needs to be done here to get
that; it's automatic for any cover entity of this device class.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import helpers
from .const import DEVICE_COMMAND_CLOSE, DEVICE_COMMAND_OPEN, DEVICE_COMMAND_STOP, DOMAIN
from .coordinator import BnDSmartHubCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BnDSmartHubCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(BnDGarageDoorCover(coordinator, device_id) for device_id in coordinator.data)


class BnDGarageDoorCover(CoordinatorEntity[BnDSmartHubCoordinator], CoverEntity):
    """The garage door itself - the primary entity of its device."""

    _attr_has_entity_name = True
    _attr_name = None  # this is the device's main feature; entity == device name
    _attr_device_class = CoverDeviceClass.GARAGE
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

    def __init__(self, coordinator: BnDSmartHubCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{coordinator.bsid}_{device_id}_door"

    @property
    def _device(self) -> dict:
        # Merges in any optimistic overlay from a just-sent command - see
        # BnDSmartHubCoordinator.device_data().
        return self.coordinator.device_data(self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        device = self._device
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device.get("name") or "Garage Door",
            manufacturer="B&D",
            model=(device.get("model") or "").strip() or None,
            sw_version=(device.get("firmware") or "").strip() or None,
        )

    @property
    def is_closed(self) -> bool | None:
        return helpers.is_closed(self._device)

    @property
    def is_opening(self) -> bool:
        return helpers.is_opening(self._device)

    @property
    def is_closing(self) -> bool:
        return helpers.is_closing(self._device)

    @property
    def available(self) -> bool:
        return super().available and not self._device.get("offline", False)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._device_id, DEVICE_COMMAND_OPEN)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._device_id, DEVICE_COMMAND_CLOSE)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._device_id, DEVICE_COMMAND_STOP)
