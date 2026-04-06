"""Config flow for BLE Battery Management System integration."""

import asyncio
import base64
from dataclasses import dataclass
import struct
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import UUID_COV_SERVICE as UUID
from .const import CONF_HOME_KEY, DOMAIN, LOGGER, MFCT_ID


def _needs_encryption(manufacturer_data_hex: str) -> bool:
    """Return True if the BLE advertisement indicates encryption (home_id != 0)."""
    data = bytearray.fromhex(manufacturer_data_hex)
    if len(data) < 2:
        return False
    home_id = int.from_bytes(data[0:2], byteorder="little")
    return home_id != 0


@dataclass
class HubShadeInfo:
    """Shade metadata from the PowerView hub."""

    name: str  # Human-readable name (decoded from base64)
    ble_name: str  # BLE advertisement name, e.g. "DUE:94ED"


async def _fetch_shades_from_hub(
    hass: HomeAssistant, hub_url: str
) -> list[HubShadeInfo]:
    """Fetch shade list with human-readable names from a PowerView G3 hub.

    Raises aiohttp.ClientError on network errors.
    Raises asyncio.TimeoutError on timeout.
    """
    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=10)

    async with session.get(f"{hub_url}/home/shades", timeout=timeout) as resp:
        resp.raise_for_status()
        shades = await resp.json(content_type=None)

    if not shades:
        return []

    hub_shades: list[HubShadeInfo] = []
    for shade in shades:
        ble_name = shade.get("bleName", "")
        if not ble_name:
            continue
        name_b64 = shade.get("name", "")
        try:
            name = base64.b64decode(name_b64).decode("utf-8") if name_b64 else ble_name
        except Exception:  # noqa: BLE001
            name = ble_name
        hub_shades.append(HubShadeInfo(name=name, ble_name=ble_name))
    return hub_shades


async def _fetch_key_and_shades_from_hub(
    hass: HomeAssistant, hub_url: str
) -> tuple[bytes, list[HubShadeInfo]]:
    """Fetch 16-byte homekey and shade list from a PowerView G3 hub.

    Returns (key, shade_list).  The key is network-wide so any reachable shade
    returns the same value.  The shade list contains human-readable names that
    can be used to label BLE-discovered devices.

    Raises ValueError on protocol/key errors.
    Raises aiohttp.ClientError on network errors.
    Raises asyncio.TimeoutError on timeout.
    """
    hub_shades = await _fetch_shades_from_hub(hass, hub_url)
    if not hub_shades:
        raise ValueError("No shades found on the hub")

    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=10)

    # GetShadeKey BLE request: sid=251, cid=18, seqId=1, data_len=0
    request_frame = struct.pack("<BBBB", 251, 18, 1, 0)

    # Try each shade until one returns a valid key (some may be out of range)
    last_error: Exception = ValueError("No shades responded")
    for hs in hub_shades:
        try:
            async with session.post(
                f"{hub_url}/home/shades/exec?shades={hs.ble_name}",
                json={"hex": request_frame.hex()},
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                result = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as ex:
            last_error = ex
            continue

        responses = result.get("responses", [])
        if len(responses) != 1 or "hex" not in responses[0]:
            continue

        response_bytes = bytes.fromhex(responses[0]["hex"])
        if len(response_bytes) < 5:
            continue
        _s, _c, _q, length = struct.unpack("<BBBB", response_bytes[0:4])
        if len(response_bytes) != 4 + length:
            continue
        if response_bytes[4] != 0:
            continue
        key_data = response_bytes[5:]
        if len(key_data) != 16:
            continue
        return key_data, hub_shades

    raise ValueError(f"No reachable shade returned a valid key: {last_error}")


_HOMEKEY_SCHEMA = vol.Schema(
    {
        vol.Required("key_method", default="hub"): SelectSelector(
            SelectSelectorConfig(
                options=[
                    {
                        "value": "hub",
                        "label": "Fetch automatically from PowerView hub",
                    },
                    {
                        "value": "manual",
                        "label": "Enter key manually (32 hex characters)",
                    },
                    {
                        "value": "skip",
                        "label": "Skip (no key — controls disabled for encrypted shades)",
                    },
                ]
            )
        ),
        vol.Optional("hub_url", default="http://powerview-g3.local"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional("home_key", default=""): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BT Battery Management System."""

    VERSION = 1
    MINOR_VERSION = 1

    @dataclass
    class DiscoveredDevice:
        """A discovered bluetooth device."""

        name: str
        discovery_info: BluetoothServiceInfoBleak

    def __init__(self) -> None:
        """Initialize the config flow."""

        self._discovered_device: ConfigFlow.DiscoveredDevice | None = None
        self._discovered_devices: dict[str, ConfigFlow.DiscoveredDevice] = {}
        self._manufacturer_data_hex: str = ""
        self._device_name: str = ""
        self._home_key: str = ""
        self._hub_url: str = ""
        self._hub_shades: list[HubShadeInfo] = []

    def _create_entry(self) -> ConfigFlowResult:
        """Create the config entry with collected data."""
        data: dict[str, str] = {
            "manufacturer_data": self._manufacturer_data_hex,
            CONF_HOME_KEY: self._home_key,
        }
        if self._hub_url:
            data["hub_url"] = self._hub_url
        return self.async_create_entry(
            title=self._device_name,
            data=data,
        )

    def _validate_manual_key(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> bool:
        """Validate a manually entered hex key and store it.

        Returns True on success, False on validation error.
        """
        raw = user_input.get("home_key", "").strip()
        if "\\x" in raw:
            raw = raw.replace("\\x", "")
        if len(raw) != 32:
            errors["home_key"] = "invalid_key_length"
            return False
        try:
            bytes.fromhex(raw)
        except ValueError:
            errors["home_key"] = "invalid_key_format"
            return False
        self._home_key = raw.lower()
        return True

    async def _validate_homekey_input(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> bool:
        """Parse and validate homekey user_input, populating self state.

        Returns True on success, False on validation error (errors dict is populated).
        On skip, self._home_key is set to "".
        """
        method = user_input.get("key_method", "skip")

        if method == "skip":
            self._home_key = ""
            return True

        if method == "manual":
            return self._validate_manual_key(user_input, errors)

        if method != "hub":
            return False

        hub_url = user_input.get("hub_url", "").rstrip("/")
        _HUB_ERROR_MAP: dict[type[Exception], str] = {
            aiohttp.ClientResponseError: "hub_http_error",
            aiohttp.ClientConnectionError: "hub_connection_error",
            TimeoutError: "hub_timeout",
            ValueError: "hub_protocol_error",
        }
        try:
            key, hub_shades = await _fetch_key_and_shades_from_hub(self.hass, hub_url)
        except tuple(_HUB_ERROR_MAP) as ex:
            errors["hub_url"] = _HUB_ERROR_MAP[type(ex)]
            return False

        self._home_key = key.hex()
        self._hub_url = hub_url
        self._hub_shades = hub_shades
        return True

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a flow initialized by Bluetooth discovery."""
        LOGGER.debug("Bluetooth device detected: %s", discovery_info)

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovered_device = ConfigFlow.DiscoveredDevice(
            discovery_info.name, discovery_info
        )
        self.context["title_placeholders"] = {"name": self._discovered_device.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm bluetooth device discovery."""
        assert self._discovered_device is not None
        LOGGER.debug("confirm step for %s", self._discovered_device.name)

        if user_input is not None:
            self._manufacturer_data_hex = (
                self._discovered_device.discovery_info.manufacturer_data[MFCT_ID].hex()
            )
            self._device_name = self._discovered_device.name

            # Unencrypted shades can skip the homekey step entirely
            if not _needs_encryption(self._manufacturer_data_hex):
                await self._resolve_friendly_name()
                return self._create_entry()

            return await self.async_step_homekey_bluetooth()

        self._set_confirm_only()

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._discovered_device.name},
        )

    async def async_step_homekey_bluetooth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure homekey for a shade discovered via Bluetooth."""
        # Reuse an existing key if another shade was already configured
        existing = self._existing_home_key()
        if existing and user_input is None:
            self._home_key = existing
            await self._resolve_friendly_name()
            return self._create_entry()

        errors: dict[str, str] = {}

        if user_input is not None and await self._validate_homekey_input(
            user_input, errors
        ):
            # Use hub name for the entry title if available
            friendly = self._hub_name_for(self._device_name)
            if friendly:
                self._device_name = friendly
            return self._create_entry()

        return self.async_show_form(
            step_id="homekey_bluetooth",
            data_schema=_HOMEKEY_SCHEMA,
            errors=errors,
            description_placeholders={"name": self._device_name},
        )

    def _existing_entry_value(self, key: str) -> str:
        """Return the first non-empty value for *key* across configured entries."""
        for entry in self._async_current_entries():
            if value := entry.data.get(key, ""):
                return value
        return ""

    def _existing_home_key(self) -> str:
        """Return the home_key from any already-configured entry, or ''."""
        return self._existing_entry_value(CONF_HOME_KEY)

    async def _resolve_friendly_name(self) -> None:
        """Try to resolve BLE device name to hub friendly name."""
        hub_url = self._hub_url or self._existing_entry_value("hub_url")
        if not hub_url:
            return
        try:
            shades = await _fetch_shades_from_hub(self.hass, hub_url)
            for hs in shades:
                if hs.ble_name == self._device_name:
                    self._device_name = hs.name
                    break
            if not self._hub_url:
                self._hub_url = hub_url
        except (TimeoutError, aiohttp.ClientError, ValueError):
            pass

    def _hub_name_for(self, ble_name: str) -> str | None:
        """Return the human-readable hub name for a BLE name, or None."""
        for hs in self._hub_shades:
            if hs.ble_name == ble_name:
                return hs.name
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step — reuse existing key or collect one."""
        LOGGER.debug("user step")
        existing = self._existing_home_key()
        if existing:
            self._home_key = existing
            return await self.async_step_select_device()
        return await self.async_step_homekey()

    def _build_selected_entries(
        self, user_input: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Build config entry data for each selected shade address."""
        addresses: list[str] = user_input[CONF_ADDRESS]
        if isinstance(addresses, str):
            addresses = [addresses]

        entries: list[dict[str, Any]] = []
        for address in addresses:
            device = self._discovered_devices[address]
            ble_name = device.name
            name = self._hub_name_for(ble_name) or ble_name
            mfct_hex = device.discovery_info.manufacturer_data[MFCT_ID].hex()
            entry_data: dict[str, str] = {
                "manufacturer_data": mfct_hex,
                CONF_HOME_KEY: self._home_key,
            }
            if self._hub_url:
                entry_data["hub_url"] = self._hub_url
            entries.append(
                {
                    "address": address,
                    "name": name,
                    "data": entry_data,
                }
            )
        return entries

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one or more BLE-discovered shades, or fall through to manual."""
        LOGGER.debug("select_device step")

        if user_input is not None:
            entries = self._build_selected_entries(user_input)

            # Kick off auto-add flows for all but the last shade
            await asyncio.gather(
                *(
                    self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": "auto_add"},
                        data=info,
                    )
                    for info in entries[:-1]
                )
            )

            # Create the final entry normally (ends this flow)
            last = entries[-1]
            await self.async_set_unique_id(last["address"], raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._device_name = last["name"]
            self._manufacturer_data_hex = last["data"]["manufacturer_data"]
            self.context["title_placeholders"] = {"name": self._device_name}
            return self._create_entry()

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue

            if MFCT_ID not in discovery_info.manufacturer_data:
                continue

            if UUID not in discovery_info.service_uuids:
                continue

            self._discovered_devices[address] = ConfigFlow.DiscoveredDevice(
                discovery_info.name, discovery_info
            )

        if not self._discovered_devices:
            return await self.async_step_manual()

        titles: list[SelectOptionDict] = []
        for address, discovery in self._discovered_devices.items():
            hub_name = self._hub_name_for(discovery.name)
            label = f"{hub_name} ({discovery.name})" if hub_name else discovery.name
            titles.append({"value": address, "label": label})

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(options=titles, multiple=True)
                    )
                }
            ),
        )

    async def async_step_auto_add(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle a shade queued from multi-select for individual setup."""
        await self.async_set_unique_id(discovery_info["address"])
        self._abort_if_unique_id_configured()

        self._device_name = discovery_info["name"]
        self._manufacturer_data_hex = discovery_info["data"]["manufacturer_data"]
        self._home_key = discovery_info["data"].get(CONF_HOME_KEY, "")
        self._hub_url = discovery_info["data"].get("hub_url", "")

        self.context["title_placeholders"] = {"name": self._device_name}
        return await self.async_step_auto_add_confirm()

    async def async_step_auto_add_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a shade discovered via multi-select."""
        if user_input is not None:
            return self._create_entry()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="auto_add_confirm",
            description_placeholders={"name": self._device_name},
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual entry of a BLE device address and name."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper().strip()
            self._device_name = user_input["ble_name"].strip()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self.context["title_placeholders"] = {"name": self._device_name}
            self._manufacturer_data_hex = ""
            return self._create_entry()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                    vol.Required("ble_name"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                }
            ),
        )

    async def async_step_homekey(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure homekey — collected before device selection."""
        errors: dict[str, str] = {}

        if user_input is not None and await self._validate_homekey_input(
            user_input, errors
        ):
            return await self.async_step_select_device()

        return self.async_show_form(
            step_id="homekey",
            data_schema=_HOMEKEY_SCHEMA,
            errors=errors,
        )
