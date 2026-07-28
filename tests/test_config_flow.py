"""Config & options flow: happy path, discovery prefill, options round-trip."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_conductor.const import (
    CONF_HOLD_SECONDS,
    CONF_LUX_ACTIVE_DAY,
    CONF_LUX_ACTIVE_EVENING,
    CONF_LUX_BACKGROUND,
    CONF_LUX_SENSOR,
    CONF_PRESENCE_PRIMARY,
    CONF_PROFILE,
    CONF_ROOMS,
    CONF_SLEEP_ENTITY,
    DOMAIN,
    _profile_from_options,
)

from .adapter import options, room, setup_entry


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


async def test_options_edit_channel(hass: HomeAssistant) -> None:
    """Per-channel editing: rooms → channels → pick room → pick channel → form."""
    entry = await setup_entry(hass, options([room("k", ["light.k"])]))

    async def configure(flow_id: str, data: dict) -> dict:
        return await hass.config_entries.options.async_configure(flow_id, data)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(result["flow_id"], {"next_step_id": "rooms"})
    result = await configure(result["flow_id"], {"next_step_id": "channels"})
    assert result["step_id"] == "channels"
    result = await configure(result["flow_id"], {"room_id": "k"})
    assert result["step_id"] == "pick_channel"
    result = await configure(result["flow_id"], {"entity": "light.k"})
    assert result["step_id"] == "channel_detail"
    # Edit band/weight/dim_floor; omit fixed_ct ⇒ CT-capable.
    result = await configure(
        result["flow_id"], {"band": "accent", "weight": 2.5, "dim_floor": 0.05}
    )
    assert result["type"] == FlowResultType.MENU  # back at the rooms menu
    result = await configure(result["flow_id"], {"next_step_id": "init"})
    result = await configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] == FlowResultType.CREATE_ENTRY

    ch = entry.options[CONF_ROOMS][0]["channels"][0]
    assert ch["entity"] == "light.k"
    assert ch["band"] == "accent"
    assert ch["weight"] == 2.5
    assert ch["fixed_ct"] is None  # empty fixed_ct ⇒ CT-capable
    assert ch["dim_floor"] == 0.05


def test_profile_parses_lux_tiers() -> None:
    """§2.1: the profile parser wires the closed-loop lux tiers; absent ⇒ 0 (auto)."""
    prof = _profile_from_options(
        {CONF_LUX_ACTIVE_DAY: 500, CONF_LUX_ACTIVE_EVENING: 250, CONF_LUX_BACKGROUND: 8}
    )
    assert prof.lux_active_day == 500.0
    assert prof.lux_active_evening == 250.0
    assert prof.lux_background == 8.0
    # An empty profile leaves every tier UNSET (0.0 = auto capacity fraction).
    empty = _profile_from_options({})
    assert empty.lux_active_day == 0.0
    assert empty.lux_active_evening == 0.0
    assert empty.lux_background == 0.0
    # A blank ("") submission is treated as unset too.
    assert _profile_from_options({CONF_LUX_ACTIVE_DAY: ""}).lux_active_day == 0.0


async def test_options_room_detail_lux_tiers_round_trip(hass: HomeAssistant) -> None:
    """Closed-loop lux tiers round-trip through room_detail and CLEAR on blank."""
    entry = await setup_entry(hass, options([room("k", ["light.k"])]))

    async def configure(flow_id: str, data: dict) -> dict:
        return await hass.config_entries.options.async_configure(flow_id, data)

    async def open_room_detail() -> dict:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await configure(result["flow_id"], {"next_step_id": "rooms"})
        result = await configure(result["flow_id"], {"next_step_id": "edit_room"})
        result = await configure(result["flow_id"], {"room_id": "k"})
        assert result["step_id"] == "room_detail"
        return result

    async def finish(result: dict) -> None:
        result = await configure(result["flow_id"], {"next_step_id": "init"})
        result = await configure(result["flow_id"], {"next_step_id": "finish"})
        assert result["type"] == FlowResultType.CREATE_ENTRY

    # (1) Set explicit lux tiers.
    result = await open_room_detail()
    result = await configure(
        result["flow_id"],
        {
            "name": "K",
            "shape": "presence",
            "channels": ["light.k"],
            CONF_LUX_ACTIVE_DAY: 500,
            CONF_LUX_ACTIVE_EVENING: 250,
            CONF_LUX_BACKGROUND: 8,
        },
    )
    assert result["type"] == FlowResultType.MENU
    await finish(result)
    profile = entry.options[CONF_ROOMS][0][CONF_PROFILE]
    assert profile[CONF_LUX_ACTIVE_DAY] == 500
    assert profile[CONF_LUX_ACTIVE_EVENING] == 250
    assert profile[CONF_LUX_BACKGROUND] == 8

    # (2) Re-open and CLEAR them — a blank submission omits the keys ⇒ auto.
    result = await open_room_detail()
    result = await configure(
        result["flow_id"], {"name": "K", "shape": "presence", "channels": ["light.k"]}
    )
    assert result["type"] == FlowResultType.MENU
    await finish(result)
    profile = entry.options[CONF_ROOMS][0][CONF_PROFILE]
    assert CONF_LUX_ACTIVE_DAY not in profile
    assert CONF_LUX_ACTIVE_EVENING not in profile
    assert CONF_LUX_BACKGROUND not in profile


async def test_options_room_detail_accepts_hold_seconds(hass: HomeAssistant) -> None:
    """hold_seconds is now in the room_detail schema (no more extra-keys reject)."""
    entry = await setup_entry(hass, options([room("k", ["light.k"])]))

    async def configure(flow_id: str, data: dict) -> dict:
        return await hass.config_entries.options.async_configure(flow_id, data)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(result["flow_id"], {"next_step_id": "rooms"})
    result = await configure(result["flow_id"], {"next_step_id": "edit_room"})
    result = await configure(result["flow_id"], {"room_id": "k"})
    assert result["step_id"] == "room_detail"
    result = await configure(
        result["flow_id"],
        {"name": "K", "shape": "presence", "channels": ["light.k"], "hold_seconds": 240},
    )
    assert result["type"] == FlowResultType.MENU  # accepted, back at the rooms menu
    result = await configure(result["flow_id"], {"next_step_id": "init"})
    result = await configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ROOMS][0][CONF_HOLD_SECONDS] == 240
