"""Config flow for Google Cast Static IP."""

from __future__ import annotations

import logging
from typing import Any, override
from uuid import UUID

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .connection import (
    CannotConnect,
    DeviceInfoUnavailable,
    DeviceUuidMismatch,
    InvalidCastDevice,
    StaticCastDeviceInfo,
    normalize_ipv4_address,
    probe_cast_device,
)
from .const import (
    CONF_DEVICE_UUID,
    CONF_RETRY_WAIT,
    CONF_SOCKET_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_RETRY_WAIT,
    DEFAULT_SOCKET_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _host_schema(
    host: str | None = None,
    port: int | None = None,
    device_uuid: str | None = None,
) -> vol.Schema:
    """Return the host input schema."""
    host_key = (
        vol.Required(CONF_HOST, default=host) if host else vol.Required(CONF_HOST)
    )
    port_key = vol.Required(CONF_PORT, default=port or DEFAULT_PORT)
    uuid_key = (
        vol.Optional(CONF_DEVICE_UUID, default=device_uuid)
        if device_uuid
        else vol.Optional(CONF_DEVICE_UUID)
    )
    return vol.Schema(
        {
            host_key: TextSelector(TextSelectorConfig()),
            port_key: NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=65535,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            uuid_key: TextSelector(TextSelectorConfig()),
        }
    )


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_RETRY_WAIT): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=60,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_SOCKET_TIMEOUT): NumberSelector(
            NumberSelectorConfig(
                min=5,
                max=120,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
)


class GoogleCastStaticConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a direct-IP Cast device."""

    VERSION = 1

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry) -> GoogleCastStaticOptionsFlow:
        """Return the options flow."""
        return GoogleCastStaticOptionsFlow()

    async def _async_probe(
        self, user_input: dict[str, Any]
    ) -> tuple[StaticCastDeviceInfo | None, dict[str, str]]:
        """Validate user input and query the Cast device."""
        try:
            host = normalize_ipv4_address(user_input[CONF_HOST])
        except ValueError:
            return None, {CONF_HOST: "invalid_ip"}

        try:
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
        except (TypeError, ValueError):
            return None, {CONF_PORT: "invalid_port"}
        if not 1 <= port <= 65535:
            return None, {CONF_PORT: "invalid_port"}

        supplied_uuid = None
        if raw_uuid := str(user_input.get(CONF_DEVICE_UUID, "")).strip():
            try:
                supplied_uuid = UUID(raw_uuid)
            except ValueError:
                return None, {CONF_DEVICE_UUID: "invalid_uuid"}

        try:
            device = await self.hass.async_add_executor_job(
                probe_cast_device, host, port, supplied_uuid
            )
        except CannotConnect:
            return None, {"base": "cannot_connect"}
        except DeviceInfoUnavailable:
            return None, {CONF_DEVICE_UUID: "uuid_required"}
        except DeviceUuidMismatch:
            return None, {CONF_DEVICE_UUID: "uuid_mismatch"}
        except InvalidCastDevice:
            return None, {"base": "invalid_cast_device"}
        except Exception:
            _LOGGER.exception("Unexpected error while probing Cast device at %s", host)
            return None, {"base": "unknown"}

        return device, {}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up a Cast device from a literal IPv4 address."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device, errors = await self._async_probe(user_input)
            if device is not None:
                await self.async_set_unique_id(str(device.uuid))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=device.friendly_name,
                    data=device.as_config_data(),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_host_schema(),
            errors=errors,
        )

    @override
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the static IP address for an existing Cast device."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            device, errors = await self._async_probe(user_input)
            if device is not None:
                if entry.unique_id != str(device.uuid):
                    errors["base"] = "different_device"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data=device.as_config_data(),
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_host_schema(
                entry.data[CONF_HOST],
                entry.data.get(CONF_PORT, DEFAULT_PORT),
                entry.data[CONF_DEVICE_UUID],
            ),
            errors=errors,
        )


class GoogleCastStaticOptionsFlow(OptionsFlowWithReload):
    """Configure reconnect behavior."""

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage connection options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        suggested = {
            CONF_RETRY_WAIT: self.config_entry.options.get(
                CONF_RETRY_WAIT, DEFAULT_RETRY_WAIT
            ),
            CONF_SOCKET_TIMEOUT: self.config_entry.options.get(
                CONF_SOCKET_TIMEOUT, DEFAULT_SOCKET_TIMEOUT
            ),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, suggested),
        )
