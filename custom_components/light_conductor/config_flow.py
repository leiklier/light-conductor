"""Config & options flow.

One config entry = the whole home (house convention). ``entry.data`` stays
empty; everything lives in ``entry.options``. Setup runs a discovery prefill —
for every HA area it proposes a room with that area's lights, illuminance
sensor, and presence-conductor room-occupancy sensor (matched by area +
device_class/integration, never by hardcoded names) — then hands off to the
options flow, which mirrors the setup for editing (globals → rooms → tunables).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ACTIVE_DAY,
    CONF_ACTIVE_EVENING,
    CONF_ACTIVITY_SENSOR,
    CONF_ANYONE_HOME_ENTITY,
    CONF_BACKGROUND,
    CONF_CHANNELS,
    CONF_EVENING_CAP,
    CONF_HOLD_SECONDS,
    CONF_LIVING_GROUP,
    CONF_LUX_SENSOR,
    CONF_NAME,
    CONF_NEIGHBOURS,
    CONF_NIGHT_OUTPUT,
    CONF_NIGHT_PATH_ROOMS,
    CONF_NIGHT_TRIGGERS,
    CONF_OCCUPANCY_FALLBACK,
    CONF_PRESENCE_FALLBACK,
    CONF_PRESENCE_PRIMARY,
    CONF_PROFILE,
    CONF_ROOM_ID,
    CONF_ROOMS,
    CONF_SHAPE,
    CONF_SLEEP_ENTITY,
    CONF_TRIGGERS,
    CONF_TUNABLES,
    CONF_TV_ENTITIES,
    CONF_TV_MODE,
    CONF_TV_OUTPUT,
    CONF_TV_OUTPUT_EMPTY,
    CONF_VACANCY,
    CONF_VACATION_ENTITY,
    CONF_WALL_EVENTS,
    DEFAULT_ACTIVE_DAY,
    DEFAULT_ACTIVE_EVENING,
    DEFAULT_BACKGROUND,
    DEFAULT_NIGHT_OUTPUT,
    DEFAULT_TV_OUTPUT,
    DEFAULT_TV_OUTPUT_EMPTY,
    DOMAIN,
    EDITABLE_TUNABLES,
    SHAPES,
    VACANCIES,
)
from .core.model import RoomShape
from .core.tunables import Tunables

CONF_HUB_NAME = "name"

# UI bounds for the generated tunables schema (spec §12 scalar rows).
_TUNABLE_UI: dict[str, tuple[float, float, float]] = {
    "hold_seconds": (10, 1800, 5),
    "living_memory": (0, 3600, 30),
    "trigger_hold": (10, 1800, 10),
    "door_close_hold": (0, 120, 1),
    "presence_blind_hold": (10, 600, 10),
    "sun_high_deg": (-10, 30, 1),
    "sun_low_deg": (-20, 10, 1),
    "circadian_tick": (30, 900, 30),
    "evening_cap_threshold": (0, 1, 0.05),
    "lux_stale": (30, 600, 10),
    "night_hold": (60, 1800, 30),
    "night_fade": (0, 60, 1),
    "sleep_fade": (0, 60, 1),
    "outdoor_on_threshold": (0, 1, 0.05),
    "gain_range_stops": (0.5, 3, 0.5),
    "slew_step": (0.02, 0.5, 0.01),
    "slew_interval": (0.5, 5, 0.5),
    "slew_step_empty": (0.05, 1, 0.05),
    "min_delta": (0.01, 0.2, 0.01),
    "min_write_interval": (0.5, 5, 0.5),
    "max_inflight": (1, 8, 1),
    "echo_window": (2, 30, 1),
    "override_timeout": (60, 86400, 60),
    "startup_grace": (0, 120, 5),
}


def _entity(domain: str | list[str], device_class: str | None = None) -> EntitySelector:
    cfg: dict[str, Any] = {"domain": domain}
    if device_class is not None:
        cfg["device_class"] = device_class
    return EntitySelector(EntitySelectorConfig(**cfg))


def _entities(domain: str | list[str], device_class: str | None = None) -> EntitySelector:
    cfg: dict[str, Any] = {"domain": domain, "multiple": True}
    if device_class is not None:
        cfg["device_class"] = device_class
    return EntitySelector(EntitySelectorConfig(**cfg))


def _select(options: tuple[str, ...]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=list(options), mode=SelectSelectorMode.DROPDOWN)
    )


def _select_multi(options: list[str]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
    )


def _pct(_default: float) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=0, max=1, step=0.01, mode=NumberSelectorMode.BOX)
    )


def _opt(key: str, source: dict[str, Any]) -> Any:
    """A ``vol.Optional`` marker pre-filled from the current value (blank if unset)."""
    current = source.get(key)
    if current in (None, [], ""):
        return vol.Optional(key)
    return vol.Optional(key, default=current)


# ---------------------------------------------------------------------------
# Discovery prefill
# ---------------------------------------------------------------------------


def _resolved_area(entity: er.RegistryEntry, devices: dr.DeviceRegistry) -> str | None:
    if entity.area_id:
        return entity.area_id
    if entity.device_id:
        device = devices.async_get(entity.device_id)
        if device is not None:
            return device.area_id
    return None


def _default_channel(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    ct_capable = False
    if state is not None:
        modes = state.attributes.get("supported_color_modes") or []
        ct_capable = "color_temp" in modes
    return {
        "entity": entity_id,
        "band": "primary",
        "weight": 1.0,
        "fixed_ct": None if ct_capable else 2700,
        "dim_floor": 0.02,
    }


def _default_profile() -> dict[str, Any]:
    return {
        CONF_VACANCY: "dim",
        CONF_ACTIVE_DAY: DEFAULT_ACTIVE_DAY,
        CONF_ACTIVE_EVENING: DEFAULT_ACTIVE_EVENING,
        CONF_BACKGROUND: DEFAULT_BACKGROUND,
        CONF_EVENING_CAP: Tunables().evening_output_cap,
        CONF_NIGHT_OUTPUT: DEFAULT_NIGHT_OUTPUT,
        CONF_TV_OUTPUT: DEFAULT_TV_OUTPUT,
        CONF_TV_OUTPUT_EMPTY: DEFAULT_TV_OUTPUT_EMPTY,
    }


def _discover_rooms(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Propose one room per HA area from its lights + sensors (generic match)."""
    areas = ar.async_get(hass)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    by_area: dict[str, list[er.RegistryEntry]] = {}
    for ent in entities.entities.values():
        area_id = _resolved_area(ent, devices)
        if area_id is not None:
            by_area.setdefault(area_id, []).append(ent)

    rooms: list[dict[str, Any]] = []
    for area_id, members in by_area.items():
        area = areas.async_get_area(area_id)
        if area is None:
            continue
        lights = [e.entity_id for e in members if e.domain == "light" and e.platform != DOMAIN]
        if not lights:
            continue  # only areas with controllable lights become rooms
        lux = next(
            (
                e.entity_id
                for e in members
                if e.domain == "sensor"
                and (e.device_class or e.original_device_class) == "illuminance"
            ),
            None,
        )
        primary = next(
            (
                e.entity_id
                for e in members
                if e.domain == "binary_sensor" and "room_occupancy" in e.entity_id
            ),
            None,
        )
        activity = next(
            (
                e.entity_id
                for e in members
                if e.domain == "sensor" and "room_activity" in e.entity_id
            ),
            None,
        )
        fallback = [
            e.entity_id
            for e in members
            if e.domain == "binary_sensor"
            and e.entity_id != primary
            and (e.device_class or e.original_device_class) in ("occupancy", "motion", "presence")
        ]
        rooms.append(
            {
                CONF_ROOM_ID: area_id,
                CONF_NAME: area.name,
                CONF_SHAPE: RoomShape.PRESENCE.value,
                CONF_CHANNELS: [_default_channel(hass, e) for e in lights],
                CONF_LUX_SENSOR: lux,
                CONF_PRESENCE_PRIMARY: primary,
                CONF_ACTIVITY_SENSOR: activity,
                CONF_OCCUPANCY_FALLBACK: fallback,
                CONF_NEIGHBOURS: [],
                CONF_WALL_EVENTS: [],
                CONF_TRIGGERS: [],
                CONF_LIVING_GROUP: False,
                CONF_TV_MODE: False,
                CONF_PROFILE: _default_profile(),
            }
        )
    return rooms


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class LightConductorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup — single instance, discovery-prefilled."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            options: dict[str, Any] = {CONF_ROOMS: _discover_rooms(self.hass)}
            return self.async_create_entry(
                title=user_input[CONF_HUB_NAME], data={}, options=options
            )
        schema = vol.Schema(
            {vol.Required(CONF_HUB_NAME, default="Light Conductor"): TextSelector()}
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return LightConductorOptionsFlow()


# ---------------------------------------------------------------------------
# Options flow (mirrors setup for editing)
# ---------------------------------------------------------------------------


class LightConductorOptionsFlow(OptionsFlow):
    """Menu-driven editor: globals → rooms loop → tunables."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._room_id: str | None = None

    @property
    def _rooms(self) -> list[dict[str, Any]]:
        return list(self._options.get(CONF_ROOMS, ()))

    def _room_ids(self) -> list[str]:
        return [r[CONF_ROOM_ID] for r in self._rooms]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._options:
            self._options = dict(self.config_entry.options)
        return self.async_show_menu(
            step_id="init", menu_options=["globals", "rooms", "tunables", "finish"]
        )

    # -- globals ------------------------------------------------------------

    async def async_step_globals(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            for key in (
                CONF_SLEEP_ENTITY,
                CONF_ANYONE_HOME_ENTITY,
                CONF_VACATION_ENTITY,
                CONF_PRESENCE_FALLBACK,
                CONF_TV_ENTITIES,
                CONF_NIGHT_TRIGGERS,
                CONF_NIGHT_PATH_ROOMS,
            ):
                if key in user_input:
                    self._options[key] = user_input[key]
                else:
                    self._options.pop(key, None)
            return await self.async_step_init()

        o = self._options
        schema = vol.Schema(
            {
                _opt(CONF_SLEEP_ENTITY, o): _entity(["binary_sensor", "input_boolean", "switch"]),
                _opt(CONF_ANYONE_HOME_ENTITY, o): _entity(["binary_sensor", "input_boolean"]),
                _opt(CONF_VACATION_ENTITY, o): _entity(["binary_sensor", "input_boolean"]),
                _opt(CONF_PRESENCE_FALLBACK, o): _entities(
                    ["binary_sensor", "person", "device_tracker"]
                ),
                _opt(CONF_TV_ENTITIES, o): _entities("media_player"),
                _opt(CONF_NIGHT_TRIGGERS, o): _entities(["binary_sensor", "event"]),
                _opt(CONF_NIGHT_PATH_ROOMS, o): _select_multi(self._room_ids()),
            }
        )
        return self.async_show_form(step_id="globals", data_schema=schema)

    # -- rooms --------------------------------------------------------------

    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="rooms", menu_options=["add_room", "edit_room", "remove_room", "init"]
        )

    async def async_step_add_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            rooms = self._rooms
            rooms.append(
                {
                    CONF_ROOM_ID: user_input[CONF_ROOM_ID],
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_SHAPE: user_input[CONF_SHAPE],
                    CONF_CHANNELS: [
                        _default_channel(self.hass, e) for e in user_input.get(CONF_CHANNELS, [])
                    ],
                    CONF_PROFILE: _default_profile(),
                }
            )
            self._options[CONF_ROOMS] = rooms
            return await self.async_step_rooms()
        schema = vol.Schema(
            {
                vol.Required(CONF_ROOM_ID): TextSelector(),
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_SHAPE, default=RoomShape.PRESENCE.value): _select(SHAPES),
                vol.Optional(CONF_CHANNELS, default=[]): _entities("light"),
            }
        )
        return self.async_show_form(step_id="add_room", data_schema=schema)

    async def async_step_remove_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            rid = user_input[CONF_ROOM_ID]
            self._options[CONF_ROOMS] = [r for r in self._rooms if r[CONF_ROOM_ID] != rid]
            return await self.async_step_rooms()
        schema = vol.Schema({vol.Required(CONF_ROOM_ID): _select(tuple(self._room_ids()))})
        return self.async_show_form(step_id="remove_room", data_schema=schema)

    async def async_step_edit_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None and self._room_id is None:
            self._room_id = user_input[CONF_ROOM_ID]
            return await self.async_step_room_detail()
        schema = vol.Schema({vol.Required(CONF_ROOM_ID): _select(tuple(self._room_ids()))})
        return self.async_show_form(step_id="edit_room", data_schema=schema)

    async def async_step_room_detail(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        rooms = self._rooms
        room = next(r for r in rooms if r[CONF_ROOM_ID] == self._room_id)
        if user_input is not None:
            updated = dict(room)
            updated[CONF_NAME] = user_input[CONF_NAME]
            updated[CONF_SHAPE] = user_input[CONF_SHAPE]
            for key in (CONF_LUX_SENSOR, CONF_PRESENCE_PRIMARY, CONF_ACTIVITY_SENSOR):
                updated[key] = user_input.get(key)
            for key in (
                CONF_OCCUPANCY_FALLBACK,
                CONF_NEIGHBOURS,
                CONF_WALL_EVENTS,
                CONF_TRIGGERS,
            ):
                updated[key] = user_input.get(key, [])
            channels = user_input.get(CONF_CHANNELS, [])
            existing = {
                (c["entity"] if isinstance(c, dict) else c): c
                for c in room.get(CONF_CHANNELS, [])
                if isinstance(c, dict)
            }
            updated[CONF_CHANNELS] = [
                existing.get(e, _default_channel(self.hass, e)) for e in channels
            ]
            updated[CONF_LIVING_GROUP] = user_input.get(CONF_LIVING_GROUP, False)
            updated[CONF_TV_MODE] = user_input.get(CONF_TV_MODE, False)
            if user_input.get(CONF_HOLD_SECONDS):
                updated[CONF_HOLD_SECONDS] = user_input[CONF_HOLD_SECONDS]
            profile = dict(room.get(CONF_PROFILE, _default_profile()))
            for key in (
                CONF_VACANCY,
                CONF_ACTIVE_DAY,
                CONF_ACTIVE_EVENING,
                CONF_BACKGROUND,
                CONF_EVENING_CAP,
                CONF_NIGHT_OUTPUT,
                CONF_TV_OUTPUT,
                CONF_TV_OUTPUT_EMPTY,
            ):
                if key in user_input:
                    profile[key] = user_input[key]
            updated[CONF_PROFILE] = profile
            self._options[CONF_ROOMS] = [
                updated if r[CONF_ROOM_ID] == self._room_id else r for r in rooms
            ]
            self._room_id = None
            return await self.async_step_rooms()

        profile = room.get(CONF_PROFILE, _default_profile())
        channel_entities = [
            c["entity"] if isinstance(c, dict) else c for c in room.get(CONF_CHANNELS, [])
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=room.get(CONF_NAME, "")): TextSelector(),
                vol.Required(CONF_SHAPE, default=room.get(CONF_SHAPE, "presence")): _select(SHAPES),
                vol.Optional(CONF_CHANNELS, default=channel_entities): _entities("light"),
                _opt(CONF_LUX_SENSOR, room): _entity("sensor", "illuminance"),
                _opt(CONF_PRESENCE_PRIMARY, room): _entity("binary_sensor", "occupancy"),
                _opt(CONF_ACTIVITY_SENSOR, room): _entity("sensor"),
                _opt(CONF_OCCUPANCY_FALLBACK, room): _entities(["binary_sensor"]),
                _opt(CONF_NEIGHBOURS, room): _select_multi(
                    [r for r in self._room_ids() if r != self._room_id]
                ),
                _opt(CONF_WALL_EVENTS, room): _entities(["event"]),
                _opt(CONF_TRIGGERS, room): _entities(["binary_sensor", "event"]),
                vol.Optional(
                    CONF_LIVING_GROUP, default=room.get(CONF_LIVING_GROUP, False)
                ): BooleanSelector(),
                vol.Optional(
                    CONF_TV_MODE, default=room.get(CONF_TV_MODE, False)
                ): BooleanSelector(),
                vol.Optional(CONF_VACANCY, default=profile.get(CONF_VACANCY, "dim")): _select(
                    VACANCIES
                ),
                vol.Optional(
                    CONF_ACTIVE_DAY, default=profile.get(CONF_ACTIVE_DAY, DEFAULT_ACTIVE_DAY)
                ): _pct(DEFAULT_ACTIVE_DAY),
                vol.Optional(
                    CONF_ACTIVE_EVENING,
                    default=profile.get(CONF_ACTIVE_EVENING, DEFAULT_ACTIVE_EVENING),
                ): _pct(DEFAULT_ACTIVE_EVENING),
                vol.Optional(
                    CONF_BACKGROUND, default=profile.get(CONF_BACKGROUND, DEFAULT_BACKGROUND)
                ): _pct(DEFAULT_BACKGROUND),
                vol.Optional(
                    CONF_EVENING_CAP,
                    default=profile.get(CONF_EVENING_CAP, Tunables().evening_output_cap),
                ): _pct(0.3),
                vol.Optional(
                    CONF_NIGHT_OUTPUT, default=profile.get(CONF_NIGHT_OUTPUT, DEFAULT_NIGHT_OUTPUT)
                ): _pct(DEFAULT_NIGHT_OUTPUT),
                vol.Optional(
                    CONF_TV_OUTPUT, default=profile.get(CONF_TV_OUTPUT, DEFAULT_TV_OUTPUT)
                ): _pct(DEFAULT_TV_OUTPUT),
                vol.Optional(
                    CONF_TV_OUTPUT_EMPTY,
                    default=profile.get(CONF_TV_OUTPUT_EMPTY, DEFAULT_TV_OUTPUT_EMPTY),
                ): _pct(DEFAULT_TV_OUTPUT_EMPTY),
            }
        )
        return self.async_show_form(
            step_id="room_detail",
            data_schema=schema,
            description_placeholders={"room": room.get(CONF_NAME, self._room_id)},
        )

    # -- tunables -----------------------------------------------------------

    async def async_step_tunables(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            defaults = Tunables()
            overrides = {
                k: v for k, v in user_input.items() if v is not None and v != getattr(defaults, k)
            }
            if overrides:
                self._options[CONF_TUNABLES] = overrides
            else:
                self._options.pop(CONF_TUNABLES, None)
            return await self.async_step_init()

        current = dict(self._options.get(CONF_TUNABLES, {}))
        defaults = Tunables()
        fields: dict[Any, Any] = {}
        for name in EDITABLE_TUNABLES:
            default = current.get(name, getattr(defaults, name))
            if isinstance(getattr(defaults, name), bool):
                fields[vol.Optional(name, default=default)] = BooleanSelector()
                continue
            lo, hi, step = _TUNABLE_UI.get(name, (0, 1000, 1))
            fields[vol.Optional(name, default=default)] = NumberSelector(
                NumberSelectorConfig(min=lo, max=hi, step=step, mode=NumberSelectorMode.BOX)
            )
        return self.async_show_form(step_id="tunables", data_schema=vol.Schema(fields))

    # -- finish -------------------------------------------------------------

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self._options)
