"""Shared helpers for the adapter (HA-integration) tests.

Not collected by pytest (no ``test_`` prefix). Builds a small opaque home,
fake light/sensor states, and a set-up config entry.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.light_conductor.const import (
    CONF_ACTIVITY_SENSOR,
    CONF_CHANNELS,
    CONF_LUX_SENSOR,
    CONF_NAME,
    CONF_OCCUPANCY_FALLBACK,
    CONF_PRESENCE_PRIMARY,
    CONF_PROFILE,
    CONF_ROOM_ID,
    CONF_ROOMS,
    CONF_SHAPE,
    CONF_TUNABLES,
    CONF_WALL_EVENTS,
    DOMAIN,
)

TRANSITION = 32  # LightEntityFeature.TRANSITION


def set_light(
    hass: HomeAssistant,
    entity_id: str,
    state: str = "off",
    brightness: int | None = None,
    ct: int | None = None,
    transition: bool = False,
    color_temp: bool = False,
) -> None:
    attrs: dict[str, Any] = {
        ATTR_SUPPORTED_FEATURES: TRANSITION if transition else 0,
        "supported_color_modes": ["color_temp"] if color_temp else ["brightness"],
    }
    if brightness is not None:
        attrs["brightness"] = brightness
    if ct is not None:
        attrs["color_temp_kelvin"] = ct
    if color_temp:
        attrs["min_color_temp_kelvin"] = 2200
        attrs["max_color_temp_kelvin"] = 4000
    hass.states.async_set(entity_id, state, attrs)


def room(
    room_id: str,
    lights: list[str],
    *,
    shape: str = "presence",
    presence: str | None = None,
    activity: str | None = None,
    lux: str | None = None,
    fallback: list[str] | None = None,
    vacancy: str = "dim",
    channels: list[dict] | None = None,
    max_output: float | None = None,
    wall: list[str] | None = None,
) -> dict[str, Any]:
    chans = channels or [
        {"entity": e, "band": "primary", "weight": 1.0, "fixed_ct": 2700, "dim_floor": 0.02}
        for e in lights
    ]
    # ``max_output`` pins day == evening == cap so the ACTIVE output is the same
    # at any time of day — tests that assert on the commanded level stay
    # circadian-independent (no wall-clock flakiness).
    active_day = 1.0 if max_output is None else max_output
    active_evening = 0.3 if max_output is None else max_output
    cap = 0.3 if max_output is None else max_output
    r: dict[str, Any] = {
        CONF_ROOM_ID: room_id,
        CONF_NAME: room_id.title(),
        CONF_SHAPE: shape,
        CONF_CHANNELS: chans,
        CONF_PROFILE: {
            "vacancy": vacancy,
            "active_day_output": active_day,
            "active_evening_output": active_evening,
            "background_output": 0.08,
            "evening_output_cap": cap,
            "night_output": 0.05,
            "tv_output": 0.15,
            "tv_output_empty": 0.0,
        },
    }
    if presence:
        r[CONF_PRESENCE_PRIMARY] = presence
    if activity:
        r[CONF_ACTIVITY_SENSOR] = activity
    if lux:
        r[CONF_LUX_SENSOR] = lux
    if fallback:
        r[CONF_OCCUPANCY_FALLBACK] = fallback
    if wall:
        r[CONF_WALL_EVENTS] = wall
    return r


def options(rooms: list[dict], *, instant: bool = True, **globals_: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {CONF_ROOMS: rooms, **globals_}
    if instant:
        # No startup grace: writes happen on the first review (test convenience).
        opts[CONF_TUNABLES] = {"startup_grace": 0, **globals_.get(CONF_TUNABLES, {})}
    return opts


async def setup_entry(
    hass: HomeAssistant,
    opts: dict[str, Any],
    enabled: bool = True,
    restore: tuple[State, ...] = (),
) -> MockConfigEntry:
    states = tuple(restore)
    if enabled:
        # Production fail-safe boots observe-only; tests go live the way a
        # real install does — through the enabled switch's restored state.
        states = (State("switch.light_conductor_enabled", "on"), *states)
    if states:
        mock_restore_cache(hass, states)
    entry = MockConfigEntry(domain=DOMAIN, title="Test", data={}, options=opts)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id_for(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str | None:
    registry = er.async_get(hass)
    unique = f"{entry.entry_id}_{suffix}"
    for ent in registry.entities.values():
        if ent.config_entry_id == entry.entry_id and ent.unique_id == unique:
            return ent.entity_id
    return None


def entities_for(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    registry = er.async_get(hass)
    return [
        ent.entity_id for ent in registry.entities.values() if ent.config_entry_id == entry.entry_id
    ]
