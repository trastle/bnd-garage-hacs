"""Shared fixtures for tests that exercise the integration against a real
(test) Home Assistant core, via `pytest-homeassistant-custom-component` -
as opposed to ../test_helpers.py and ../test_sdd_client_*.py, which test
pure logic with no `homeassistant` involved at all and stay fast/sync.
Scoped to this directory (not the shared tests/conftest.py) so those other
tests aren't forced to pull in Home Assistant or run under pytest-asyncio.
"""

from pathlib import Path

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bnd_smart_hub.const import DOMAIN

MOCK_ENTRY_DATA = {
    "bsid": "bsid1",
    "phoneId": "phone1",
    "phoneSecret": "secret1",
    "phonePassword": "pw1",
    "phoneKey": "key1",
    "sessionKey": "sess1",
    "accountPassword": "acctpw1",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Home Assistant only loads custom_components/ in tests when
    explicitly told to - this switches that on for every test here.
    """
    yield


@pytest.fixture
def hass_config_dir() -> str:
    """Point Home Assistant's test config dir at the real repo root, so its
    component loader finds custom_components/bnd_smart_hub/ here instead of
    the plugin's own empty default test config directory.
    """
    return str(Path(__file__).parent.parent.parent)


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    """A config entry with a full, fake (non-network-reachable) credential
    set - enough to construct a coordinator/set up the integration without
    ever needing a real SDD account.
    """
    return MockConfigEntry(domain=DOMAIN, data=dict(MOCK_ENTRY_DATA))
