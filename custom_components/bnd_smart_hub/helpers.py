"""Pure device-state logic, kept free of any `homeassistant` import so it can
be unit tested with plain pytest (see tests/test_helpers.py) without needing
a full Home Assistant dev environment installed.
"""

from __future__ import annotations

from .const import CLOSING_COMMAND_CODES, OPENING_COMMAND_CODES


def is_closed(device: dict) -> bool | None:
    """True if the door is closed, False if not, None if unknown.

    The only field confirmed live so far is that `position == 0` means
    closed (see ../../wan-api/README.md "getDevices()") - there's no
    confirmed data for what a fully-open position value looks like, so
    treat any other position as simply "not closed" rather than guessing
    at a percentage.
    """
    position = device.get("position")
    if position is None:
        return None
    return position == 0


def is_opening(device: dict) -> bool:
    """True if the door is currently mid-open, per pendingCommand."""
    return device.get("pendingCommand") in OPENING_COMMAND_CODES


def is_closing(device: dict) -> bool:
    """True if the door is currently mid-close, per pendingCommand."""
    return device.get("pendingCommand") in CLOSING_COMMAND_CODES


def is_light_on(device: dict) -> bool | None:
    return device.get("lightOn")
