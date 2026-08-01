"""Tests for the garage light entity - on/off state, availability, service
calls, and the lightSupported gate that decides whether the entity exists
at all (see light.py's async_setup_entry).
"""

from unittest.mock import patch

from custom_components.bnd_smart_hub.const import DEVICE_COMMAND_LIGHT_OFF, DEVICE_COMMAND_LIGHT_ON

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


async def test_light_reports_on(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, lightOn=True)
    assert hass.states.get("light.garage_door_light").state == "on"


async def test_light_reports_off(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, lightOn=False)
    assert hass.states.get("light.garage_door_light").state == "off"


async def test_light_entity_not_created_when_unsupported(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, lightSupported=False)
    assert hass.states.get("light.garage_door_light") is None


async def test_light_unavailable_when_device_offline(hass, mock_entry):
    await _setup_with_device(hass, mock_entry, offline=True)
    assert hass.states.get("light.garage_door_light").state == "unavailable"


async def test_turn_on_service_sends_light_on_command(hass, mock_entry):
    entry = await _setup_with_device(hass, mock_entry, lightOn=False)
    coordinator = hass.data[entry.domain][entry.entry_id]
    with patch.object(coordinator, "async_send_command") as mock_send:
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": "light.garage_door_light"}, blocking=True,
        )
    mock_send.assert_awaited_once_with("dev1", DEVICE_COMMAND_LIGHT_ON)


async def test_turn_off_service_sends_light_off_command(hass, mock_entry):
    entry = await _setup_with_device(hass, mock_entry, lightOn=True)
    coordinator = hass.data[entry.domain][entry.entry_id]
    with patch.object(coordinator, "async_send_command") as mock_send:
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": "light.garage_door_light"}, blocking=True,
        )
    mock_send.assert_awaited_once_with("dev1", DEVICE_COMMAND_LIGHT_OFF)


async def test_light_shares_device_with_cover(hass, mock_entry):
    from homeassistant.helpers import entity_registry as er

    await _setup_with_device(hass, mock_entry)
    registry = er.async_get(hass)
    cover_entry = registry.async_get("cover.garage_door")
    light_entry = registry.async_get("light.garage_door_light")
    assert cover_entry.device_id == light_entry.device_id
