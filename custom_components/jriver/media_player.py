"""Support for interfacing with the JRiver MCWS API."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from hamcws import MediaServer, PlaybackInfo, PlaybackState
import voluptuous as vol

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    PLATFORM_SCHEMA,
    BrowseError,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_TIMEOUT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import BrowsePath, MediaServerUpdateCoordinator, _translate_to_media_type
from .browse_media import browse_nodes, media_source_content_filter
from .const import (
    CONF_BROWSE_PATHS,
    CONF_DEVICE_PER_ZONE,
    CONF_DEVICE_ZONES,
    DATA_BROWSE_PATHS,
    DATA_COORDINATOR,
    DATA_MEDIA_SERVER,
    DATA_ZONES,
    DEFAULT_DEVICE_PER_ZONE,
    DEFAULT_PORT,
    DEFAULT_SSL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .entity import MediaServerEntity, cmd

_LOGGER = logging.getLogger(__name__)

CONF_TCP_PORT = "tcp_port"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_SSL, default=DEFAULT_SSL): cv.boolean,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
        vol.Inclusive(CONF_USERNAME, "auth"): cv.string,
        vol.Inclusive(CONF_PASSWORD, "auth"): cv.string,
        vol.Optional(CONF_DEVICE_PER_ZONE, default=DEFAULT_DEVICE_PER_ZONE): cv.boolean,
        vol.Optional(CONF_DEVICE_ZONES): cv.ensure_list,
        vol.Optional(CONF_BROWSE_PATHS): cv.ensure_list,
    }
)

SERVICE_ADD_MEDIA = "add_to_playlist"

ATTR_PLAYLIST_PATH = "playlist_path"
ATTR_QUERY = "play_query"

MC_ADD_MEDIA_SCHEMA = {
    vol.Optional(ATTR_PLAYLIST_PATH): cv.string,
    vol.Optional(ATTR_QUERY): cv.string,
}


SERVICE_ACTIVATE_ZONE = "activate_zone"

ATTR_ZONE_NAME = "zone_name"

MC_ACTIVATE_ZONE_SCHEMA = {
    vol.Required(ATTR_ZONE_NAME): cv.string,
}


SERVICE_SEND_MCC = "send_mcc"

ATTR_MCC_COMMAND = "command"
ATTR_MCC_PARAMETER = "parameter"
ATTR_MCC_BLOCK = "block"
ATTR_ZONE_NAME = "zone_name"

MC_SEND_MCC_SCHEMA = {
    vol.Required(ATTR_MCC_COMMAND): vol.All(
        vol.Coerce(int), vol.Range(min=10000, max=40000)
    ),
    vol.Optional(ATTR_MCC_PARAMETER): vol.Coerce(int),
    vol.Optional(ATTR_MCC_BLOCK): cv.boolean,
    vol.Optional(ATTR_ZONE_NAME): cv.string,
}


def find_matching_config_entries_for_host(hass, host):
    """Search existing config entries for one matching the host."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data[CONF_HOST] == host:
            return entry
    return None


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the JRiver platform."""
    host = config[CONF_HOST]
    if find_matching_config_entries_for_host(hass, host):
        return

    entry_data = {
        CONF_NAME: config.get(CONF_NAME, host),
        CONF_HOST: host,
        CONF_PORT: config.get(CONF_PORT),
        CONF_USERNAME: config.get(CONF_USERNAME),
        CONF_PASSWORD: config.get(CONF_PASSWORD),
        CONF_SSL: config.get(CONF_SSL),
        CONF_TIMEOUT: config.get(CONF_TIMEOUT),
    }

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=entry_data
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the JRiver Media Center media player platform."""
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_ADD_MEDIA, MC_ADD_MEDIA_SCHEMA, "async_add_media_to_playlist"
    )
    platform.async_register_entity_service(
        SERVICE_ACTIVATE_ZONE, MC_ACTIVATE_ZONE_SCHEMA, "async_activate_zone"
    )
    platform.async_register_entity_service(
        SERVICE_SEND_MCC, MC_SEND_MCC_SCHEMA, "async_send_mcc"
    )

    data = hass.data[DOMAIN][config_entry.entry_id]
    ms = data[DATA_MEDIA_SERVER]
    name = f"{config_entry.data[CONF_HOST]} Player"
    unique_id = f"{config_entry.unique_id or config_entry.entry_id}_player"
    zones = data[DATA_ZONES]
    browse_paths = data[DATA_BROWSE_PATHS]
    coordinator = data[DATA_COORDINATOR]
    if zones:
        entities = [
            JRiverMediaPlayer(
                coordinator, ms, f"{name} - {z}", f"{unique_id}-{z}", browse_paths, z
            )
            for z in zones
        ]
    else:
        entities = [JRiverMediaPlayer(coordinator, ms, name, unique_id, browse_paths)]
    async_add_entities(entities)


class JRiverMediaPlayer(MediaServerEntity, MediaPlayerEntity):
    """Representation of a JRiver Media Server."""

    _attr_name = None
    _attr_media_image_remotely_accessible = True
    _attr_supported_features = (
        MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.SEEK
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.CLEAR_PLAYLIST
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.SHUFFLE_SET
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.REPEAT_SET
    )

    def __init__(
        self,
        coordinator: MediaServerUpdateCoordinator,
        media_server: MediaServer,
        name,
        uid: str,
        browse_paths: list[str],
        zone_name: str | None = None,
    ) -> None:
        """Initialize the MediaServer entity."""
        super().__init__(coordinator, uid, name)
        self._media_server: MediaServer = media_server
        self._playback_info: PlaybackInfo | None = None
        self._position_updated_at: dt.datetime | None = None
        self._browse_paths = [BrowsePath(bp) for bp in browse_paths]
        self._target_zone: str | None = zone_name

    def _reset_state(self):
        self._position_updated_at = None
        self._playback_info = None

    async def _clear_connection(self, close=True):
        self._reset_state()
        self.async_write_ha_state()
        if close:
            await self._media_server.close()

    @property
    def state(self) -> MediaPlayerState:
        """Return the state of the device."""
        if not self._playback_info:
            return MediaPlayerState.OFF

        if self._playback_info.state in [PlaybackState.STOPPED, PlaybackState.WAITING]:
            return MediaPlayerState.IDLE

        if self._playback_info.state == PlaybackState.PAUSED:
            return MediaPlayerState.PAUSED

        return MediaPlayerState.PLAYING

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._position_updated_at = self.coordinator.data.get_position_updated_at(
            self._target_zone
        )
        self._playback_info = self.coordinator.data.get_playback_info(self._target_zone)
        self.async_write_ha_state()

    @property
    def volume_level(self) -> float | None:
        """Volume level of the media player (0..1)."""
        return self._playback_info.volume if self._playback_info else None

    @property
    def is_volume_muted(self) -> bool | None:
        """Boolean if volume is currently muted."""
        return self._playback_info.muted if self._playback_info else None

    @property
    def media_content_id(self) -> str | None:
        """Content ID of current playing media."""
        return (
            str(self._playback_info.file_key)
            if self._playback_info and self._playback_info != -1
            else None
        )

    @property
    def media_content_type(self) -> MediaType | str | None:
        """Content type of current playing media, if any."""
        return (
            _translate_to_media_type(
                self._playback_info.media_type, self._playback_info.media_sub_type
            )
            if self._playback_info
            else None
        )

    @property
    def media_duration(self) -> int | None:
        """Duration of current playing media in seconds."""
        if not self._playback_info:
            return None

        if self._playback_info.live_input:
            return None

        if not self._playback_info.duration_ms or self._playback_info.duration_ms < 0:
            return None

        return self._playback_info.duration_ms * 1000

    @property
    def media_position(self) -> int | None:
        """Position of current playing media in seconds."""
        if not self._playback_info:
            return None

        if self._playback_info.live_input:
            return None

        if not self._playback_info.position_ms or self._playback_info.position_ms < 0:
            return None

        return self._playback_info.position_ms * 1000

    @property
    def media_position_updated_at(self) -> dt.datetime | None:
        """Last valid time of media position."""
        return self._position_updated_at

    @property
    def media_image_url(self) -> str | None:
        """Image url of current playing media."""
        if not self._playback_info:
            return None

        if not self._playback_info.image_url:
            return None

        return self._media_server.make_url(self._playback_info.image_url)

    @property
    def media_title(self) -> str | None:
        """Title of current playing media."""
        if not self._playback_info:
            return None

        return self._playback_info.name

    @property
    def media_series_title(self) -> str | None:
        """Title of series of current playing media, TV show only."""
        if not self._playback_info:
            return None

        return self._playback_info.series

    @property
    def media_season(self) -> str | None:
        """Season of current playing media, TV show only."""
        if not self._playback_info:
            return None

        return self._playback_info.season

    @property
    def media_episode(self) -> str | None:
        """Episode of current playing media, TV show only."""
        if not self._playback_info:
            return None

        return self._playback_info.episode

    @property
    def media_album_name(self) -> str | None:
        """Album name of current playing media, music track only."""
        if not self._playback_info:
            return None

        return self._playback_info.album

    @property
    def media_artist(self) -> str | None:
        """Artist of current playing media, music track only."""
        if not self._playback_info:
            return None

        return self._playback_info.artist

    @property
    def media_album_artist(self):
        """Album artist of current playing media, music track only."""
        if not self._playback_info:
            return None

        return self._playback_info.album_artist

    @cmd
    async def async_volume_up(self) -> None:
        """Volume up the media player."""
        await self._media_server.volume_up(zone=self._target_zone)

    @cmd
    async def async_volume_down(self) -> None:
        """Volume down the media player."""
        await self._media_server.volume_down(zone=self._target_zone)

    @cmd
    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        await self._media_server.set_volume_level(volume, zone=self._target_zone)

    @cmd
    async def async_mute_volume(self, mute: bool) -> None:
        """Mute (true) or unmute (false) media player."""
        await self._media_server.mute(mute, zone=self._target_zone)

    @cmd
    async def async_media_play_pause(self) -> None:
        """Pause media on media player."""
        await self._media_server.play_pause(zone=self._target_zone)

    @cmd
    async def async_media_play(self) -> None:
        """Play media."""
        await self._media_server.play(zone=self._target_zone)

    @cmd
    async def async_media_pause(self) -> None:
        """Pause the media player."""
        await self._media_server.pause(zone=self._target_zone)

    @cmd
    async def async_media_stop(self) -> None:
        """Stop the media player."""
        await self._media_server.stop(zone=self._target_zone)

    @cmd
    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self._media_server.next_track(zone=self._target_zone)

    @cmd
    async def async_media_previous_track(self) -> None:
        """Send next track command."""
        await self._media_server.previous_track(zone=self._target_zone)

    @cmd
    async def async_media_seek(self, position: float) -> None:
        """Send seek command to a position specified in seconds."""
        await self._media_server.media_seek(
            int(position * 1000), zone=self._target_zone
        )

    @cmd
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Send the play_media command to the media player."""
        if media_source.is_media_source_id(media_id):
            media_type = MediaType.URL
            play_item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = play_item.url

        media_type_lower = media_type.lower()

        if media_type_lower == MediaType.PLAYLIST:
            await self._media_server.play_playlist(media_id, zone=self._target_zone)
        elif media_type_lower == "file":
            await self._media_server.play_file(media_id, zone=self._target_zone)
        elif media_type_lower in [
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.MOVIE,
            MediaType.EPISODE,
            MediaType.SEASON,
            MediaType.TVSHOW,
        ]:
            await self.async_clear_playlist()
            if media_id[:2] == "N|":
                _, node_id, _ = media_id.split("|", 3)
                await self._media_server.play_browse_files(
                    int(node_id), zone=self._target_zone
                )
            elif media_id[:2] == "K|":
                await self._media_server.play_item(media_id[2:], zone=self._target_zone)
            else:
                raise ValueError(f"Unknown media id {media_id}")
        else:
            media_id = async_process_play_media_url(self.hass, media_id)

            await self._media_server.play_file(media_id, zone=self._target_zone)

    @cmd
    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Set shuffle mode."""
        await self._media_server.set_shuffle(shuffle, zone=self._target_zone)

    @cmd
    async def async_clear_playlist(self) -> None:
        """Clear default playlist."""
        await self._media_server.clear_playlist(zone=self._target_zone)

    @cmd
    async def async_add_media_to_playlist(
        self, query: str | None, playlist_path: str | None
    ):
        """Add the results of a query to playing now or plays a playlist.

        Used by the exposed service "add_to_playlist"
        """
        if query:
            await self._media_server.play_search(query, zone=self._target_zone)
            return
        if playlist_path:
            await self._media_server.play_playlist(
                playlist_path, zone=self._target_zone
            )
            return

        _LOGGER.warning(
            "Service add_to_playlist requires either query or playlist_path to be set"
        )

    @cmd
    async def async_activate_zone(self, zone_name: str):
        """Activate the named zone."""
        await self._media_server.set_active_zone(zone_name)

    @cmd
    async def async_send_mcc(
        self,
        command: int,
        parameter: int | None = None,
        block: bool = True,
        zone_name: str | None = None,
    ):
        """Send an MCC command."""
        await self._media_server.send_mcc(
            command, param=parameter, block=block, zone=zone_name
        )

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Return a BrowseMedia instance."""

        if not media_content_type:
            card, _ = await browse_nodes(
                self.hass, self._media_server, self._browse_paths
            )
            return card

        if media_content_id and media_content_type:
            if media_source.is_media_source_id(media_content_id):
                return await media_source.async_browse_media(
                    self.hass,
                    media_content_id,
                    content_filter=media_source_content_filter,
                )
            card, has_mc_nodes = await browse_nodes(
                self.hass,
                self._media_server,
                self._browse_paths,
                parent_content_type=media_content_type,
                parent_id=media_content_id,
            )
            if has_mc_nodes:
                return card
        raise BrowseError(f"Media not found: {media_content_type} / {media_content_id}")
