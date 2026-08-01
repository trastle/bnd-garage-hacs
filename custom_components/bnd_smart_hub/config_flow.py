"""Config flow for the B&D Smart Hub integration.

Runs the Smart Door Devices (SDD) client bootstrap chain once, at setup
time: pair (app/remoteregister) -> migrate (app/v3migrate, retried until it
completes) -> auth (appv3/message path=auth). The account password IS
stored as part of the config entry's
data (alongside the device credential set: bsid, phoneId, phoneSecret,
phonePassword, phoneKey, hubKey, sessionKey) - the coordinator needs it to
proactively re-authenticate every 24h (see coordinator.py), matching how the
app itself resends the real password on every login rather than treating it
as a one-time bootstrap secret.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from . import sdd_client
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

CONF_JOIN_CODE = "join_code"

MIGRATE_TIMEOUT_SECONDS = 30
MIGRATE_POLL_INTERVAL_SECONDS = 1.0

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_JOIN_CODE): str,
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


def _do_setup(join_code: str, password: str) -> dict:
    """Run pair -> migrate -> auth synchronously; call via executor job.

    Raises sdd_client.SddError on any real failure. Returns the full
    credential dict to store as the config entry's data.
    """
    pair_result = sdd_client.remote_register(join_code, password)

    # v3migrate_prepare() generates the RSA/EC keypair and random
    # newPhonePassword ONCE - reusing that same session across every retry
    # below matters, the server needs the same keys resent on every attempt
    # (see v3migrate_prepare()'s docstring in sdd_client.py for why).
    session = sdd_client.v3migrate_prepare(
        bsid=pair_result["bsid"],
        phone_id=pair_result["phoneId"],
        legacy_phone_secret=pair_result["phoneSecret"],
        legacy_phone_password=pair_result["phonePassword"],
        user_password=password,
    )
    deadline = time.monotonic() + MIGRATE_TIMEOUT_SECONDS
    migrate_result: dict = {"pending": True}
    while migrate_result.get("pending"):
        migrate_result = sdd_client.v3migrate_attempt(session)
        if migrate_result.get("pending"):
            if time.monotonic() >= deadline:
                raise sdd_client.SddError(
                    f"app/v3migrate never completed after {MIGRATE_TIMEOUT_SECONDS}s "
                    f"- last ack: {migrate_result.get('ack')}"
                )
            time.sleep(MIGRATE_POLL_INTERVAL_SECONDS)

    creds = {**pair_result, **migrate_result}

    auth_result = sdd_client.authenticate(
        bsid=creds["bsid"],
        phone_id=creds["phoneId"],
        phone_secret=creds["phoneSecret"],
        phone_password=creds["phonePassword"],
        user_password=password,
        phone_key=creds["phoneKey"],
    )
    session_key = auth_result.get("data", {}).get("key")
    if not session_key:
        raise sdd_client.SddError(f"auth succeeded but no session key in the response: {auth_result}")
    creds["sessionKey"] = session_key
    # Stored so the coordinator can proactively re-authenticate every 24h
    # (see coordinator.py) - authenticate() genuinely needs the real account
    # password every time, the same way the real app itself resends it on
    # every login rather than treating it as a one-time bootstrap secret.
    creds["accountPassword"] = password
    return creds


class BnDSmartHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an SDD-platform garage hub, published here
    as "B&D Smart Hub".
    """

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                creds = await self.hass.async_add_executor_job(
                    _do_setup, user_input[CONF_JOIN_CODE], user_input[CONF_PASSWORD]
                )
            except sdd_client.SddError as err:
                _LOGGER.error("B&D Smart Hub setup failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(creds["bsid"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=creds.get("name") or "B&D Smart Hub", data=creds)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> BnDSmartHubOptionsFlow:
        return BnDSmartHubOptionsFlow()


class BnDSmartHubOptionsFlow(config_entries.OptionsFlow):
    """Lets a user tune the day/night poll schedule after setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_DAY_START, default=options.get(CONF_DAY_START, DEFAULT_DAY_START)): selector.TimeSelector(),
                vol.Required(CONF_DAY_END, default=options.get(CONF_DAY_END, DEFAULT_DAY_END)): selector.TimeSelector(),
                vol.Required(
                    CONF_DAY_INTERVAL_MINUTES, default=options.get(CONF_DAY_INTERVAL_MINUTES, DEFAULT_DAY_INTERVAL_MINUTES)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=1440, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min")
                ),
                vol.Required(
                    CONF_NIGHT_INTERVAL_MINUTES,
                    default=options.get(CONF_NIGHT_INTERVAL_MINUTES, DEFAULT_NIGHT_INTERVAL_MINUTES),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=1440, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min")
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
