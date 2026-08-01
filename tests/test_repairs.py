"""Repairs fix flow for the lux-wedge notice (§3.5, D17 beta.11).

Drives the flow through HA's real ``RepairsFlowManager`` so the automatic
post-flow issue deletion (done by the repairs integration) is exercised.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.button import ButtonDeviceClass
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.light_conductor.const import DOMAIN
from custom_components.light_conductor.core.events import ReviewTick
from custom_components.light_conductor.repairs import (
    LuxWedgedFixFlow,
    async_create_fix_flow,
)

from .adapter import options, room, set_light, setup_entry

ISSUE_ID = "lux_wedged_sensor.klux"


async def _raise_fixable_issue(hass: HomeAssistant):
    """Set up a room with a wedged, registry-known lux sensor whose device has a
    restart button, and drive the controller to raise the FIXABLE wedge issue.

    Returns ``(controller, entry)``.
    """
    await async_setup_component(hass, "repairs", {})

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    dev_entry = MockConfigEntry(domain="apollo", data={})
    dev_entry.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=dev_entry.entry_id,
        identifiers={("apollo", "msr2")},
    )
    ent_reg.async_get_or_create(
        "sensor",
        "apollo",
        "lux1",
        device_id=device.id,
        original_device_class="illuminance",
        suggested_object_id="klux",
    )
    ent_reg.async_get_or_create(
        "button",
        "apollo",
        "reboot1",
        device_id=device.id,
        original_device_class=ButtonDeviceClass.RESTART,
        suggested_object_id="esp_reboot",
    )

    set_light(hass, "light.k", transition=True)
    hass.states.async_set("binary_sensor.pk", "on")
    hass.states.async_set("sensor.klux", "40")
    entry = await setup_entry(
        hass,
        options([room("k", ["light.k"], presence="binary_sensor.pk", lux="sensor.klux")]),
    )
    async_mock_service(hass, "light", "turn_on")
    controller = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set("sensor.klux", "41")
    await hass.async_block_till_done()
    controller.engine.room_state("k").est.last_report_at = dt_util.utcnow() - timedelta(
        seconds=2000
    )

    controller.submit(ReviewTick())
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)
    assert issue is not None and issue.is_fixable
    return controller, entry


async def test_async_create_fix_flow_returns_lux_wedged_flow(hass: HomeAssistant) -> None:
    """The platform entry point returns a confirm flow for the wedge issue."""
    await _raise_fixable_issue(hass)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)
    flow = await async_create_fix_flow(hass, ISSUE_ID, issue.data)
    assert isinstance(flow, LuxWedgedFixFlow)


async def test_fix_flow_presses_button_and_deletes_issue(hass: HomeAssistant) -> None:
    """Completing the confirm step presses the RIGHT restart button, stamps the
    controller grace, and lets HA auto-delete the issue."""
    controller, _entry = await _raise_fixable_issue(hass)
    press = async_mock_service(hass, "button", "press")

    flow_manager = hass.data["repairs"]["flow_manager"]
    result = await flow_manager.async_init(
        DOMAIN, context={"source": "repairs"}, data={"issue_id": ISSUE_ID}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await flow_manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Pressed the resolved restart button, exactly once, on the right entity.
    assert len(press) == 1
    assert press[0].data[ATTR_ENTITY_ID] == "button.esp_reboot"
    # Grace stamped on the owning controller.
    assert "sensor.klux" in controller._wedge_fix_pressed
    assert "sensor.klux" not in controller._wedged
    # HA's repairs helper deleted the issue once the flow finished.
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_fix_flow_without_button_data_is_noop_press(hass: HomeAssistant) -> None:
    """A flow whose data lacks a button (defensive) completes without pressing —
    it must never raise, only skip the press."""
    press = async_mock_service(hass, "button", "press")
    flow = LuxWedgedFixFlow({"entry_id": "x", "sensor_entity_id": "sensor.klux"})
    flow.hass = hass
    flow.flow_id = "test"
    flow.handler = DOMAIN
    flow.context = {}
    result = await flow.async_step_confirm(user_input={})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(press) == 0


async def test_fix_flow_press_timeout_completes_without_grace(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Review F2: a hung button.press is bounded by PRESS_TIMEOUT — the flow
    completes (warning logged) and stamps NO grace, so an honest immediate
    re-raise remains possible (the reboot likely never happened)."""
    import asyncio

    from custom_components.light_conductor import repairs as repairs_mod

    controller, _entry = await _raise_fixable_issue(hass)
    monkeypatch.setattr(repairs_mod, "PRESS_TIMEOUT", 0.05)

    async def hang(call) -> None:
        await asyncio.sleep(5)

    hass.services.async_register("button", "press", hang)

    flow_manager = hass.data["repairs"]["flow_manager"]
    result = await flow_manager.async_init(
        DOMAIN, context={"source": "repairs"}, data={"issue_id": ISSUE_ID}
    )
    result = await flow_manager.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "sensor.klux" not in controller._wedge_fix_pressed
