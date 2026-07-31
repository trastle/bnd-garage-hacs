"""Light entity for the B&D Smart Hub garage light - on/off only, no
brightness or color control (the hardware doesn't support either)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import helpers
from .const import DEVICE_COMMAND_LIGHT_OFF, DEVICE_COMMAND_LIGHT_ON, DOMAIN
from .coordinator import BnDSmartHubCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BnDSmartHubCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BnDGarageLight(coordinator, device_id)
        for device_id, device in coordinator.data.items()
        if device.get("lightSupported")
    )


class BnDGarageLight(CoordinatorEntity[BnDSmartHubCoordinator], LightEntity):
    """The garage light - a secondary feature of the same device as the door."""

    _attr_has_entity_name = True
    _attr_name = "Light"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator: BnDSmartHubCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{coordinator.bsid}_{device_id}_light"

    @property
    def _device(self) -> dict:
        return self.coordinator.data[self._device_id]

    @property
    def device_info(self) -> DeviceInfo:
        # Same identifiers as the cover entity in cover.py - this attaches
        # the light to the same Home Assistant "device" as the door rather
        # than creating a separate device for it.
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def is_on(self) -> bool | None:
        return helpers.is_light_on(self._device)

    @property
    def available(self) -> bool:
        return super().available and not self._device.get("offline", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._device_id, DEVICE_COMMAND_LIGHT_ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._device_id, DEVICE_COMMAND_LIGHT_OFF)
