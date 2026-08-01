"""Repairs fix flow for the lux-wedge notice (§3.5, D17 beta.11).

The wedge issue is raised fixable (see ``controller._check_lux_wedge``) whenever
the wedged Apollo MSR-2 lux sensor has an ESP reboot button on the same device.
The Fix button opens a one-step confirm flow that presses that button — the
reboot unwedges the LTR390 and readings resume within a minute. HA deletes the
issue automatically once the flow finishes; the controller grace-suppresses an
immediate re-raise (``note_wedge_fix_pressed``) so the user is not re-nagged
while the sensor boots.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import (
    SERVICE_PRESS,
)
from homeassistant.components.repairs import RepairsFlow
from homeassistant.components.repairs.models import RepairsFlowResult
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .const import DOMAIN

if TYPE_CHECKING:
    from .controller import Controller

type IssueData = dict[str, str | int | float | None] | None

_LOGGER = logging.getLogger(__name__)

#: Upper bound on the blocking button.press inside the fix flow (review F2).
PRESS_TIMEOUT = 15.0


class LuxWedgedFixFlow(RepairsFlow):
    """Confirm flow: press the sensor's ESP reboot button to unwedge it."""

    def __init__(self, data: IssueData) -> None:
        self._data: dict[str, str | int | float | None] = dict(data or {})

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """First step just funnels into the confirm step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the confirm form, then press the reboot button on submit."""
        if user_input is not None:
            button = self._data.get("button_entity_id")
            pressed = False
            if button:
                # blocking so the press is dispatched before the issue goes away,
                # but bounded: a genuinely stalled ESPHome link must surface as a
                # warning, not hang the repairs dialog forever (review F2).
                try:
                    async with asyncio.timeout(PRESS_TIMEOUT):
                        await self.hass.services.async_call(
                            BUTTON_DOMAIN,
                            SERVICE_PRESS,
                            {ATTR_ENTITY_ID: button},
                            blocking=True,
                        )
                    pressed = True
                except TimeoutError:
                    _LOGGER.warning(
                        "Pressing %s timed out after %ss; the device link may be "
                        "down — the wedge notice will re-raise if readings do "
                        "not resume",
                        button,
                        PRESS_TIMEOUT,
                    )
            # Reach the owning controller to stamp the post-press grace window so
            # the still-silent sensor is not immediately re-flagged (§3.5). No
            # grace on a timed-out press: the reboot likely never happened, so an
            # honest immediate re-raise is preferable.
            if pressed:
                entry_id = self._data.get("entry_id")
                sensor = self._data.get("sensor_entity_id")
                controller: Controller | None = self.hass.data.get(DOMAIN, {}).get(entry_id)
                if controller is not None and isinstance(sensor, str):
                    controller.note_wedge_fix_pressed(sensor)
            # HA deletes the issue once this entry is created.
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "entity_id": str(self._data.get("sensor_entity_id", "")),
                "room": str(self._data.get("room", "")),
                "button": str(self._data.get("button_entity_id", "")),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: IssueData,
) -> RepairsFlow:
    """Create the fix flow for a ``lux_wedged_*`` issue.

    Guarded by prefix so a future fixable issue in this domain cannot be
    routed here by accident (review F4).
    """
    if not issue_id.startswith("lux_wedged_"):
        raise ValueError(f"no fix flow for issue {issue_id}")
    return LuxWedgedFixFlow(data)
