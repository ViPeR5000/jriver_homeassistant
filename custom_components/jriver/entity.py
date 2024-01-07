"""MediaServer entity base."""
from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
import logging
from typing import Any, Concatenate, ParamSpec, TypeVar

from auth import InvalidAuthError
from components.jriver import DOMAIN, MediaServerUpdateCoordinator
from hamcws import CannotConnectError
from helpers.device_registry import DeviceInfo
from helpers.update_coordinator import CoordinatorEntity

_LOGGER = logging.getLogger(__name__)


class MediaServerEntity(CoordinatorEntity[MediaServerUpdateCoordinator]):
    """MediaServer entity class."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MediaServerUpdateCoordinator,
        unique_id: str | None,
        name: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            manufacturer="JRiver",
            model=f"Media Server - {coordinator.data.server_info.platform}",
            sw_version=coordinator.data.server_info.version,
            name=name,
        )


_MediaServerEntityT = TypeVar("_MediaServerEntityT", bound="MediaServerEntity")
_P = ParamSpec("_P")


def cmd(
    func: Callable[Concatenate[_MediaServerEntityT, _P], Awaitable[Any]],
) -> Callable[Concatenate[_MediaServerEntityT, _P], Coroutine[Any, Any, None]]:
    """Catch command exceptions."""

    @wraps(func)
    async def wrapper(
        obj: _MediaServerEntityT, *args: _P.args, **kwargs: _P.kwargs
    ) -> None:
        """Wrap all command methods."""
        try:
            await func(obj, *args, **kwargs)
            await obj.coordinator.async_request_refresh()
        except (CannotConnectError, InvalidAuthError) as exc:
            _LOGGER.error(
                "Error calling %s on entity %s: %r",
                func.__name__,
                obj.entity_id,
                exc,
            )

    return wrapper
