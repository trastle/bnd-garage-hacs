"""Constants for the B&D Smart Hub integration.

See ../../wan-api/README.md in the parent repo for the full protocol
write-up this integration implements against.
"""

from datetime import timedelta

DOMAIN = "bnd_smart_hub"

DEFAULT_UPDATE_INTERVAL = timedelta(seconds=30)

# com.smartdoordevices.client.sdk.model.device.DeviceCommand - matches
# sdd_client.DEVICE_COMMAND, duplicated here so config_flow/coordinator don't
# need to import sdd_client just for these two names.
DEVICE_COMMAND_OPEN = "OPEN"
DEVICE_COMMAND_CLOSE = "CLOSE"
DEVICE_COMMAND_STOP = "STOP"
DEVICE_COMMAND_LIGHT_ON = "LIGHT_ON"
DEVICE_COMMAND_LIGHT_OFF = "LIGHT_OFF"

# Raw deviceCommand integer codes that mean "the door is currently moving in
# this direction" when seen in a device's pendingCommand field - see
# ../../wan-api/README.md's DeviceCommand table. PART_OPEN_1/2/3 and the
# OPEN_PERCENT_* codes also mean "opening" but haven't been exercised live;
# only the plain OPEN/CLOSE codes are included here deliberately.
OPENING_COMMAND_CODES = {2}
CLOSING_COMMAND_CODES = {4}
