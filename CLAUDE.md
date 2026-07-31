# Working conventions for this repo

This is a standalone Home Assistant custom integration (HACS-installable) for the B&D Smart Hub garage door controller (SDD WAN cloud API). It's deliberately separate from the private research repo it was derived from, so it can be published on its own later without dragging along reverse-engineering notes, session logs, or anything credential-shaped.

## Where the protocol write-up lives

The WAN API this integration talks to was reverse-engineered in a sibling repo, `../garage-door/` (private, not published) - see `../garage-door/wan-api/README.md` for the full protocol spec, decryption schemes, and session logs explaining how each piece was confirmed. `custom_components/bnd_smart_hub/sdd_client.py` here is a **copy** of `../garage-door/wan-api/client/sdd_client.py` (that repo's copy is the canonical one, with its own test suite) - keep them in sync manually when the protocol implementation changes. If this repo is ever published independently, that cross-reference stops resolving for other readers; replace it with whatever public write-up (if any) makes sense at that point.

`custom_components/bnd_smart_hub/sdd-cloud-ca.pem` is likewise a copy of `../garage-door/wan-api/reference/sdd-cloud-ca.pem` (the private CA `sdd_client.py` needs to validate `version3.smartdoordevices.com`'s cert chain) - it has to live **inside** `custom_components/bnd_smart_hub/` here, not in a sibling `reference/` dir like the source repo's layout, because HACS only ever pulls the `custom_components/bnd_smart_hub/` directory itself. `CA_BUNDLE_PATH` in `sdd_client.py` was adjusted accordingly when copying it over - don't blindly copy that one line verbatim from the source repo if it changes there.

## Structure

- `custom_components/bnd_smart_hub/` - the actual integration, standard HA custom component layout (`manifest.json`, `config_flow.py`, `coordinator.py`, entity platforms, `translations/`).
- `custom_components/bnd_smart_hub/helpers.py` - pure device-state logic (no `homeassistant` import) so it's unit-testable with plain `pytest`, without needing a full Home Assistant dev environment.
- `hacs.json` - HACS repo metadata, at repo root (required for HACS custom-repository installs).

## No secrets here, ever

Unlike the parent research repo, this one should never contain real credentials at all - a user's `bsid`/`phoneKey`/`phoneSecret`/etc. are entered through the config flow at runtime and stored in Home Assistant's own config entry storage, never in this repo. If you ever find yourself about to commit anything credential-shaped here, stop - that almost certainly means it ended up somewhere it shouldn't (e.g. a test using a real value instead of a fake one).

## Testing

`tests/` (top-level, deliberately **not** nested inside `custom_components/bnd_smart_hub/`) covers `helpers.py` and `sdd_client.py` - neither needs `homeassistant` installed, so both run with plain `pytest` from the repo root (`pip install -r requirements-dev.txt && pytest tests/`). It has to live outside the real package: `custom_components/bnd_smart_hub/__init__.py` imports real `homeassistant` modules at the top level, and pytest's default import mode walks up to and executes any enclosing package's `__init__.py` before collecting a test file within it - so a `tests/` directory nested inside the component would force `homeassistant` to be installed just to run these pytest-only tests.

- `tests/test_helpers.py` loads `helpers.py`/`const.py` directly by file path (with a stub parent package registered in `sys.modules`) specifically to sidestep that, without changing how `helpers.py` is written or how it runs for real inside Home Assistant. Needed because `helpers.py` has a relative import (`from .const import ...`).
- `tests/test_sdd_client_crypto.py` / `tests/test_sdd_client_requests.py` are copies of the canonical test suite in the sibling research repo (`../garage-door/wan-api/client/tests/`) - keep them in sync manually alongside `sdd_client.py` itself (see "Where the protocol write-up lives" above). `sdd_client.py` has no relative imports, so `tests/conftest.py` just adds `custom_components/bnd_smart_hub/` to `sys.path` and they `import sdd_client` directly, same as the canonical copy.

GitHub Actions (`.github/workflows/tests.yml`) runs the full `tests/` suite on every pull request and on push to `main`.

Testing the actual entities/config flow against real Home Assistant would need `pytest-homeassistant-custom-component` and a full HA dev environment, which hasn't been set up yet - treat anything beyond `helpers.py`/`sdd_client.py` as manually tested only (by installing into a real HA instance) until that's in place.
