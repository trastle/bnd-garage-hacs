"""Constants for the B&D Smart Hub integration."""

DOMAIN = "bnd_smart_hub"

# Options flow keys - how often to poll getDevices(), on a day/night
# schedule (checking a physically-idle garage door every few seconds is
# unnecessary; polling less often overnight is a reasonable default, not a
# protocol requirement). All configurable via the integration's Options.
CONF_DAY_START = "day_start"
CONF_DAY_END = "day_end"
CONF_DAY_INTERVAL_MINUTES = "day_interval_minutes"
CONF_NIGHT_INTERVAL_MINUTES = "night_interval_minutes"

DEFAULT_DAY_START = "06:00"
DEFAULT_DAY_END = "22:00"
DEFAULT_DAY_INTERVAL_MINUTES = 3
DEFAULT_NIGHT_INTERVAL_MINUTES = 15

# Matches sdd_client.DEVICE_COMMAND, duplicated here so config_flow/
# coordinator don't need to import sdd_client just for these two names.
DEVICE_COMMAND_OPEN = "OPEN"
DEVICE_COMMAND_CLOSE = "CLOSE"
DEVICE_COMMAND_STOP = "STOP"
DEVICE_COMMAND_LIGHT_ON = "LIGHT_ON"
DEVICE_COMMAND_LIGHT_OFF = "LIGHT_OFF"

# Raw deviceCommand integer codes that mean "the door is currently moving in
# this direction" when seen in a device's pendingCommand field. PART_OPEN_1/
# 2/3 and the OPEN_PERCENT_* codes also mean "opening" but haven't been
# exercised live; only the plain OPEN/CLOSE codes are included here
# deliberately.
#
# COMMAND_CODE_OPEN/CLOSE are the same two codes, named individually because
# coordinator.py's optimistic-state overlay needs to write "the" open/close
# code we ourselves send, not just check membership in a set.
COMMAND_CODE_OPEN = 2
COMMAND_CODE_CLOSE = 4

OPENING_COMMAND_CODES = {COMMAND_CODE_OPEN}
CLOSING_COMMAND_CODES = {COMMAND_CODE_CLOSE}
