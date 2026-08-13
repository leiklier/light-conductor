"""Switch entities (§10): enable, away-lighting, per-outdoor occupational,
per-trigger-room door lighting.

All of them are restorable runtime knobs (rule §11.2): the restored state is
re-submitted through the controller so the engine and the entity agree.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_NAME, CONF_ROOM_ID, CONF_ROOMS, CONF_SHAPE, CONF_TRIGGERS, DOMAIN
from .controller import Controller
from .core.events import (
    DoorLightingChanged,
    OccupationalChanged,
    SetAwayLighting,
    SetEnabled,
)
from .core.model import RoomShape
from .entity import LightConductorEntity, room_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller: Controller = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [
        EnabledSwitch(controller),
        AwayLightingSwitch(controller),
    ]
    for room in entry.options.get(CONF_ROOMS, ()):
        room_id = room[CONF_ROOM_ID]
        name = room.get(CONF_NAME, room_id)
        if room.get(CONF_SHAPE) == RoomShape.OUTDOOR.value:
            entities.append(OccupationalSwitch(controller, room_id, name))
        # Keyed off the trigger entities, not the shape: the switch is only
        # meaningful where a trigger can actually fire (§1.9).
        if room.get(CONF_TRIGGERS):
            entities.append(DoorLightingSwitch(controller, room_id, name))
    async_add_entities(entities)


class _RestoreSwitch(LightConductorEntity, SwitchEntity, RestoreEntity):
    """A restorable switch that re-submits its restored state on startup."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._apply(last.state == "on")
        else:
            # The engine's fail-safe default stands (enabled=False ⇒ observe
            # only). A missed restore must be visible, not silent: a restore
            # miss on the enabled switch is how a conductor could otherwise
            # go live unbidden after a restart.
            _LOGGER.warning("No restore data for %s; using the fail-safe default", self.entity_id)

    def _apply(self, on: bool) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class EnabledSwitch(_RestoreSwitch):
    """Master enable; off = observe-only (§10)."""

    _attr_translation_key = "enabled"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: Controller) -> None:
        super().__init__(controller, "enabled")

    @property
    def is_on(self) -> bool:
        return self.controller.engine.state.enabled

    def _apply(self, on: bool) -> None:
        self.controller.submit(SetEnabled(enabled=on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.controller.submit(SetEnabled(enabled=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.controller.submit(SetEnabled(enabled=False))


class AwayLightingSwitch(_RestoreSwitch):
    """Outdoor presence simulation while away (§6.4), default on."""

    _attr_translation_key = "away_lighting"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: Controller) -> None:
        super().__init__(controller, "away_lighting")

    @property
    def is_on(self) -> bool:
        return self.controller.engine.state.away_lighting

    def _apply(self, on: bool) -> None:
        self.controller.submit(SetAwayLighting(on=on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.controller.submit(SetAwayLighting(on=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.controller.submit(SetAwayLighting(on=False))


class OccupationalSwitch(_RestoreSwitch):
    """An outdoor room's 'sitting outside' switch (§6.5), default off."""

    _attr_translation_key = "occupational"

    def __init__(self, controller: Controller, room_id: str, room_name: str) -> None:
        super().__init__(controller, f"{room_id}_occupational")
        self._room_id = room_id
        self._attr_device_info = room_device_info(controller.entry, room_id, room_name)

    @property
    def is_on(self) -> bool:
        return self.controller.engine.room_state(self._room_id).occupational

    def _apply(self, on: bool) -> None:
        self.controller.submit(OccupationalChanged(room_id=self._room_id, on=on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.controller.submit(OccupationalChanged(room_id=self._room_id, on=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.controller.submit(OccupationalChanged(room_id=self._room_id, on=False))


class DoorLightingSwitch(_RestoreSwitch):
    """A trigger room's door-lighting gate (§1.9), default on.

    A convenience toggle, not a safety boundary — so unlike the enabled switch
    a restore miss keeps the engine default (on).
    """

    _attr_translation_key = "door_lighting"

    def __init__(self, controller: Controller, room_id: str, room_name: str) -> None:
        super().__init__(controller, f"{room_id}_door_lighting")
        self._room_id = room_id
        self._attr_device_info = room_device_info(controller.entry, room_id, room_name)

    @property
    def is_on(self) -> bool:
        return self.controller.engine.room_state(self._room_id).door_lighting

    def _apply(self, on: bool) -> None:
        self.controller.submit(DoorLightingChanged(room_id=self._room_id, on=on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.controller.submit(DoorLightingChanged(room_id=self._room_id, on=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.controller.submit(DoorLightingChanged(room_id=self._room_id, on=False))
