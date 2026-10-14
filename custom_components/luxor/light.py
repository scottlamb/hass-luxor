import logging

from datetime import timedelta
from functools import partial

from dataclasses import dataclass

from homeassistant.core import callback
from homeassistant.components.light import (
    ColorMode,
    LightEntity,
)
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

import luxor_openapi_asyncio
from luxor_openapi_asyncio.api import groups_api

from .const import (
    DEFAULT_NAME,
    DOMAIN,
    LIGHT,
    REQUEST_REFRESH_DELAY,
    CONF_HOST,
    CONF_GROUP_INTERVAL,
    DEFAULT_GROUP_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class LuxorGroup:
    grp: int
    name: str
    inten: int


def _field(raw, *keys):
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _parse_group(raw):
    """Parse a group from either controller dialect.

    ZDC-style controllers send Grp/Inten/Colr; ZD-style controllers send
    GroupNumber/Intensity. luxor-openapi-asyncio's Group model only knows the
    former, so this bypasses it and reads the raw JSON.
    """
    grp = _field(raw, "Grp", "GroupNumber")
    if grp is None:
        _LOGGER.warning("Ignoring Luxor group with no group number: %s", raw)
        return None
    name = _field(raw, "Name") or "Group {}".format(grp)
    inten = _field(raw, "Inten", "Intensity") or 0
    return LuxorGroup(grp=int(grp), name=name, inten=int(inten))

async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]

    light_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="{}_{}".format(DOMAIN, LIGHT),
        update_method=partial(
            async_fetch_lights,
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            controller,
        ),
        update_interval=timedelta(seconds=entry.data.get(CONF_GROUP_INTERVAL, DEFAULT_GROUP_INTERVAL)),
        request_refresh_debouncer=Debouncer(
            hass, _LOGGER, cooldown=REQUEST_REFRESH_DELAY, immediate=True
        ),
    )
    await light_coordinator.async_refresh()

    if not light_coordinator.last_update_success:
        raise PlatformNotReady

    light_coordinator.async_add_listener(
        partial(
            async_update_lights,
            hass,
            controller,
            {},
            async_add_entities,
            partial(LuxorLight, light_coordinator, controller),
        )
    )
    await light_coordinator.async_refresh()


async def async_fetch_lights(session, host, controller):
    async with session.post(
        "http://{}/GroupListGet.json".format(host), json={}
    ) as resp:
        resp.raise_for_status()
        # The controller may not label the response as JSON.
        body = await resp.json(content_type=None)
    if body.get("Status", 0) != 0:
        raise UpdateFailed("GroupListGet returned status {}".format(body.get("Status")))
    groups = [_parse_group(raw) for raw in body.get("GroupList") or []]
    controller.lights = {g.grp: g for g in groups if g is not None}


@callback
def async_update_lights(
    hass, controller, current_entities, async_add_entities, create_light
):
    new_items = []
    for grp_id, data in controller.lights.items():
        if grp_id in current_entities:
            continue
        light = current_entities[grp_id] = create_light(data.grp)
        new_items.append(light)

    hass.async_create_task(async_remove_entities(controller, current_entities))

    # add new entities
    async_add_entities(new_items)


async def async_remove_entities(controller, current_entities):
    # clean up entities which are no longer present
    for grp_id, entity in list(current_entities.items()):
        if grp_id not in controller.lights:
            await entity.async_remove(force_remove=True)
            del current_entities[grp_id]


def intensity_to_brightness(intensity: int):
    return round(255 * (intensity / 100))


def brightness_to_intensity(brightness: int):
    return round(100 * (1 - (255 - brightness) / 255))


class LuxorLight(CoordinatorEntity, LightEntity):

    def __init__(self, coordinator, controller, group_id):
        super().__init__(coordinator)
        self.controller = controller
        self.group_id = group_id
        self._attr_unique_id = "LUXOR_LIGHT_{}".format(group_id)
        color_mode = ColorMode.BRIGHTNESS
        self._attr_color_mode = color_mode
        self._attr_supported_color_modes = {color_mode}
        self._attr_effect = None

    async def async_turn_on(
        self, brightness=255, **kwargs
    ):  # pylint: disable=unused-argument
        intensity = brightness_to_intensity(brightness)

        api_instance = groups_api.GroupsApi(self.controller.api)
        api_response = await api_instance.illuminate_group(
            luxor_openapi_asyncio.IlluminateGroupRequest(self.group_id, intensity)
        )

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        api_instance = groups_api.GroupsApi(self.controller.api)
        api_response = await api_instance.illuminate_group(
            luxor_openapi_asyncio.IlluminateGroupRequest(self.group_id, 0)
        )

        await self.coordinator.async_request_refresh()

    @property
    def name(self):
        return self.controller.lights[self.group_id].name

    @property
    def brightness(self):
        return intensity_to_brightness(self.controller.lights[self.group_id].inten)

    @property
    def is_on(self):
        return self.controller.lights[self.group_id].inten > 0

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={("{}_{}".format(DOMAIN, LIGHT), self.group_id)},
            manufacturer="FXLuminaire",
            name=self.name,
            via_device=(DOMAIN, self.controller.name),
        )
