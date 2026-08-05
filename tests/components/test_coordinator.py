"""Tests for BnDSmartHubCoordinator - the optimistic-state/fast-poll/
cooldown/token-refresh behavior layered on top of the plain SDD API calls
(see coordinator.py's module docstring). Every sdd_client call is mocked;
nothing here touches the network.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.bnd_smart_hub import sdd_client
from custom_components.bnd_smart_hub.const import (
    COMMAND_CODE_CLOSE,
    COMMAND_CODE_OPEN,
    DEVICE_COMMAND_CLOSE,
    DEVICE_COMMAND_LIGHT_OFF,
    DEVICE_COMMAND_LIGHT_ON,
    DEVICE_COMMAND_OPEN,
    DEVICE_COMMAND_STOP,
)
from custom_components.bnd_smart_hub.coordinator import (
    FAST_POLL_INTERVAL,
    BnDSmartHubCoordinator,
)

SDD = "custom_components.bnd_smart_hub.coordinator.sdd_client"


async def _coordinator(hass, entry) -> BnDSmartHubCoordinator:
    entry.add_to_hass(hass)
    return BnDSmartHubCoordinator(hass, entry)


# --------------------------------------------------------------------------
# Construction - regression test for the _fast_poll_until init-order bug:
# __init__ computes its initial update_interval via _scheduled_interval(),
# which reads self._fast_poll_until, so that attribute must already exist
# by the time super().__init__() runs.
# --------------------------------------------------------------------------


async def test_coordinator_constructs_without_error(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    assert coordinator.update_interval is not None
    assert coordinator.bsid == "bsid1"


# --------------------------------------------------------------------------
# device_data() - optimistic overlay merging
# --------------------------------------------------------------------------


async def test_device_data_merges_optimistic_overlay(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0, "lightOn": False}}
    coordinator._optimistic["dev1"] = {"pendingCommand": COMMAND_CODE_OPEN}

    merged = coordinator.device_data("dev1")

    assert merged["pendingCommand"] == COMMAND_CODE_OPEN
    assert merged["lightOn"] is False  # untouched fields survive the merge


async def test_device_data_returns_real_data_when_no_overlay(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}

    assert coordinator.device_data("dev1") == {"pendingCommand": 0}


# --------------------------------------------------------------------------
# async_send_command() - optimistic overlay, fast-poll kickoff, cooldown
# --------------------------------------------------------------------------


async def test_send_open_sets_optimistic_pending_command(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}
    coordinator.async_request_refresh = AsyncMock()

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_OPEN)

    assert coordinator.device_data("dev1")["pendingCommand"] == COMMAND_CODE_OPEN
    coordinator.async_request_refresh.assert_awaited_once()


async def test_send_close_sets_optimistic_pending_command(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}
    coordinator.async_request_refresh = AsyncMock()

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_CLOSE)

    assert coordinator.device_data("dev1")["pendingCommand"] == COMMAND_CODE_CLOSE


async def test_send_light_on_off_sets_optimistic_light_state(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"lightOn": False}}
    coordinator.async_request_refresh = AsyncMock()

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_LIGHT_ON)
        assert coordinator.device_data("dev1")["lightOn"] is True

        # cooldown would otherwise block this immediate second command -
        # simulate the cooldown having already elapsed
        coordinator._last_command_at["dev1"] = dt_util.utcnow() - timedelta(seconds=10)
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_LIGHT_OFF)
        assert coordinator.device_data("dev1")["lightOn"] is False


async def test_send_stop_has_no_optimistic_overlay(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}
    coordinator.async_request_refresh = AsyncMock()

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_STOP)

    assert coordinator.device_data("dev1") == {"pendingCommand": 0}


async def test_send_command_starts_fast_poll(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}
    coordinator.async_request_refresh = AsyncMock()
    assert coordinator._fast_poll_until is None

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_OPEN)

    assert coordinator._fast_poll_until is not None
    assert coordinator._fast_poll_until > dt_util.utcnow()


async def test_send_command_clears_optimistic_overlay_on_error(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}

    with patch(f"{SDD}.send_device_command", side_effect=sdd_client.SddError("boom")):
        with pytest.raises(HomeAssistantError):
            await coordinator.async_send_command("dev1", DEVICE_COMMAND_OPEN)

    assert "dev1" not in coordinator._optimistic


async def test_second_command_within_cooldown_is_rejected(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}
    coordinator.async_request_refresh = AsyncMock()

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_OPEN)
        with pytest.raises(HomeAssistantError, match="wait"):
            await coordinator.async_send_command("dev1", DEVICE_COMMAND_CLOSE)


async def test_stop_is_exempt_from_cooldown(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}
    coordinator.async_request_refresh = AsyncMock()

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_OPEN)
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_STOP)  # must not raise


async def test_cooldown_is_per_device(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}, "dev2": {"pendingCommand": 0}}
    coordinator.async_request_refresh = AsyncMock()

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_OPEN)
        await coordinator.async_send_command("dev2", DEVICE_COMMAND_OPEN)  # must not raise


async def test_cooldown_clears_after_window_elapses(hass, mock_entry, freezer):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator.data = {"dev1": {"pendingCommand": 0}}
    coordinator.async_request_refresh = AsyncMock()

    with patch(f"{SDD}.send_device_command", return_value={}):
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_OPEN)
        freezer.tick(timedelta(seconds=6))
        await coordinator.async_send_command("dev1", DEVICE_COMMAND_CLOSE)  # must not raise


# --------------------------------------------------------------------------
# _scheduled_interval() - fast-poll override vs the day/night schedule
# --------------------------------------------------------------------------


async def test_scheduled_interval_is_fast_poll_while_active(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator._fast_poll_until = dt_util.utcnow() + timedelta(seconds=30)

    assert coordinator._scheduled_interval() == FAST_POLL_INTERVAL


async def test_scheduled_interval_falls_back_once_fast_poll_expires(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator._fast_poll_until = dt_util.utcnow() - timedelta(seconds=1)  # already in the past

    assert coordinator._scheduled_interval() != FAST_POLL_INTERVAL


# --------------------------------------------------------------------------
# _async_update_data() - optimistic overlay clearing, fast-poll early exit
# --------------------------------------------------------------------------


async def test_update_data_clears_optimistic_overlay(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator._optimistic["dev1"] = {"pendingCommand": COMMAND_CODE_OPEN}

    with patch(f"{SDD}.get_devices", return_value={"data": [{"deviceId": "dev1", "pendingCommand": 0}]}):
        data = await coordinator._async_update_data()

    assert coordinator._optimistic == {}
    assert data["dev1"]["pendingCommand"] == 0


async def test_update_data_ends_fast_poll_early_once_nothing_transitioning(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator._fast_poll_until = dt_util.utcnow() + timedelta(seconds=30)

    with patch(f"{SDD}.get_devices", return_value={"data": [{"deviceId": "dev1", "pendingCommand": 0}]}):
        await coordinator._async_update_data()

    assert coordinator._fast_poll_until is None


async def test_update_data_keeps_fast_poll_while_still_transitioning(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)
    coordinator._fast_poll_until = dt_util.utcnow() + timedelta(seconds=30)

    with patch(f"{SDD}.get_devices", return_value={"data": [{"deviceId": "dev1", "pendingCommand": 2}]}):
        await coordinator._async_update_data()

    assert coordinator._fast_poll_until is not None


async def test_update_data_raises_update_failed_on_sdd_error(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)

    with patch(f"{SDD}.get_devices", side_effect=sdd_client.SddError("boom")):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


async def test_update_data_raises_update_failed_on_malformed_response(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)

    # a 200 response that doesn't match the expected shape (e.g. "data"
    # missing entirely) should fail cleanly via UpdateFailed, not crash with
    # a raw KeyError/TypeError - see helpers.parse_device_list()
    with patch(f"{SDD}.get_devices", return_value={"errorCode": 0, "state": 0}):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


# --------------------------------------------------------------------------
# _async_refresh_token_if_due() - proactive 24h re-authentication
# --------------------------------------------------------------------------


async def test_token_refresh_skipped_when_not_due(hass, mock_entry):
    coordinator = await _coordinator(hass, mock_entry)

    with patch(f"{SDD}.authenticate") as mock_auth:
        await coordinator._async_refresh_token_if_due()

    mock_auth.assert_not_called()


async def test_token_refresh_updates_session_key_when_due(hass, mock_entry, freezer):
    coordinator = await _coordinator(hass, mock_entry)
    freezer.tick(timedelta(hours=25))

    with patch(f"{SDD}.authenticate", return_value={"data": {"key": "new-session-key"}}):
        await coordinator._async_refresh_token_if_due()

    assert coordinator.session_key == "new-session-key"
    assert coordinator.entry.data["sessionKey"] == "new-session-key"


async def test_token_refresh_soft_fails_on_sdd_error(hass, mock_entry, freezer):
    coordinator = await _coordinator(hass, mock_entry)
    freezer.tick(timedelta(hours=25))
    original_session_key = coordinator.session_key

    with patch(f"{SDD}.authenticate", side_effect=sdd_client.SddError("boom")):
        await coordinator._async_refresh_token_if_due()  # must not raise

    assert coordinator.session_key == original_session_key


async def test_token_refresh_soft_fails_when_no_session_key_in_response(hass, mock_entry, freezer):
    coordinator = await _coordinator(hass, mock_entry)
    freezer.tick(timedelta(hours=25))
    original_session_key = coordinator.session_key

    with patch(f"{SDD}.authenticate", return_value={"data": {}}):
        await coordinator._async_refresh_token_if_due()  # must not raise

    assert coordinator.session_key == original_session_key
