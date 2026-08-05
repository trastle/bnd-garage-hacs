"""Tests for helpers.py - deliberately loaded by file path rather than a
normal `import`. helpers.py's relative import (`from .const import ...`)
would otherwise force Python to first execute the package's own
__init__.py, which imports real `homeassistant` modules - defeating the
point of keeping this module homeassistant-free and plain-pytest-testable.
Loading by path with a stub parent package in sys.modules sidesteps that
without changing anything about how helpers.py is written or how it runs
for real inside Home Assistant.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "bnd_smart_hub"


def _load(name: str) -> types.ModuleType:
    full_name = f"custom_components.bnd_smart_hub.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, COMPONENT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
_bnd_stub = types.ModuleType("custom_components.bnd_smart_hub")
_bnd_stub.__path__ = [str(COMPONENT_DIR)]
sys.modules.setdefault("custom_components.bnd_smart_hub", _bnd_stub)

_load("const")
helpers = _load("helpers")


def _device(**overrides) -> dict:
    base = {"position": 0, "pendingCommand": 0, "lightOn": False}
    base.update(overrides)
    return base


def test_is_closed_when_position_zero():
    assert helpers.is_closed(_device(position=0)) is True


def test_is_closed_false_when_position_nonzero():
    assert helpers.is_closed(_device(position=50)) is False


def test_is_closed_none_when_position_missing():
    device = _device()
    del device["position"]
    assert helpers.is_closed(device) is None


@pytest.mark.parametrize("command_code", [2])
def test_is_opening_true_for_open_command(command_code):
    assert helpers.is_opening(_device(pendingCommand=command_code)) is True


@pytest.mark.parametrize("command_code", [4])
def test_is_closing_true_for_close_command(command_code):
    assert helpers.is_closing(_device(pendingCommand=command_code)) is True


def test_not_opening_or_closing_when_no_pending_command():
    device = _device(pendingCommand=0)
    assert helpers.is_opening(device) is False
    assert helpers.is_closing(device) is False


def test_is_light_on_reflects_device_field():
    assert helpers.is_light_on(_device(lightOn=True)) is True
    assert helpers.is_light_on(_device(lightOn=False)) is False


def test_parse_time_string_hh_mm():
    assert helpers.parse_time_string("06:00") == helpers.dt_time(6, 0)


def test_parse_time_string_hh_mm_ss_ignores_seconds():
    assert helpers.parse_time_string("22:00:30") == helpers.dt_time(22, 0)


DAY_START = helpers.dt_time(6, 0)
DAY_END = helpers.dt_time(22, 0)


@pytest.mark.parametrize(
    "now,expected_minutes",
    [
        (helpers.dt_time(6, 0), 3),  # exactly day_start -> day
        (helpers.dt_time(12, 0), 3),  # midday -> day
        (helpers.dt_time(21, 59), 3),  # just before day_end -> still day
        (helpers.dt_time(22, 0), 15),  # exactly day_end -> night
        (helpers.dt_time(23, 30), 15),  # late night -> night
        (helpers.dt_time(0, 0), 15),  # midnight -> night
        (helpers.dt_time(5, 59), 15),  # just before day_start -> still night
    ],
)
def test_current_poll_interval_minutes_default_schedule(now, expected_minutes):
    assert (
        helpers.current_poll_interval_minutes(now, DAY_START, DAY_END, day_interval_minutes=3, night_interval_minutes=15)
        == expected_minutes
    )


def test_parse_device_list_returns_devices_keyed_by_id():
    response = {"data": [_device(deviceId="dev1"), _device(deviceId="dev2")]}
    assert helpers.parse_device_list(response) == {
        "dev1": _device(deviceId="dev1"),
        "dev2": _device(deviceId="dev2"),
    }


def test_parse_device_list_empty_list_is_valid():
    assert helpers.parse_device_list({"data": []}) == {}


@pytest.mark.parametrize(
    "response",
    [
        "not a dict",
        {},  # no "data" key at all
        {"data": None},
        {"data": "not a list"},
        {"data": {"deviceId": "dev1"}},  # a single device dict, not wrapped in a list
    ],
)
def test_parse_device_list_rejects_malformed_top_level_shape(response):
    with pytest.raises(ValueError):
        helpers.parse_device_list(response)


@pytest.mark.parametrize(
    "devices",
    [
        ["not a dict"],
        [{"name": "no deviceId field at all"}],
        [{"deviceId": 12345}],  # deviceId present but not a string
        [{"deviceId": None}],
    ],
)
def test_parse_device_list_rejects_malformed_device_entries(devices):
    with pytest.raises(ValueError):
        helpers.parse_device_list({"data": devices})


def test_current_poll_interval_minutes_handles_wraparound_day_window():
    # a day window that itself wraps past midnight (not the default, but the
    # boundary math needs to hold up if someone configures it this way)
    night_wraps_start = helpers.dt_time(22, 0)
    night_wraps_end = helpers.dt_time(6, 0)
    # "day" is now 22:00 -> 06:00 (wraps midnight); 23:00 should read as day
    assert (
        helpers.current_poll_interval_minutes(
            helpers.dt_time(23, 0), night_wraps_start, night_wraps_end, day_interval_minutes=3, night_interval_minutes=15
        )
        == 3
    )
    # and noon should read as night under that same wrapped window
    assert (
        helpers.current_poll_interval_minutes(
            helpers.dt_time(12, 0), night_wraps_start, night_wraps_end, day_interval_minutes=3, night_interval_minutes=15
        )
        == 15
    )
