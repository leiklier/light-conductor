"""Entity behaviours: master gain restore + off, away lighting, calibration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.light_conductor.const import (
    CONF_CALIBRATIONS,
    CONF_SLEEP_ENTITY,
    DOMAIN,
)
from custom_components.light_conductor.core.model import RoomCalibration
from custom_components.light_conductor.core.plan import CalibrationResult
from custom_components.light_conductor.event import EVENT_REJECTED

from .adapter import entity_id_for, options, room, set_light, setup_entry


async def test_master_gain_restore(hass: HomeAssistant) -> None:
    # Restored 200/255 ≈ 78 % ⇒ clearly above the 50 % neutral default. The
    # object id is language-pinned, so the restore key is light_conductor_master.
    entry = await setup_entry(
        hass,
        options([room("k", ["light.k"])]),
        restore=(State("light.light_conductor_master", "on", {"brightness": 200}),),
    )
    controller = hass.data[DOMAIN][entry.entry_id]
    assert controller.engine.state.master_pct > 70.0
    assert controller.master_on is True


async def test_entity_object_ids_are_language_pinned(hass: HomeAssistant) -> None:
    """§10/E: object ids are slugified English `light_conductor_*`, room name not
    doubled, independent of the (Norwegian) display names."""
    entry = await setup_entry(
        hass,
        options(
            [
                room("kjokken", ["light.k"], lux="sensor.klux"),
                room("balkong", ["light.b"], shape="outdoor"),
            ]
        ),
    )
    reg = er.async_get(hass)

    def eid(suffix: str) -> str | None:
        unique = f"{entry.entry_id}_{suffix}"
        return next(
            (
                e.entity_id
                for e in reg.entities.values()
                if e.config_entry_id == entry.entry_id and e.unique_id == unique
            ),
            None,
        )

    # Global entities.
    assert eid("master") == "light.light_conductor_master"
    assert eid("enabled") == "switch.light_conductor_enabled"
    assert eid("away_lighting") == "switch.light_conductor_away_lighting"
    # Per-room entities (room id, not display name; no doubling).
    assert eid("kjokken_role") == "sensor.light_conductor_kjokken_role"
    assert eid("kjokken_natural_lux") == "sensor.light_conductor_kjokken_natural_lux"
    assert eid("kjokken_target_lux") == "sensor.light_conductor_kjokken_target_lux"
    assert eid("kjokken_overridden") == "binary_sensor.light_conductor_kjokken_overridden"
    assert (
        eid("kjokken_record_light_response")
        == "button.light_conductor_kjokken_record_light_response"
    )
    assert eid("kjokken_calibration") == "event.light_conductor_kjokken_calibration"
    assert eid("balkong_occupational") == "switch.light_conductor_balkong_occupational"


async def test_outdoor_lux_room_gets_no_closed_loop_entities(hass: HomeAssistant) -> None:
    """§6.5a: an outdoor room's lux sensor measures dusk, it does not close a
    loop — so it publishes natural lux but no target and no calibration UI."""
    entry = await setup_entry(
        hass,
        options([room("balkong", ["light.b"], shape="outdoor", lux="sensor.blux")]),
    )
    assert entity_id_for(hass, entry, "balkong_natural_lux") is not None
    assert entity_id_for(hass, entry, "balkong_target_lux") is None
    assert entity_id_for(hass, entry, "balkong_record_light_response") is None
    assert entity_id_for(hass, entry, "balkong_calibration") is None


def test_channels_sensor_marks_all_attributes_unrecorded() -> None:
    """§10: every attribute is MATCH_ALL-unrecorded so churn never records."""
    from homeassistant.const import MATCH_ALL

    from custom_components.light_conductor.sensor import ChannelsSensor

    assert ChannelsSensor._unrecorded_attributes == frozenset({MATCH_ALL})


async def test_channels_sensor_disabled_by_default_and_reports_commanded(
    hass: HomeAssistant,
) -> None:
    """§10: the per-room channels debug sensor is disabled by default; once the
    operator enables it, its state/attributes reflect the commanded outputs."""
    set_light(hass, "light.a", "on", brightness=128, transition=True)
    hass.states.async_set("binary_sensor.pa", "on")
    entry = await setup_entry(
        hass,
        options([room("a", ["light.a"], presence="binary_sensor.pa", max_output=0.5)]),
    )
    reg = er.async_get(hass)
    eid = entity_id_for(hass, entry, "a_channels")
    assert eid == "sensor.light_conductor_a_channels"
    ent = reg.async_get(eid)
    assert ent.disabled_by is not None  # opt-in debug sensor
    assert hass.states.get(eid) is None  # disabled → no state object

    # Enable it and reload; now it publishes the per-channel commanded outputs.
    reg.async_update_entity(eid, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(eid)
    assert state is not None
    controller = hass.data[DOMAIN][entry.entry_id]
    cs = controller.engine.room_state("a").channels["light.a"]
    pct = round(cs.commanded_b * 100.0)
    assert int(float(state.state)) == pct  # peak commanded output, whole percent
    assert state.attributes.get("unit_of_measurement") == "%"
    ch = state.attributes["light.a"]
    assert ch["output_pct"] == pct
    assert ch["on"] is True
    assert "ct" in ch  # whole-Kelvin int or None


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


async def test_options_finish_preserves_runtime_calibration(hass: HomeAssistant) -> None:
    """A calibration committed while the options flow is open survives finish (F5)."""
    hass.states.async_set("sensor.klux", "40")
    entry = await setup_entry(hass, options([room("k", ["light.k"], lux="sensor.klux")]))
    controller = hass.data[DOMAIN][entry.entry_id]

    # Open + edit the options flow (edits a non-runtime key).
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "globals"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SLEEP_ENTITY: "binary_sensor.sleep"}
    )

    # Meanwhile the controller commits a calibration (runtime options write).
    controller._exec_calibration(CalibrationResult("k", True, "ok", (("light.k", 1.0),)))
    await hass.async_block_till_done()
    assert "k" in entry.options[CONF_CALIBRATIONS]

    # Finishing must not clobber the committed calibration with the stale snapshot.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert "k" in entry.options[CONF_CALIBRATIONS]
    assert entry.options[CONF_SLEEP_ENTITY] == "binary_sensor.sleep"
