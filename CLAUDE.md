# Working conventions for this repo

This is a standalone Home Assistant custom integration (HACS-installable) for the B&D Smart Hub garage door controller (SDD WAN cloud API). It's deliberately separate from the private research repo it was derived from, so it can be published on its own later without dragging along reverse-engineering notes, session logs, or anything credential-shaped.

## Where the protocol write-up lives

The WAN API this integration talks to was reverse-engineered in a sibling repo, `../garage-door/` (private, not published) - see `../garage-door/wan-api/README.md` for the full protocol spec, decryption schemes, and session logs explaining how each piece was confirmed. `custom_components/bnd_smart_hub/sdd_client.py` here is a **copy** of `../garage-door/wan-api/client/sdd_client.py` (that repo's copy is the canonical one, with its own test suite) - keep them in sync manually when the protocol implementation changes. If this repo is ever published independently, that cross-reference stops resolving for other readers; replace it with whatever public write-up (if any) makes sense at that point.

## Structure

- `custom_components/bnd_smart_hub/` - the actual integration, standard HA custom component layout (`manifest.json`, `config_flow.py`, `coordinator.py`, entity platforms, `translations/`).
- `custom_components/bnd_smart_hub/helpers.py` - pure device-state logic (no `homeassistant` import) so it's unit-testable with plain `pytest`, without needing a full Home Assistant dev environment.
- `hacs.json` - HACS repo metadata, at repo root (required for HACS custom-repository installs).

## No secrets here, ever

Unlike the parent research repo, this one should never contain real credentials at all - a user's `bsid`/`phoneKey`/`phoneSecret`/etc. are entered through the config flow at runtime and stored in Home Assistant's own config entry storage, never in this repo. If you ever find yourself about to commit anything credential-shaped here, stop - that almost certainly means it ended up somewhere it shouldn't (e.g. a test using a real value instead of a fake one).

## Testing

`tests/` (top-level, deliberately **not** nested inside `custom_components/bnd_smart_hub/`) covers only the pure logic in `helpers.py` - no `homeassistant` package dependency, run with plain `pytest` from the repo root. It has to live outside the real package: `custom_components/bnd_smart_hub/__init__.py` imports real `homeassistant` modules at the top level, and pytest's default import mode walks up to and executes any enclosing package's `__init__.py` before collecting a test file within it - so a `tests/` directory nested inside the component would force `homeassistant` to be installed just to run these pytest-only tests. `tests/test_helpers.py` loads `helpers.py`/`const.py` directly by file path (with a stub parent package registered in `sys.modules`) specifically to sidestep that, without changing how `helpers.py` is written or how it runs for real inside Home Assistant.

Testing the actual entities/config flow against real Home Assistant would need `pytest-homeassistant-custom-component` and a full HA dev environment, which hasn't been set up yet - treat anything beyond `helpers.py` as manually tested only (by installing into a real HA instance) until that's in place.
