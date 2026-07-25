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
