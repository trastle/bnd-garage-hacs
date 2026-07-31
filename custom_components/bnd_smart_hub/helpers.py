"""Pure device-state logic, kept free of any `homeassistant` import so it can
be unit tested with plain pytest (see tests/test_helpers.py) without needing
a full Home Assistant dev environment installed.
"""

from __future__ import annotations

from datetime import time as dt_time

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


def parse_time_string(value: str) -> dt_time:
    """Parse "HH:MM" or "HH:MM:SS" (what HA's TimeSelector stores) into a
    plain datetime.time - seconds are accepted but ignored, this schedule
    only has minute resolution.
    """
    hour, minute, *_ = value.split(":")
    return dt_time(int(hour), int(minute))


def current_poll_interval_minutes(
    now: dt_time,
    day_start: dt_time,
    day_end: dt_time,
    day_interval_minutes: int,
    night_interval_minutes: int,
) -> int:
    """How often to poll right now, given a day/night schedule.

    "Day" is [day_start, day_end) - inclusive of the start minute, exclusive
    of the end minute, so with the default 06:00/22:00 window, 21:59 is
    still day and 22:00 is already night. Handles a day window that wraps
    past midnight (day_start > day_end) too, though the default doesn't.
    """
    if day_start <= day_end:
        in_day = day_start <= now < day_end
    else:
        in_day = now >= day_start or now < day_end
    return day_interval_minutes if in_day else night_interval_minutes
