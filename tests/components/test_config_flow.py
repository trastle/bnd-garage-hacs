"""Tests for the config flow's pair -> migrate -> auth bootstrap chain and
the options flow. Every sdd_client call is mocked; nothing here touches the
network, and time.sleep is patched out so the migrate retry loop doesn't
actually wait between attempts.
"""

from unittest.mock import patch

from custom_components.bnd_smart_hub import sdd_client
from custom_components.bnd_smart_hub.const import (
    CONF_DAY_END,
    CONF_DAY_INTERVAL_MINUTES,
    CONF_DAY_START,
    CONF_NIGHT_INTERVAL_MINUTES,
    DOMAIN,
)

CF = "custom_components.bnd_smart_hub.config_flow.sdd_client"

PAIR_RESULT = {
    "bsid": "bsid1", "phoneId": "phone1", "phoneSecret": "legacy-secret",
    "phonePassword": "legacy-pw", "userId": "user1", "userName": "Test User", "isAdmin": True,
}
MIGRATE_SUCCESS = {
    "bsid": "bsid1", "phoneId": "phone1", "phoneKey": "key1",
    "hubKey": "hubkey1", "phoneSecret": "new-secret", "phonePassword": "new-pw",
}
AUTH_SUCCESS = {"data": {"key": "sess1"}}


async def _start_and_submit(hass, join_code="ABC123", password="hunter2"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"join_code": join_code, "password": password},
    )


async def test_full_setup_flow_success(hass):
    with (
        patch(f"{CF}.remote_register", return_value=PAIR_RESULT),
        patch(f"{CF}.v3migrate_prepare", return_value={"session": "opaque"}),
        patch(f"{CF}.v3migrate_attempt", return_value=MIGRATE_SUCCESS),
        patch(f"{CF}.authenticate", return_value=AUTH_SUCCESS),
    ):
        result = await _start_and_submit(hass)

    assert result["type"] == "create_entry"
    assert result["data"]["bsid"] == "bsid1"
    assert result["data"]["sessionKey"] == "sess1"
    assert result["data"]["accountPassword"] == "hunter2"  # stored for the 24h refresh


async def test_migrate_retries_then_succeeds(hass):
    attempts = [{"pending": True, "ack": {}}, {"pending": True, "ack": {}}, MIGRATE_SUCCESS]
    with (
        patch(f"{CF}.remote_register", return_value=PAIR_RESULT),
        patch(f"{CF}.v3migrate_prepare", return_value={"session": "opaque"}),
        patch(f"{CF}.v3migrate_attempt", side_effect=attempts) as mock_attempt,
        patch(f"{CF}.authenticate", return_value=AUTH_SUCCESS),
        patch("custom_components.bnd_smart_hub.config_flow.time.sleep"),
    ):
        result = await _start_and_submit(hass)

    assert result["type"] == "create_entry"
    assert mock_attempt.call_count == 3


async def test_migrate_timeout_shows_cannot_connect(hass):
    with (
        patch(f"{CF}.remote_register", return_value=PAIR_RESULT),
        patch(f"{CF}.v3migrate_prepare", return_value={"session": "opaque"}),
        patch(f"{CF}.v3migrate_attempt", return_value={"pending": True, "ack": {}}),
        patch("custom_components.bnd_smart_hub.config_flow.time.sleep"),
        patch("custom_components.bnd_smart_hub.config_flow.MIGRATE_TIMEOUT_SECONDS", 0),
    ):
        result = await _start_and_submit(hass)

    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"


async def test_setup_flow_shows_error_on_sdd_error(hass):
    with patch(f"{CF}.remote_register", side_effect=sdd_client.SddError("bad join code")):
        result = await _start_and_submit(hass)

    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"


async def test_setup_flow_aborts_on_duplicate_bsid(hass, mock_entry):
    mock_entry.add_to_hass(hass)

    # simulate the existing entry already having this bsid as its unique_id
    hass.config_entries.async_update_entry(mock_entry, unique_id="bsid1")

    with (
        patch(f"{CF}.remote_register", return_value=PAIR_RESULT),
        patch(f"{CF}.v3migrate_prepare", return_value={"session": "opaque"}),
        patch(f"{CF}.v3migrate_attempt", return_value=MIGRATE_SUCCESS),
        patch(f"{CF}.authenticate", return_value=AUTH_SUCCESS),
    ):
        result = await _start_and_submit(hass)

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_options_flow_round_trip(hass, mock_entry):
    mock_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DAY_START: "07:00:00",
            CONF_DAY_END: "21:00:00",
            CONF_DAY_INTERVAL_MINUTES: 5,
            CONF_NIGHT_INTERVAL_MINUTES: 20,
        },
    )

    assert result["type"] == "create_entry"
    assert mock_entry.options[CONF_DAY_START] == "07:00:00"
    assert mock_entry.options[CONF_DAY_INTERVAL_MINUTES] == 5
