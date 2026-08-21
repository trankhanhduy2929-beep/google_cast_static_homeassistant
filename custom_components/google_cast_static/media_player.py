"""Media player for a Google Cast device connected by static IP."""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial
from typing import Any, override

import pychromecast
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_EXTRA,
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util
from pychromecast.config import APP_MEDIA_RECEIVER
from pychromecast.controllers.media import (
    MEDIA_PLAYER_STATE_BUFFERING,
    MEDIA_PLAYER_STATE_PLAYING,
    MediaStatus,
)
from pychromecast.controllers.receiver import (
    VOLUME_CONTROL_TYPE_FIXED,
    CastStatus,
    LaunchFailure,
)
from pychromecast.error import PyChromecastError
from pychromecast.quick_play import quick_play
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    ConnectionStatus,
)

from .connection import create_chromecast
from .const import (
    CONF_DEVICE_UUID,
    CONF_FRIENDLY_NAME,
    CONF_MANUFACTURER,
    CONF_MODEL_NAME,
    CONF_RETRY_WAIT,
    CONF_SOCKET_TIMEOUT,
    CONNECTION_METHOD,
    DEFAULT_PORT,
    DEFAULT_RETRY_WAIT,
    DEFAULT_SOCKET_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the static-IP Cast media player."""
    async_add_entities([GoogleCastStaticMediaPlayer(entry)])


class CastStatusForwarder:
    """Forward PyChromecast worker-thread callbacks to the entity."""

    def __init__(self, entity: GoogleCastStaticMediaPlayer) -> None:
        """Initialize the callback forwarder."""
        self._entity = entity
        self._active = True

    def invalidate(self) -> None:
        """Ignore callbacks after entity removal."""
        self._active = False

    def _submit(self, target, *args) -> None:
        """Forward a worker-thread callback to the entity."""
        if not self._active:
            return
        target(*args)

    def new_cast_status(self, status: CastStatus) -> None:
        """Receive a receiver status update."""
        self._submit(self._entity._handle_cast_status, status)

    def new_media_status(self, status: MediaStatus) -> None:
        """Receive a media status update."""
        self._submit(self._entity._handle_media_status, status)

    def new_connection_status(self, status: ConnectionStatus) -> None:
        """Receive a socket connection status update."""
        self._submit(self._entity._handle_connection_status, status)

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        """Receive a media load failure."""
        self._submit(self._entity._handle_media_failure, queue_item_id, error_code)

    def new_launch_error(self, status: LaunchFailure) -> None:
        """Receive an application launch failure."""
        self._submit(self._entity._handle_launch_failure, status)


class GoogleCastStaticMediaPlayer(MediaPlayerEntity):
    """Representation of a Google Cast speaker reached by static IP."""

    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_has_entity_name = True
    _attr_media_image_remotely_accessible = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the media player."""
        self._entry = entry
        self._chromecast: pychromecast.Chromecast | None = None
        self._listener: CastStatusForwarder | None = None
        self._cast_status: CastStatus | None = None
        self._media_status: MediaStatus | None = None
        self._media_status_received: datetime | None = None
        self._connection_status = "DISCONNECTED"
        self._removed = False

        uuid = entry.data[CONF_DEVICE_UUID]
        friendly_name = entry.data[CONF_FRIENDLY_NAME]
        self._attr_unique_id = uuid
        self._attr_available = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uuid.replace("-", ""))},
            manufacturer=entry.data.get(CONF_MANUFACTURER, "Google"),
            model=entry.data.get(CONF_MODEL_NAME, "Google Cast"),
            name=friendly_name,
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Create and start the persistent direct-IP Cast connection."""
        await super().async_added_to_hass()
        retry_wait = float(self._entry.options.get(CONF_RETRY_WAIT, DEFAULT_RETRY_WAIT))
        socket_timeout = float(
            self._entry.options.get(CONF_SOCKET_TIMEOUT, DEFAULT_SOCKET_TIMEOUT)
        )
        chromecast = create_chromecast(
            dict(self._entry.data),
            retry_wait=retry_wait,
            socket_timeout=socket_timeout,
        )
        listener = CastStatusForwarder(self)
        chromecast.register_status_listener(listener)
        chromecast.media_controller.register_status_listener(listener)
        chromecast.register_connection_listener(listener)
        chromecast.register_launch_error_listener(listener)
        self._chromecast = chromecast
        self._listener = listener
        chromecast.start()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop the Cast socket worker."""
        self._removed = True
        if self._listener is not None:
            self._listener.invalidate()
            self._listener = None

        chromecast = self._chromecast
        self._chromecast = None
        self._attr_available = False
        if chromecast is not None:
            try:
                await self.hass.async_add_executor_job(chromecast.disconnect, 10)
            except TimeoutError:
                _LOGGER.warning(
                    "Timed out while stopping Cast connection to %s",
                    self._entry.data[CONF_HOST],
                )
        await super().async_will_remove_from_hass()

    def _require_chromecast(self) -> pychromecast.Chromecast:
        """Return the active Chromecast or raise a Home Assistant error."""
        if self._chromecast is None or not self.available:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_connected",
            )
        return self._chromecast

    async def _async_run(self, command, *args, **kwargs) -> Any:
        """Run a blocking PyChromecast command in the executor."""
        try:
            return await self.hass.async_add_executor_job(
                partial(command, *args, **kwargs)
            )
        except PyChromecastError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    def _handle_cast_status(self, status: CastStatus) -> None:
        """Handle receiver state on the Home Assistant event loop."""
        if self._removed:
            return
        self._cast_status = status
        self._attr_volume_level = status.volume_level
        self._attr_is_volume_muted = status.volume_muted
        self.schedule_update_ha_state()

    def _handle_media_status(self, status: MediaStatus) -> None:
        """Handle media state on the Home Assistant event loop."""
        if self._removed:
            return
        self._media_status = status
        self._media_status_received = dt_util.utcnow()
        self.schedule_update_ha_state()

    def _handle_connection_status(self, status: ConnectionStatus) -> None:
        """Handle socket availability without discarding the reconnecting client."""
        if self._removed:
            return
        new_available = status.status == CONNECTION_STATUS_CONNECTED
        changed = (
            new_available != self.available or status.status != self._connection_status
        )
        self._connection_status = status.status
        self._attr_available = new_available
        if not new_available:
            self._cast_status = None
            self._media_status = None
            self._media_status_received = None
            self._attr_volume_level = None
            self._attr_is_volume_muted = None
        if changed:
            self.schedule_update_ha_state()

    def _handle_media_failure(self, queue_item_id: int, error_code: int) -> None:
        """Log a media load failure."""
        _LOGGER.warning(
            "Cast device %s failed to load queue item %s (error %s)",
            self._entry.title,
            queue_item_id,
            error_code,
        )

    def _handle_launch_failure(self, status: LaunchFailure) -> None:
        """Log an application launch failure."""
        _LOGGER.warning(
            "Cast device %s failed to launch app %s: %s",
            self._entry.title,
            status.app_id,
            status.reason,
        )

    @property
    @override
    def state(self) -> MediaPlayerState | None:
        """Return the media player state."""
        if not self.available or self._cast_status is None:
            return None

        media_status = self._media_status
        if media_status is not None:
            if media_status.player_state == MEDIA_PLAYER_STATE_PLAYING:
                return MediaPlayerState.PLAYING
            if media_status.player_state == MEDIA_PLAYER_STATE_BUFFERING:
                return MediaPlayerState.BUFFERING
            if media_status.player_is_paused:
                return MediaPlayerState.PAUSED
            if media_status.player_is_idle:
                return MediaPlayerState.IDLE

        if self.app_id in (pychromecast.IDLE_APP_ID, None):
            return MediaPlayerState.OFF
        return MediaPlayerState.IDLE

    @property
    @override
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Return supported media player features."""
        support = (
            MediaPlayerEntityFeature.BROWSE_MEDIA
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
        )

        if (
            self._cast_status is not None
            and self._cast_status.volume_control_type != VOLUME_CONTROL_TYPE_FIXED
        ):
            support |= (
                MediaPlayerEntityFeature.VOLUME_MUTE
                | MediaPlayerEntityFeature.VOLUME_SET
            )

        if self._media_status is not None:
            support |= MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.STOP
            if self._media_status.supports_pause:
                support |= MediaPlayerEntityFeature.PAUSE
            if self._media_status.supports_seek:
                support |= MediaPlayerEntityFeature.SEEK
            if self._media_status.supports_queue_next:
                support |= MediaPlayerEntityFeature.NEXT_TRACK
            if self._media_status.supports_queue_prev:
                support |= MediaPlayerEntityFeature.PREVIOUS_TRACK

        return support

    @property
    @override
    def app_id(self) -> str | None:
        """Return the active Cast application ID."""
        return self._chromecast.app_id if self._chromecast else None

    @property
    @override
    def app_name(self) -> str | None:
        """Return the active Cast application name."""
        return self._chromecast.app_display_name if self._chromecast else None

    @property
    @override
    def media_content_id(self) -> str | None:
        """Return the current media content ID."""
        return self._media_status.content_id if self._media_status else None

    @property
    @override
    def media_content_type(self) -> MediaType | str | None:
        """Return the current media content type."""
        if self._media_status is None:
            return None
        return self._media_status.content_type or MediaType.MUSIC

    @property
    @override
    def media_duration(self) -> float | None:
        """Return current media duration."""
        return self._media_status.duration if self._media_status else None

    @property
    @override
    def media_position(self) -> float | None:
        """Return current media position."""
        return self._media_status.current_time if self._media_status else None

    @property
    @override
    def media_position_updated_at(self) -> datetime | None:
        """Return when the media position was last updated."""
        return self._media_status_received

    @property
    @override
    def media_title(self) -> str | None:
        """Return current media title."""
        return self._media_status.title if self._media_status else None

    @property
    @override
    def media_artist(self) -> str | None:
        """Return current media artist."""
        return self._media_status.artist if self._media_status else None

    @property
    @override
    def media_album_name(self) -> str | None:
        """Return current album name."""
        return self._media_status.album_name if self._media_status else None

    @property
    @override
    def media_image_url(self) -> str | None:
        """Return current media artwork URL."""
        if self._media_status is None:
            return None
        images = self._media_status.images
        return images[0].url if images and images[0].url else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return direct connection diagnostics."""
        return {
            "connection_method": CONNECTION_METHOD,
            "connection_status": self._connection_status,
            "host": self._entry.data[CONF_HOST],
            "port": self._entry.data.get(CONF_PORT, DEFAULT_PORT),
        }

    @override
    async def async_turn_on(self) -> None:
        """Start the default media receiver when the speaker is idle."""
        chromecast = self._require_chromecast()
        if chromecast.is_idle:
            await self._async_run(chromecast.start_app, APP_MEDIA_RECEIVER)

    @override
    async def async_turn_off(self) -> None:
        """Stop the active Cast application."""
        await self._async_run(self._require_chromecast().quit_app)

    @override
    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute the speaker."""
        await self._async_run(self._require_chromecast().set_volume_muted, mute)

    @override
    async def async_set_volume_level(self, volume: float) -> None:
        """Set speaker volume from 0 to 1."""
        await self._async_run(self._require_chromecast().set_volume, volume)

    @override
    async def async_media_play(self) -> None:
        """Resume playback."""
        await self._async_run(self._require_chromecast().media_controller.play)

    @override
    async def async_media_pause(self) -> None:
        """Pause playback."""
        await self._async_run(self._require_chromecast().media_controller.pause)

    @override
    async def async_media_stop(self) -> None:
        """Stop playback."""
        await self._async_run(self._require_chromecast().media_controller.stop)

    @override
    async def async_media_seek(self, position: float) -> None:
        """Seek to a playback position."""
        await self._async_run(
            self._require_chromecast().media_controller.seek, position
        )

    @override
    async def async_media_next_track(self) -> None:
        """Skip to the next queue item."""
        await self._async_run(self._require_chromecast().media_controller.queue_next)

    @override
    async def async_media_previous_track(self) -> None:
        """Skip to the previous queue item."""
        await self._async_run(self._require_chromecast().media_controller.queue_prev)

    @override
    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse Home Assistant media sources."""
        return await media_source.async_browse_media(self.hass, media_content_id)

    @override
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media with the Cast default media receiver."""
        if media_source.is_media_source_id(media_id):
            sourced_media = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_type = sourced_media.mime_type
            media_id = sourced_media.url

        media_id = async_process_play_media_url(self.hass, media_id)
        extra = kwargs.get(ATTR_MEDIA_EXTRA) or {}
        cast_media_type = str(media_type)
        if "/" not in cast_media_type:
            cast_media_type = {
                "image": "image/jpeg",
                "music": "audio/mpeg",
                "track": "audio/mpeg",
                "podcast": "audio/mpeg",
                "movie": "video/mp4",
                "tvshow": "video/mp4",
                "video": "video/mp4",
            }.get(cast_media_type, "audio/mpeg")
        app_data = {
            "media_id": media_id,
            "media_type": cast_media_type,
            **extra,
        }
        await self._async_run(
            quick_play,
            self._require_chromecast(),
            "default_media_receiver",
            app_data,
        )
