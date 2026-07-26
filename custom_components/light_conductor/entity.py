"""Shared entity base and device grouping (family idiom).

Every entity is read-only over the engine state and never polls: it refreshes
on the per-entry dispatcher signal the controller sends after each cycle
(sonos ConductorEntity idiom). Entities *submit events* to the controller; they
never write engine state directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, signal_update

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .controller import Controller


def hub_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Light Conductor",
        entry_type=DeviceEntryType.SERVICE,
    )


def room_device_info(entry: ConfigEntry, room_id: str, room_name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_room_{room_id}")},
        name=room_name,
        manufacturer="Light Conductor",
        via_device=(DOMAIN, entry.entry_id),
        suggested_area=room_name,
    )


class LightConductorEntity(Entity):
    """Base: no polling, name from translations, refresh on dispatcher signal."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, controller: Controller, suffix: str) -> None:
        self.controller = controller
        self._entry = controller.entry
        self._attr_unique_id = f"{self._entry.entry_id}_{suffix}"
        self._attr_device_info = hub_device_info(self._entry)

    @callback
    def _on_engine_update(self) -> None:
        """Refresh on each engine cycle. Override to interpose a publish gate."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._entry.entry_id),
                self._on_engine_update,
            )
        )
