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
