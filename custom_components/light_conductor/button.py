"""Per-room calibration button (§4.4/§10).

Only rooms with a lux sensor get one; pressing it submits a
:class:`StartCalibration` event. The engine accepts it only at night with a
stable sensor and always emits a :class:`CalibrationResult` (surfaced on the
room's calibration event entity).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LUX_SENSOR, CONF_NAME, CONF_ROOM_ID, CONF_ROOMS, DOMAIN
from .controller import Controller
from .core.events import StartCalibration
from .entity import LightConductorEntity, room_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller: Controller = hass.data[DOMAIN][entry.entry_id]
    entities = [
        RecordLightResponseButton(
            controller, room[CONF_ROOM_ID], room.get(CONF_NAME, room[CONF_ROOM_ID])
        )
        for room in entry.options.get(CONF_ROOMS, ())
        if room.get(CONF_LUX_SENSOR)
    ]
    async_add_entities(entities)


class RecordLightResponseButton(LightConductorEntity, ButtonEntity):
    """Start a photometric calibration sweep for one room (§4.4)."""

    _attr_translation_key = "record_light_response"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: Controller, room_id: str, name: str) -> None:
        super().__init__(controller, f"{room_id}_record_light_response")
        self._room_id = room_id
        self._attr_device_info = room_device_info(controller.entry, room_id, name)

    async def async_press(self) -> None:
        self.controller.submit(StartCalibration(room_id=self._room_id))
