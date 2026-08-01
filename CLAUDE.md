# Working conventions for this repo

This is a standalone Home Assistant custom integration (HACS-installable) for garage door hubs on the Smart Door Devices (SDD) WAN cloud API, currently tested against a B&D hub.

**Display name vs domain**: published under the name "Smart Door Devices Hub", but the `domain` (`bnd_smart_hub`, set in `manifest.json` and `const.py`) is unchanged from this integration's original name and must stay that way - Home Assistant has no migration path for changing a `config_entries` domain, so changing it would orphan every existing install's config entry and entities. Only the human-facing name changes (`manifest.json`'s `name`, `hacs.json`'s `name`, `strings.json`/`translations/en.json`, and log/title strings in the code) - Python identifiers like `BnDSmartHubCoordinator` were deliberately left alone too, since renaming those is a purely internal, non-user-visible choice with no bearing on the domain question.

## The CA trust file

`custom_components/bnd_smart_hub/sdd-root-ca-public.pem` is the CA cert `sdd_client.py` needs to validate `version3.smartdoordevices.com`'s cert chain. It has to live **inside** `custom_components/bnd_smart_hub/` because HACS only ever pulls that directory - anything this integration needs at runtime has to be in there, not in a separate top-level directory. `CA_BUNDLE_PATH` in `sdd_client.py` points at it.

This is SmartDoorDevices' **Root** CA, not the Intermediate CA the official app itself pins to - deliberately, so a future intermediate rotation validates automatically without needing a new release of this integration (see the detailed comment above `CA_BUNDLE_PATH` in `sdd_client.py` for the full reasoning). **Provenance matters here**: this file came from a direct live TLS connection to `version3.smartdoordevices.com` from the public internet (`openssl s_client -showcerts`), not from extracting anything out of the official app's own bundled keystore. If this file is ever regenerated, keep noting which method produced it - a live TLS fetch and an app-extracted artifact carry meaningfully different trust weight and shouldn't get conflated.

## Structure

- `custom_components/bnd_smart_hub/` - the actual integration, standard HA custom component layout (`manifest.json`, `config_flow.py`, `coordinator.py`, entity platforms, `translations/`).
- `custom_components/bnd_smart_hub/sdd_client.py` - the Smart Door Devices (SDD) protocol client; see its own module docstring and comments for the endpoints and wire format it implements.
- `custom_components/bnd_smart_hub/helpers.py` - pure device-state logic (no `homeassistant` import) so it's unit-testable with plain `pytest`, without needing a full Home Assistant dev environment.
- `hacs.json` - HACS repo metadata, at repo root (required for HACS custom-repository installs).

## No secrets here, ever

This repo should never contain real credentials at all - a user's `bsid`/`phoneKey`/`phoneSecret`/etc. are entered through the config flow at runtime and stored in Home Assistant's own config entry storage, never in this repo. If you ever find yourself about to commit anything credential-shaped here, stop - that almost certainly means it ended up somewhere it shouldn't (e.g. a test using a real value instead of a fake one).

## Testing

`pip install -r requirements-dev.txt && pytest tests/` runs everything, both the tests below and the HA-dependent ones. `pytest.ini` sets `asyncio_mode = auto` so `async def test_...` functions run without individual `@pytest.mark.asyncio` markers - needed by everything under `tests/components/`.

`tests/` lives at the top level, deliberately **not** nested inside `custom_components/bnd_smart_hub/`: `custom_components/bnd_smart_hub/__init__.py` imports real `homeassistant` modules at the top level, and pytest's default import mode walks up to and executes any enclosing package's `__init__.py` before collecting a test file within it - so a `tests/` directory nested inside the component would force that import to happen just to collect tests that don't need it.

**Fast, no-`homeassistant`-needed tests** (repo root of `tests/`):
- `tests/test_helpers.py` loads `helpers.py`/`const.py` directly by file path (with a stub parent package registered in `sys.modules`), specifically so collecting it never touches the real package `__init__.py`. Needed because `helpers.py` has a relative import (`from .const import ...`).
- `tests/test_sdd_client_crypto.py` / `tests/test_sdd_client_requests.py` cover `sdd_client.py`'s crypto primitives and HTTP request shapes. `sdd_client.py` has no relative imports, so `tests/conftest.py` just adds `custom_components/bnd_smart_hub/` to `sys.path` and they `import sdd_client` directly.

**`tests/components/` - real Home Assistant core, via `pytest-homeassistant-custom-component`** (a fake `hass` instance, `MockConfigEntry`, a real config-entry/entity/device registry, etc.) - this is what actually exercises `coordinator.py`, `config_flow.py`, `cover.py`, `light.py`, and `__init__.py`, none of which the fast tests above touch at all. Every `sdd_client` call in these tests is mocked (via `unittest.mock.patch` on the exact `sdd_client.<function>` the code under test calls) - nothing here makes a real network request. `tests/components/conftest.py` provides two things every test file here needs:
- `hass_config_dir` - overrides the plugin's default (which points at its own empty package directory) to point at this repo's actual root, so Home Assistant's component loader finds `custom_components/bnd_smart_hub/` here instead of "Integration not found".
- `auto_enable_custom_integrations` (autouse) - Home Assistant only loads `custom_components/` in tests when explicitly told to; this switches that on.

These fixtures are scoped to `tests/components/` only (their own `conftest.py`, not the shared root one), so the fast tests above stay fast and don't need `homeassistant` installed or `pytest-asyncio` semantics to run.

Two real bugs were caught writing this suite, both fixed in the same change that added it - worth knowing about since they're exactly the class of regression this suite exists to catch:
- `BnDSmartHubCoordinator.__init__` called `self._scheduled_interval()` (which reads `self._fast_poll_until`) as an argument to `super().__init__()`, before `self._fast_poll_until` was assigned - an `AttributeError` on every single setup, not just an edge case.
- `light.py`'s `device_info` only gave `identifiers`, relying on `cover.py`'s entity to be the one that establishes the shared device's name/manufacturer/model. `__init__.py` loads the `cover`/`light` platforms concurrently, so which one actually creates the device (and therefore what entity_id the light gets assigned) wasn't guaranteed - reproduced locally as genuine run-to-run flakiness. Fixed by giving both entities' `device_info` the same full information.

GitHub Actions (`.github/workflows/tests.yml`) runs the full `tests/` suite (both fast and HA-dependent) on every pull request and on push to `main`.
