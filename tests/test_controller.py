"""Controller e2e: command execution, echo/foreign, lux, review timers, unload."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_REPORTED
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.light_conductor.const import CONF_TUNABLES, DOMAIN

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

    # A foreign change OUTSIDE the fade envelope (yanked to full) → override.
    set_light(hass, "light.a", "on", brightness=255, transition=True)
    await hass.async_block_till_done()
    assert hass.states.get(overridden).state == "on"
    assert controller.engine.room_state("a").overridden is True


async def test_transition_fade_reports_no_override(hass: HomeAssistant) -> None:
    """Intermediate mesh reports during a native fade must NOT latch (F1)."""
    set_light(hass, "light.a", transition=True)
    hass.states.async_set("binary_sensor.pa", "off")
    entry = await setup_entry(hass, options([room("a", ["light.a"], presence="binary_sensor.pa")]))
    async_mock_service(hass, "light", "turn_on")
    controller = hass.data[DOMAIN][entry.entry_id]
    overridden = entity_id_for(hass, entry, "a_overridden")

    hass.states.async_set("binary_sensor.pa", "on")
    await hass.async_block_till_done()
    target = round(controller.engine.room_state("a").channels["light.a"].commanded_b * 255)

    # A rising fade trajectory that ends at the commanded target — all inside
    # the [0, target] envelope, so none may latch.
    for frac in (0.2, 0.5, 0.8, 1.0):
        set_light(hass, "light.a", "on", brightness=max(1, round(target * frac)), transition=True)
        await hass.async_block_till_done()
    assert hass.states.get(overridden).state == "off"

    # A wall dial yanks to full mid-fade — outside the envelope ⇒ override.
    set_light(hass, "light.a", "on", brightness=255, transition=True)
    await hass.async_block_till_done()
    assert hass.states.get(overridden).state == "on"


async def test_min_write_interval_coalesces(hass: HomeAssistant) -> None:
    """Bursted writes to one channel: first immediate, rest coalesced+delayed (F2)."""
    set_light(hass, "light.a", transition=True)
    entry = await setup_entry(hass, options([room("a", ["light.a"])]))
    turn_on = async_mock_service(hass, "light", "turn_on")
    writer = hass.data[DOMAIN][entry.entry_id]._writer("light.a")

    writer.set_channel(0.5, None, 0.0)
    await hass.async_block_till_done()
    assert len(turn_on) == 1  # first write immediate

    writer.set_channel(0.6, None, 0.0)  # coalesced away
    writer.set_channel(0.7, None, 0.0)  # latest wins
    await hass.async_block_till_done()
    assert len(turn_on) == 1  # still spacing-limited

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1.2))
    await hass.async_block_till_done()
    assert len(turn_on) == 2  # the delayed final landed
    assert turn_on[-1].data["brightness"] == round(0.7 * 255)


async def test_off_not_buried_by_slow_set(hass: HomeAssistant) -> None:
    """set then off back-to-back: off is the final call even on a slow device (F3)."""
    set_light(hass, "light.a", transition=True)
    entry = await setup_entry(
        hass, options([room("a", ["light.a"])], **{CONF_TUNABLES: {"min_write_interval": 0.0}})
    )
    calls: list[str] = []

    async def _slow_on(call):
        calls.append("on")
        await asyncio.sleep(0)  # yield so the off is submitted mid-flight

    async def _rec_off(call):
        calls.append("off")

    hass.services.async_register("light", "turn_on", _slow_on)
    hass.services.async_register("light", "turn_off", _rec_off)
    writer = hass.data[DOMAIN][entry.entry_id]._writer("light.a")

    writer.set_channel(0.5, None, 0.0)
    writer.turn_off(0.0)
    await hass.async_block_till_done()
    assert calls[-1] == "off"  # never buried by the stale turn_on


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
