"""Tests for __init__.py's async_setup_entry/async_unload_entry - the
full component-loader path (real custom_components/bnd_smart_hub loading,
not just constructing BnDSmartHubCoordinator directly like test_coordinator.py
does), with sdd_client mocked so no network call happens.
"""

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.bnd_smart_hub.const import DOMAIN

SDD = "custom_components.bnd_smart_hub.coordinator.sdd_client"


async def test_setup_entry_loads_and_forwards_platforms(hass, mock_entry):
    mock_entry.add_to_hass(hass)

    with patch(f"{SDD}.get_devices", return_value={"data": []}):
        result = await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert result is True
    assert mock_entry.state is ConfigEntryState.LOADED
    assert DOMAIN in hass.data
    assert mock_entry.entry_id in hass.data[DOMAIN]


async def test_setup_entry_creates_entities_from_device_list(hass, mock_entry):
    mock_entry.add_to_hass(hass)
    device = {
        "deviceId": "dev1", "name": "Garage Door", "position": 0, "pendingCommand": 0,
        "lightOn": False, "lightSupported": True, "offline": False,
    }

    with patch(f"{SDD}.get_devices", return_value={"data": [device]}):
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("cover.garage_door") is not None
    assert hass.states.get("light.garage_door_light") is not None


async def test_unload_entry_cleans_up_hass_data(hass, mock_entry):
    mock_entry.add_to_hass(hass)

    with patch(f"{SDD}.get_devices", return_value={"data": []}):
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        unloaded = await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert unloaded is True
    assert mock_entry.entry_id not in hass.data.get(DOMAIN, {})
