"""Config & options flow: happy path, discovery prefill, options round-trip."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_conductor.const import (
    CONF_LUX_SENSOR,
    CONF_PRESENCE_PRIMARY,
    CONF_ROOMS,
    CONF_SLEEP_ENTITY,
    DOMAIN,
)


async def test_user_flow_creates_single_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Home lights"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home lights"
    assert result["data"] == {}
    assert CONF_ROOMS in result["options"]


async def test_single_instance_guard(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, title="One", data={}, options={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_discovery_prefill(hass: HomeAssistant) -> None:
    """An area with a light + illuminance + room-occupancy becomes a room."""
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    area = areas.async_create("Kjøkken")

    entry = MockConfigEntry(domain="test", data={})
    entry.add_to_hass(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", "dev1")},
    )
    devices.async_update_device(device.id, area_id=area.id)

    light = entities.async_get_or_create("light", "demo", "l1", device_id=device.id)
    lux = entities.async_get_or_create(
        "sensor", "apollo", "s1", device_id=device.id, original_device_class="illuminance"
    )
    occ = entities.async_get_or_create(
        "binary_sensor",
        "presence_conductor",
        "kjokken_occ",
        device_id=device.id,
        suggested_object_id="presence_conductor_kjokken_room_occupancy",
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "H"})
    rooms = result["options"][CONF_ROOMS]
    kitchen = next(r for r in rooms if r["room_id"] == area.id)
    assert kitchen[CONF_LUX_SENSOR] == lux.entity_id
    assert kitchen[CONF_PRESENCE_PRIMARY] == occ.entity_id
    assert kitchen["channels"][0]["entity"] == light.entity_id


async def test_options_round_trip(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="T", data={}, options={CONF_ROOMS: []})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "globals"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SLEEP_ENTITY: "binary_sensor.sleep"}
    )
    # Back at the menu; finish saves.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SLEEP_ENTITY] == "binary_sensor.sleep"
