"""Per-room calibration result event entity (§4.4/§10).

Fires a ``committed`` / ``rejected`` event whenever a calibration sweep for the
room finishes, carrying the reason and per-channel coverage. Consumed by
automations that want to react to (re)calibration.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LUX_SENSOR, CONF_NAME, CONF_ROOM_ID, CONF_ROOMS, DOMAIN, signal_calibration
from .controller import Controller
from .entity import LightConductorEntity, room_device_info

EVENT_COMMITTED = "committed"
EVENT_REJECTED = "rejected"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller: Controller = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CalibrationEvent(controller, room[CONF_ROOM_ID], room.get(CONF_NAME, room[CONF_ROOM_ID]))
        for room in entry.options.get(CONF_ROOMS, ())
        if room.get(CONF_LUX_SENSOR)
    ]
    async_add_entities(entities)


class CalibrationEvent(LightConductorEntity, EventEntity):
    """Result of the room's last calibration sweep (§4.4)."""

    _attr_translation_key = "calibration"
    _attr_event_types: ClassVar[list[str]] = [EVENT_COMMITTED, EVENT_REJECTED]

    def __init__(self, controller: Controller, room_id: str, name: str) -> None:
        super().__init__(controller, f"{room_id}_calibration")
        self._room_id = room_id
        self._attr_device_info = room_device_info(controller.entry, room_id, name)

    async def async_added_to_hass(self) -> None:
        # Note: does NOT chain the base update signal — an EventEntity must only
        # write on an actual event, never on every engine cycle.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_calibration(self._entry.entry_id, self._room_id),
                self._handle_result,
            )
        )

    @callback
    def _handle_result(self, data: dict[str, Any]) -> None:
        event_type = EVENT_COMMITTED if data.get("ok") else EVENT_REJECTED
        self._trigger_event(
            event_type,
            {"reason": data.get("reason", ""), "coverage": data.get("coverage", {})},
        )
        self.async_write_ha_state()
