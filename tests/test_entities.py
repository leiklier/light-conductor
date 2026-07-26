"""Entity behaviours: master gain restore + off, away lighting, calibration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.light_conductor.const import CONF_CALIBRATIONS, DOMAIN
from custom_components.light_conductor.core.model import RoomCalibration
from custom_components.light_conductor.event import EVENT_REJECTED

from .adapter import entity_id_for, options, room, set_light, setup_entry


async def test_master_gain_restore(hass: HomeAssistant) -> None:
    # Restored 200/255 ≈ 78 % ⇒ clearly above the 50 % neutral default.
    mock_restore_cache(hass, (State("light.test_master", "on", {"brightness": 200}),))
    entry = await setup_entry(hass, options([room("k", ["light.k"])]))
    controller = hass.data[DOMAIN][entry.entry_id]
    assert controller.engine.state.master_pct > 70.0
    assert controller.master_on is True


async def test_master_off_event(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, options([room("k", ["light.k"])]))
    controller = hass.data[DOMAIN][entry.entry_id]
    master = entity_id_for(hass, entry, "master")

    await hass.services.async_call("light", "turn_off", {"entity_id": master}, blocking=True)
    await hass.async_block_till_done()
    assert controller.engine.state.master_on is False
    assert hass.states.get(master).state == "off"


async def test_away_lighting_switch(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, options([room("b", ["light.b"], shape="outdoor")]))
    controller = hass.data[DOMAIN][entry.entry_id]
    away = entity_id_for(hass, entry, "away_lighting")
    assert hass.states.get(away).state == "on"  # default on

    await hass.services.async_call("switch", "turn_off", {"entity_id": away}, blocking=True)
    await hass.async_block_till_done()
    assert controller.engine.state.away_lighting is False
    assert hass.states.get(away).state == "off"


async def test_calibration_reject_by_day(hass: HomeAssistant) -> None:
    """Pressing the button in daylight rejects; nothing is persisted."""
    set_light(hass, "light.k", transition=True)
    hass.states.async_set("sensor.klux", "40")
    hass.states.async_set("sun.sun", "above_horizon", {"elevation": 30.0})
    entry = await setup_entry(hass, options([room("k", ["light.k"], lux="sensor.klux")]))
    button = entity_id_for(hass, entry, "k_record_light_response")
    ev = entity_id_for(hass, entry, "k_calibration")

    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()

    ev_state = hass.states.get(ev)
    assert ev_state.attributes.get("event_type") == EVENT_REJECTED
    assert CONF_CALIBRATIONS not in entry.options


async def test_calibration_persist_and_guard(hass: HomeAssistant) -> None:
    """A committed result persists into options without reloading the entry."""
    hass.states.async_set("sensor.klux", "40")
    entry = await setup_entry(hass, options([room("k", ["light.k"], lux="sensor.klux")]))
    controller = hass.data[DOMAIN][entry.entry_id]

    # Simulate the engine committing a calibration for room k.
    from custom_components.light_conductor.core.plan import CalibrationResult

    controller._exec_calibration(CalibrationResult("k", True, "ok", (("light.k", 1.0),)))
    await hass.async_block_till_done()

    assert "k" in entry.options[CONF_CALIBRATIONS]
    # Runtime option write must not have reloaded the entry (same controller).
    assert hass.data[DOMAIN][entry.entry_id] is controller


async def test_invalid_stored_calibration_discarded(hass: HomeAssistant) -> None:
    """A calibration bound to the wrong channel set is discarded on load."""
    bad = RoomCalibration("k", {"light.OTHER": 1.0}, {"light.OTHER": ((0.0, 0.0),)})
    entry = await setup_entry(
        hass,
        options(
            [room("k", ["light.k"], lux="sensor.klux")],
            **{CONF_CALIBRATIONS: {"k": bad.to_dict()}},
        ),
    )
    controller = hass.data[DOMAIN][entry.entry_id]
    # Mismatch → room stays uncalibrated (default photometry).
    assert controller.engine._photo["k"].calibrated is False
