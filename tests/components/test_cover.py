"""Tests for the garage door cover entity - state reflects coordinator
data (including the optimistic overlay), availability tracks the device's
offline flag, and each service call dispatches the right command.
"""

from unittest.mock import patch

from custom_components.bnd_smart_hub.const import (
    DEVICE_COMMAND_CLOSE,
    DEVICE_COMMAND_OPEN,
    DEVICE_COMMAND_STOP,
)

SDD = "custom_components.bnd_smart_hub.coordinator.sdd_client"


async def _setup_with_device(hass, mock_entry, **device_overrides):
    mock_entry.add_to_hass(hass)
    device = {
        "deviceId": "dev1", "name": "Garage Door", "position": 0, "pendingCommand": 0,
        "lightOn": False, "lightSupported": True, "offline": False,
    }
    device.update(device_overrides)
    with patch(f"{SDD}.get_devices", return_value={"data": [device]}):
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
    return mock_entry


async def test_cover_reports_closed(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, position=0)
    assert hass.states.get("cover.garage_door").state == "closed"


async def test_cover_reports_open(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, position=100)
    assert hass.states.get("cover.garage_door").state == "open"


async def test_cover_reports_opening_while_pending_command_is_open(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, position=0, pendingCommand=2)
    assert hass.states.get("cover.garage_door").state == "opening"


async def test_cover_reports_closing_while_pending_command_is_close(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, position=100, pendingCommand=4)
    assert hass.states.get("cover.garage_door").state == "closing"


async def test_cover_unavailable_when_offline(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, offline=True)
    assert hass.states.get("cover.garage_door").state == "unavailable"


async def test_open_cover_service_sends_open_command(hass, mock_entry):
    entry = await _setup_with_device(hass, mock_entry)
    coordinator = hass.data[entry.domain][entry.entry_id]
    with patch.object(coordinator, "async_send_command") as mock_send:
        await hass.services.async_call(
            "cover", "open_cover", {"entity_id": "cover.garage_door"}, blocking=True,
        )
    mock_send.assert_awaited_once_with("dev1", DEVICE_COMMAND_OPEN)


async def test_close_cover_service_sends_close_command(hass, mock_entry):
    entry = await _setup_with_device(hass, mock_entry)
    coordinator = hass.data[entry.domain][entry.entry_id]
    with patch.object(coordinator, "async_send_command") as mock_send:
        await hass.services.async_call(
            "cover", "close_cover", {"entity_id": "cover.garage_door"}, blocking=True,
        )
    mock_send.assert_awaited_once_with("dev1", DEVICE_COMMAND_CLOSE)


async def test_stop_cover_service_sends_stop_command(hass, mock_entry):
    entry = await _setup_with_device(hass, mock_entry)
    coordinator = hass.data[entry.domain][entry.entry_id]
    with patch.object(coordinator, "async_send_command") as mock_send:
        await hass.services.async_call(
            "cover", "stop_cover", {"entity_id": "cover.garage_door"}, blocking=True,
        )
    mock_send.assert_awaited_once_with("dev1", DEVICE_COMMAND_STOP)


async def test_device_info_uses_device_name_and_model(hass, mock_entry):
    from homeassistant.helpers import device_registry as dr

    await _setup_with_device(hass, mock_entry, name="Duncraig Garage", model="SDO9V1", firmware="1.31")
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={("bnd_smart_hub", "dev1")})
    assert device.name == "Duncraig Garage"
    assert device.model == "SDO9V1"
    assert device.sw_version == "1.31"
    assert device.manufacturer == "B&D"
