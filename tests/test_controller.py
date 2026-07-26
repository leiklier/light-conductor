"""Controller e2e: command execution, echo/foreign, lux, review timers, unload."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_REPORTED
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.light_conductor.const import DOMAIN

from .adapter import entity_id_for, options, room, set_light, setup_entry


async def test_transition_vs_stepping(hass: HomeAssistant) -> None:
    """A TRANSITION-capable light gets one call w/ transition; others step."""
    set_light(hass, "light.a", transition=True)
    set_light(hass, "light.b", transition=False)
    hass.states.async_set("binary_sensor.pa", "off")
    hass.states.async_set("binary_sensor.pb", "off")
    await setup_entry(
        hass,
        options(
            [
                room("a", ["light.a"], presence="binary_sensor.pa"),
                room("b", ["light.b"], presence="binary_sensor.pb"),
            ]
        ),
    )
    # Mock AFTER setup so it overrides the real light service the platform loads.
    turn_on = async_mock_service(hass, "light", "turn_on")

    hass.states.async_set("binary_sensor.pa", "on")
    hass.states.async_set("binary_sensor.pb", "on")
    await hass.async_block_till_done()

    calls = {c.data[ATTR_ENTITY_ID]: c.data for c in turn_on}
    assert "transition" in calls["light.a"]  # native ramp
    assert "transition" not in calls["light.b"]  # software stepping fallback
    assert calls["light.b"]["brightness"] < 255  # ramp in progress


async def test_echo_then_foreign_override(hass: HomeAssistant) -> None:
    set_light(hass, "light.a", transition=True)
    hass.states.async_set("binary_sensor.pa", "off")
    entry = await setup_entry(hass, options([room("a", ["light.a"], presence="binary_sensor.pa")]))
    async_mock_service(hass, "light", "turn_on")
    controller = hass.data[DOMAIN][entry.entry_id]
    overridden = entity_id_for(hass, entry, "a_overridden")

    hass.states.async_set("binary_sensor.pa", "on")
    await hass.async_block_till_done()
    cs = controller.engine.room_state("a").channels["light.a"]
    commanded = round(cs.commanded_b * 255)

    # Device echoes our own command back → consumed, no override.
    set_light(hass, "light.a", "on", brightness=commanded, transition=True)
    await hass.async_block_till_done()
    assert hass.states.get(overridden).state == "off"

    # A foreign change (someone dims to a value we never commanded) → override.
    set_light(hass, "light.a", "on", brightness=7, transition=True)
    await hass.async_block_till_done()
    assert hass.states.get(overridden).state == "on"
    assert controller.engine.room_state("a").overridden is True


async def test_lux_report_feeds_estimator(hass: HomeAssistant) -> None:
    set_light(hass, "light.k", transition=True)
    hass.states.async_set("binary_sensor.pk", "off")
    hass.states.async_set("sensor.klux", "40")
    entry = await setup_entry(
        hass,
        options([room("k", ["light.k"], presence="binary_sensor.pk", lux="sensor.klux")]),
    )
    controller = hass.data[DOMAIN][entry.entry_id]

    for val in ("42", "45", "43", "41"):
        hass.states.async_set("sensor.klux", val)
        await hass.async_block_till_done()
    # A same-value 1 Hz sample flows via EVENT_STATE_REPORTED too.
    hass.bus.async_fire(EVENT_STATE_REPORTED, {"entity_id": "sensor.klux"})
    await hass.async_block_till_done()

    est = controller.engine.room_state("k").est
    assert est.last_report_at is not None
    assert est.l_filt is not None


async def test_review_timer_rearms(hass: HomeAssistant) -> None:
    hass.states.async_set("binary_sensor.pk", "off")
    entry = await setup_entry(hass, options([room("k", ["light.k"], presence="binary_sensor.pk")]))
    controller = hass.data[DOMAIN][entry.entry_id]
    assert controller._review_cancel is not None

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=700))
    await hass.async_block_till_done()
    assert controller._review_cancel is not None  # re-armed after the tick


async def test_unload_is_clean(hass: HomeAssistant) -> None:
    set_light(hass, "light.k", transition=True)
    hass.states.async_set("binary_sensor.pk", "off")
    entry = await setup_entry(hass, options([room("k", ["light.k"], presence="binary_sensor.pk")]))
    controller = hass.data[DOMAIN][entry.entry_id]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]
    assert controller._unsubs == []
    assert controller._review_cancel is None
    assert controller._task is None
