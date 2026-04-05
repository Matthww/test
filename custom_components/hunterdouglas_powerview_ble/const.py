"""Constants for the BLE Battery Management System integration."""

import logging
from typing import Final

DOMAIN: Final[str] = "hunterdouglas_powerview_ble"
LOGGER: Final = logging.getLogger(__package__)
MFCT_ID: Final[int] = 2073
TIMEOUT: Final[int] = 5

CONF_HOME_KEY: Final[str] = "home_key"

# attributes (do not change)
ATTR_RSSI: Final[str] = "rssi"
