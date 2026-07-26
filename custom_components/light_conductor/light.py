"""Master-gain dimmer (`light.light_conductor_master`, §7/§10).

A brightness-only dimmer whose level *is* the master gain: 0-255 maps linearly
to 0-100 %, and the core turns that percentage into an exponential gain
(50 % = x1). The entity carries no policy — it only maps the slider and submits
:class:`MasterGainChanged` / :class:`MasterPowerChanged` events. Restorable
(rule §11.2): a restored level is re-submitted through the controller.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .controller import Controller
from .core.events import MasterGainChanged, MasterPowerChanged
from .entity import LightConductorEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller: Controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MasterLight(controller)])


class MasterLight(LightConductorEntity, LightEntity, RestoreEntity):
    """The whole-home master gain, exposed as a HomeKit-ready dimmer."""

    _attr_translation_key = "master"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.BRIGHTNESS}

    def __init__(self, controller: Controller) -> None:
        super().__init__(controller, "master")

    @property
    def is_on(self) -> bool:
        return self.controller.master_on

    @property
    def brightness(self) -> int:
        return round(self.controller.master_pct / 100.0 * 255.0)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            pct = kwargs[ATTR_BRIGHTNESS] / 255.0 * 100.0
            self.controller.submit(MasterGainChanged(pct=pct))
        else:
            self.controller.submit(MasterPowerChanged(on=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.controller.submit(MasterPowerChanged(on=False))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        if last.state == "off":
            self.controller.submit(MasterPowerChanged(on=False))
        else:
            bri = last.attributes.get(ATTR_BRIGHTNESS)
            if bri is not None:
                self.controller.submit(MasterGainChanged(pct=bri / 255.0 * 100.0))
