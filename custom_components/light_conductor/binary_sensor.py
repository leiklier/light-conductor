"""Per-room override indicator (§9/§10)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NAME, CONF_ROOM_ID, CONF_ROOMS, DOMAIN
from .controller import Controller
from .entity import LightConductorEntity, room_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller: Controller = hass.data[DOMAIN][entry.entry_id]
    entities = [
        OverriddenBinarySensor(
            controller, room[CONF_ROOM_ID], room.get(CONF_NAME, room[CONF_ROOM_ID])
        )
        for room in entry.options.get(CONF_ROOMS, ())
    ]
    async_add_entities(entities)


class OverriddenBinarySensor(LightConductorEntity, BinarySensorEntity):
    """True while a manual override is latched on the room (rule §9.1)."""

    _attr_translation_key = "overridden"

    def __init__(self, controller: Controller, room_id: str, name: str) -> None:
        super().__init__(controller, f"{room_id}_overridden")
        self._room_id = room_id
        self._attr_device_info = room_device_info(controller.entry, room_id, name)

    @property
    def is_on(self) -> bool:
        diag = self.controller.diagnostics.get(self._room_id)
        return bool(diag.overridden) if diag is not None else False
