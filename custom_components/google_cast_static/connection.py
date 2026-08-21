"""Direct-IP connection helpers for Google Cast devices."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from ipaddress import AddressValueError, IPv4Address
from typing import Any
from uuid import UUID

import pychromecast
from homeassistant.const import CONF_HOST, CONF_PORT
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_GROUP
from pychromecast.dial import get_device_info
from pychromecast.models import CastInfo, HostServiceInfo

from .const import (
    CONF_CAST_TYPE,
    CONF_DEVICE_UUID,
    CONF_FRIENDLY_NAME,
    CONF_MANUFACTURER,
    CONF_MODEL_NAME,
    DEFAULT_PORT,
    DEVICE_INFO_TIMEOUT,
    SOCKET_PROBE_TIMEOUT,
)


class CannotConnect(Exception):
    """Raised when a Cast device cannot be reached."""


class InvalidCastDevice(Exception):
    """Raised when the target does not provide valid Cast device information."""


class DeviceInfoUnavailable(Exception):
    """Raised when DIAL information is unavailable and no UUID was supplied."""


class DeviceUuidMismatch(Exception):
    """Raised when the supplied UUID does not match the discovered device."""


@dataclass(frozen=True, slots=True)
class StaticCastDeviceInfo:
    """Stored information required to reconnect without discovery."""

    host: str
    port: int
    uuid: UUID
    friendly_name: str
    model_name: str
    manufacturer: str
    cast_type: str

    def as_config_data(self) -> dict[str, Any]:
        """Return serializable config-entry data."""
        return {
            CONF_HOST: self.host,
            CONF_PORT: self.port,
            CONF_DEVICE_UUID: str(self.uuid),
            CONF_FRIENDLY_NAME: self.friendly_name,
            CONF_MODEL_NAME: self.model_name,
            CONF_MANUFACTURER: self.manufacturer,
            CONF_CAST_TYPE: self.cast_type,
        }


def normalize_ipv4_address(value: str) -> str:
    """Validate and normalize a literal IPv4 address."""
    try:
        return str(IPv4Address(value.strip()))
    except (AddressValueError, AttributeError) as err:
        raise ValueError("A literal IPv4 address is required") from err


def probe_cast_device(
    host: str,
    port: int = DEFAULT_PORT,
    supplied_uuid: UUID | None = None,
) -> StaticCastDeviceInfo:
    """Validate a Cast socket and fetch stable device information by IP."""
    host = normalize_ipv4_address(host)

    try:
        with socket.create_connection((host, port), timeout=SOCKET_PROBE_TIMEOUT):
            pass
    except OSError as err:
        raise CannotConnect from err

    status = get_device_info(host, timeout=DEVICE_INFO_TIMEOUT)
    if status is None:
        if supplied_uuid is None:
            raise DeviceInfoUnavailable
        return StaticCastDeviceInfo(
            host=host,
            port=port,
            uuid=supplied_uuid,
            friendly_name=f"Google Cast {host}",
            model_name="Google Cast (manual UUID)",
            manufacturer="Google",
            cast_type=CAST_TYPE_GROUP if port != DEFAULT_PORT else CAST_TYPE_AUDIO,
        )

    if (
        status.uuid is not None
        and supplied_uuid is not None
        and status.uuid != supplied_uuid
    ):
        raise DeviceUuidMismatch

    device_uuid = status.uuid or supplied_uuid
    if device_uuid is None:
        raise InvalidCastDevice

    return StaticCastDeviceInfo(
        host=host,
        port=port,
        uuid=device_uuid,
        friendly_name=status.friendly_name or f"Google Cast {host}",
        model_name=status.model_name or "Google Cast",
        manufacturer=status.manufacturer or "Google",
        cast_type=status.cast_type,
    )


def build_cast_info(data: dict[str, Any]) -> CastInfo:
    """Build CastInfo containing only a direct host service."""
    host = normalize_ipv4_address(data[CONF_HOST])
    port = int(data.get(CONF_PORT, DEFAULT_PORT))
    uuid = UUID(data[CONF_DEVICE_UUID])

    return CastInfo(
        services={HostServiceInfo(host, port)},
        uuid=uuid,
        model_name=data.get(CONF_MODEL_NAME),
        friendly_name=data.get(CONF_FRIENDLY_NAME),
        host=host,
        port=port,
        cast_type=data.get(CONF_CAST_TYPE),
        manufacturer=data.get(CONF_MANUFACTURER),
    )


def create_chromecast(
    data: dict[str, Any], *, retry_wait: float, socket_timeout: float
) -> pychromecast.Chromecast:
    """Create a Chromecast that reconnects forever using its static IP."""
    return pychromecast.get_chromecast_from_cast_info(
        build_cast_info(data),
        None,
        tries=None,
        retry_wait=retry_wait,
        timeout=socket_timeout,
    )
