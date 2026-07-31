"""Entry lifecycle: setup and unload round-trip."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_conductor.const import DOMAIN


async def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Test", data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_stores_placeholder(hass: HomeAssistant) -> None:
    entry = await make_entry(hass)
    assert entry.entry_id in hass.data[DOMAIN]


async def test_unload_cleans_up(hass: HomeAssistant) -> None:
    entry = await make_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]


def test_presence_capable_derivation() -> None:
    """§9.2/D15: presence_capable derives from configured sensing — the live
    soverom option shape (presence_primary null, occupancy_fallback []) must
    derive False; a fallback-only room (kontor's PIR) and a primary-sensor
    room derive True."""
    from custom_components.light_conductor.const import build_engine_config

    from .adapter import options, room

    blind = room("soverom", ["light.s"], shape="door")
    blind["presence_primary"] = None  # exact live-entry shape of the incident
    blind["occupancy_fallback"] = []
    cfg = build_engine_config(
        None,
        options(
            [
                blind,
                room("kontor", ["light.k"], fallback=["binary_sensor.pir"]),
                room("sofakrok", ["light.f"], presence="binary_sensor.p"),
            ]
        ),
    )
    assert cfg.room("soverom").presence_capable is False
    assert cfg.room("kontor").presence_capable is True
    assert cfg.room("sofakrok").presence_capable is True
