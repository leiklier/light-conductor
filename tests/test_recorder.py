"""Recorder discipline: zero state writes under churn + entity-inventory floor."""

from __future__ import annotations

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
)

from custom_components.light_conductor import sensor as lc_sensor

from .adapter import entities_for, options, room, set_light, setup_entry


async def test_recorder_discipline_sweep(hass: HomeAssistant, monkeypatch) -> None:
    """Estimator wiggle inside a 5-lx bucket must produce ZERO state writes."""
    # Freeze the gate's monotonic clock so the ≥10 s min-interval never elapses.
    monkeypatch.setattr(lc_sensor, "_monotonic", lambda: 1000.0)

    set_light(hass, "light.k", transition=True)
    hass.states.async_set("sensor.klux", "50")
    entry = await setup_entry(hass, options([room("k", ["light.k"], lux="sensor.klux")]))
    async_mock_service(hass, "light", "turn_on")

    # Warm-up: let the estimator settle and the sensors publish their first
    # value (an unavailable→available transition legitimately writes once).
    for val in ("50", "51", "50"):
        hass.states.async_set("sensor.klux", val)
        await hass.async_block_till_done()

    ours = set(entities_for(hass, entry))
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    events.clear()

    # Now drive in-bucket churn (48..52 all round to the 50-lx bucket).
    for val in ("48", "52", "49", "51", "50", "48.5", "51.5") * 4:
        hass.states.async_set("sensor.klux", val)
        await hass.async_block_till_done()

    writes = [e for e in events if e.data["entity_id"] in ours]
    assert writes == [], f"unexpected recorder writes: {[e.data['entity_id'] for e in writes]}"


EXPECTED_INVENTORY = (
    # A lux room (k) + an outdoor room (b):
    3  # hub: master, enabled, away_lighting
    + 1  # outdoor extra: occupational
    + 6  # room k (lux): role, overridden, natural_lux, target_lux, button, event
    + 2  # room b (no lux): role, overridden
)


def test_entity_inventory_floor() -> None:
    """The entity inventory for a known home is pinned (kept in sync by hand)."""
    assert EXPECTED_INVENTORY == 12


async def test_entity_inventory_count(hass: HomeAssistant) -> None:
    set_light(hass, "light.k", transition=True)
    set_light(hass, "light.b", transition=True)
    entry = await setup_entry(
        hass,
        options(
            [
                room("k", ["light.k"], lux="sensor.klux"),
                room("b", ["light.b"], shape="outdoor"),
            ]
        ),
    )
    assert len(entities_for(hass, entry)) == EXPECTED_INVENTORY
