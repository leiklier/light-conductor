"""Constants and the options contract.

Options contract (house convention, shared with the rest of the conductor
family): ``entry.data`` stays empty. Every user-facing setting lives in
``entry.options`` so the options flow can edit all of it without recreating the
entry; ``entry.title`` is the display name. Runtime knobs that HA restores
(master dimmer, mode switches) are entity state, never options.

The normative list of signals and tunables is ``docs/ENGINE_SPEC.md``; this
module is the single place that maps the flat option dict onto the frozen core
dataclasses (``build_engine_config`` / ``build_tunables``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .core.model import (
    Band,
    ChannelConfig,
    EngineConfig,
    Profile,
    RoomConfig,
    RoomShape,
    Vacancy,
)
from .core.tunables import Tunables

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

DOMAIN = "light_conductor"

PLATFORMS: tuple[str, ...] = (
    "light",
    "switch",
    "sensor",
    "binary_sensor",
    "button",
    "event",
)

# --- global option keys -----------------------------------------------------

CONF_SLEEP_ENTITY = "sleep_entity"
CONF_ANYONE_HOME_ENTITY = "anyone_home_entity"
CONF_PRESENCE_FALLBACK = "presence_fallback_entities"
CONF_TV_ENTITIES = "tv_entities"
CONF_VACATION_ENTITY = "vacation_entity"
CONF_NIGHT_TRIGGERS = "night_trigger_entities"
CONF_NIGHT_PATH_ROOMS = "night_path_rooms"
CONF_ROOMS = "rooms"
CONF_TUNABLES = "tunables"

#: Committed per-room calibrations, persisted into ``entry.options`` (not a
#: helpers.storage Store) so they ride the same reload path as everything else
#: — the family convention (presence-conductor CalibrationManager, sonos
#: last_master). Written at runtime by the controller; excluded from the
#: reload-baseline guard so a commit never triggers an entry reload loop.
CONF_CALIBRATIONS = "calibrations"

#: Options keys the controller writes at runtime; changes to these must NOT
#: reload the entry (they are echoes of the running engine, not user edits).
RUNTIME_OPTION_KEYS: frozenset[str] = frozenset({CONF_CALIBRATIONS})

# --- per-room option keys ---------------------------------------------------

CONF_ROOM_ID = "room_id"
CONF_NAME = "name"
CONF_SHAPE = "shape"
CONF_CHANNELS = "channels"
CONF_LUX_SENSOR = "lux_sensor"
CONF_PRESENCE_PRIMARY = "presence_primary"
CONF_ACTIVITY_SENSOR = "activity_sensor"
CONF_OCCUPANCY_FALLBACK = "occupancy_fallback"
CONF_NEIGHBOURS = "neighbours"
CONF_WALL_EVENTS = "wall_event_entities"
CONF_TRIGGERS = "trigger_entities"
CONF_LIVING_GROUP = "living_group"
CONF_TV_MODE = "tv_mode"
CONF_HOLD_SECONDS = "hold_seconds"
CONF_PROFILE = "profile"

# --- per-channel option keys ------------------------------------------------

CONF_CH_ENTITY = "entity"
CONF_CH_BAND = "band"
CONF_CH_WEIGHT = "weight"
CONF_CH_FIXED_CT = "fixed_ct"  # None/0 => CT-capable, read range from entity
CONF_CH_DIM_FLOOR = "dim_floor"
#: Affine response mapping (rule 4.5): open-loop command =
#: clamp(response_slope · out + response_offset, 0, 1). Blank => defaults 1.0/0.0
#: (exact no-op). See ADR D16 for the benke calibration.
CONF_CH_RESPONSE_SLOPE = "response_slope"
CONF_CH_RESPONSE_OFFSET = "response_offset"

# --- profile option keys ----------------------------------------------------

CONF_VACANCY = "vacancy"
CONF_ACTIVE_DAY = "active_day_output"
CONF_ACTIVE_EVENING = "active_evening_output"
CONF_BACKGROUND = "background_output"
CONF_EVENING_CAP = "evening_output_cap"
CONF_NIGHT_OUTPUT = "night_output"
CONF_TV_OUTPUT = "tv_output"
CONF_TV_OUTPUT_EMPTY = "tv_output_empty"
#: Closed-loop lux tiers (lx) for a room with a lux sensor (§2.1). Absent or 0
#: means UNSET ⇒ the engine auto-targets a fraction of the room's calibrated
#: capacity; an explicit value overrides that default.
CONF_LUX_ACTIVE_DAY = "lux_active_day"
CONF_LUX_ACTIVE_EVENING = "lux_active_evening"
CONF_LUX_BACKGROUND = "lux_background"

# --- dispatcher / hass.data keys -------------------------------------------


#: Per-entry dispatcher signal published after every engine cycle so entities
#: can pull fresh state (sonos ConductorEntity idiom).
def signal_update(entry_id: str) -> str:
    return f"{DOMAIN}_update_{entry_id}"


#: Per-room dispatcher signal for calibration-result events (§10).
def signal_calibration(entry_id: str, room_id: str) -> str:
    return f"{DOMAIN}_calibration_{entry_id}_{room_id}"


# --- HA bus event fired on calibration completion (§10) --------------------

EVENT_CALIBRATION = "light_conductor_calibration"

# Shapes/vacancy exposed as selector options.
SHAPES = tuple(s.value for s in RoomShape)
VACANCIES = tuple(v.value for v in Vacancy)
BANDS = tuple(b.value for b in Band)

# Generic per-tier defaults (normalized channel output per band). Applied
# uniformly across the room's bands; per-band refinement is available by
# editing options (see docs). Values chosen to be useful out of the box.
DEFAULT_ACTIVE_DAY = 1.0
DEFAULT_ACTIVE_EVENING = 0.3
DEFAULT_BACKGROUND = 0.08
DEFAULT_NIGHT_OUTPUT = 0.05
DEFAULT_TV_OUTPUT = 0.15
DEFAULT_TV_OUTPUT_EMPTY = 0.0


def _band_map(value: float) -> dict[Band, float]:
    """Expand a single per-tier output across all three bands (rule 4.6).

    The boost band's evening lockout is enforced downstream by
    :func:`core.photometry.allocate`, so a uniform expansion is safe.
    """
    return {b: float(value) for b in Band}


def _profile_from_options(opts: Mapping[str, Any]) -> Profile:
    """Build a frozen :class:`Profile` from a room's ``profile`` option dict."""
    vacancy = Vacancy(opts.get(CONF_VACANCY, Vacancy.DIM.value))
    day = float(opts.get(CONF_ACTIVE_DAY, DEFAULT_ACTIVE_DAY))
    evening = float(opts.get(CONF_ACTIVE_EVENING, DEFAULT_ACTIVE_EVENING))
    background = float(opts.get(CONF_BACKGROUND, DEFAULT_BACKGROUND))
    night = float(opts.get(CONF_NIGHT_OUTPUT, DEFAULT_NIGHT_OUTPUT))
    tv = float(opts.get(CONF_TV_OUTPUT, DEFAULT_TV_OUTPUT))
    tv_empty = float(opts.get(CONF_TV_OUTPUT_EMPTY, DEFAULT_TV_OUTPUT_EMPTY))
    # Closed-loop lux tiers (§2.1): absent/blank ⇒ 0.0 = auto (capacity fraction).
    lux_day = float(opts.get(CONF_LUX_ACTIVE_DAY) or 0.0)
    lux_evening = float(opts.get(CONF_LUX_ACTIVE_EVENING) or 0.0)
    lux_background = float(opts.get(CONF_LUX_BACKGROUND) or 0.0)
    return Profile(
        vacancy=vacancy,
        out_active_day=_band_map(day),
        out_active_evening=_band_map(evening),
        out_background=_band_map(background),
        evening_output_cap=float(opts.get(CONF_EVENING_CAP, Tunables().evening_output_cap)),
        night_output=_band_map(night),
        tv_output=_band_map(tv),
        tv_output_empty=_band_map(tv_empty),
        lux_active_day=lux_day,
        lux_active_evening=lux_evening,
        lux_background=lux_background,
    )


def _ct_range(hass: HomeAssistant | None, entity_id: str) -> tuple[int, int] | None:
    """Read a light entity's kelvin range for a CT-capable channel (rule 4.1)."""
    if hass is None:
        return (2200, 4000)  # conservative default when the entity is not live yet
    state = hass.states.get(entity_id)
    if state is None:
        return (2200, 4000)
    lo = state.attributes.get("min_color_temp_kelvin")
    hi = state.attributes.get("max_color_temp_kelvin")
    if lo is None or hi is None:
        return (2200, 4000)
    return (int(lo), int(hi))


def _channel_from_options(hass: HomeAssistant | None, opts: Mapping[str, Any]) -> ChannelConfig:
    fixed_ct = opts.get(CONF_CH_FIXED_CT)
    entity_id = opts[CONF_CH_ENTITY]
    ct_capable = fixed_ct in (None, 0, "")
    return ChannelConfig(
        channel_id=entity_id,
        band=Band(opts.get(CONF_CH_BAND, Band.PRIMARY.value)),
        fixed_ct=None if ct_capable else int(fixed_ct),
        ct_range=_ct_range(hass, entity_id) if ct_capable else None,
        dim_floor=float(opts.get(CONF_CH_DIM_FLOOR, 0.02)),
        weight=float(opts.get(CONF_CH_WEIGHT, 1.0)),
        response_slope=float(opts.get(CONF_CH_RESPONSE_SLOPE, 1.0)),
        response_offset=float(opts.get(CONF_CH_RESPONSE_OFFSET, 0.0)),
    )


def build_engine_config(hass: HomeAssistant | None, options: Mapping[str, Any]) -> EngineConfig:
    """Translate ``entry.options`` into the frozen core :class:`EngineConfig`.

    ``hass`` is used only to read CT-capable channels' kelvin range from the
    live light entity; ``None`` (unit tests / not-yet-live) falls back to a
    conservative range.
    """
    night_path_rooms = set(options.get(CONF_NIGHT_PATH_ROOMS, ()))
    rooms: list[RoomConfig] = []
    for room in options.get(CONF_ROOMS, ()):
        room_id = room[CONF_ROOM_ID]
        channels = tuple(_channel_from_options(hass, ch) for ch in room.get(CONF_CHANNELS, ()))
        rooms.append(
            RoomConfig(
                room_id=room_id,
                channels=channels,
                profile=_profile_from_options(room.get(CONF_PROFILE, {})),
                shape=RoomShape(room.get(CONF_SHAPE, RoomShape.PRESENCE.value)),
                neighbours=tuple(room.get(CONF_NEIGHBOURS, ())),
                living_group=bool(room.get(CONF_LIVING_GROUP, False)),
                hold_seconds=room.get(CONF_HOLD_SECONDS),
                night_path=room_id in night_path_rooms,
                tv_mode=bool(room.get(CONF_TV_MODE, False)),
                has_lux_sensor=bool(room.get(CONF_LUX_SENSOR)),
                presence_capable=bool(
                    room.get(CONF_PRESENCE_PRIMARY) or room.get(CONF_OCCUPANCY_FALLBACK)
                ),
            )
        )
    return EngineConfig(rooms=tuple(rooms))


def build_tunables(options: Mapping[str, Any]) -> Tunables:
    """Build core :class:`Tunables`, overriding only the keys present in options."""
    overrides = dict(options.get(CONF_TUNABLES, {}) or {})
    if not overrides:
        return Tunables()
    defaults = Tunables()
    fields = {f: getattr(defaults, f) for f in Tunables.__dataclass_fields__}
    kwargs: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in fields:
            continue
        default = fields[key]
        if isinstance(default, bool):
            kwargs[key] = bool(value)
        elif isinstance(default, int) and not isinstance(default, bool):
            kwargs[key] = int(value)
        elif isinstance(default, float):
            kwargs[key] = float(value)
        else:
            kwargs[key] = value
    return Tunables(**kwargs)


#: Tunable keys editable through the options flow (spec §12 scalar rows;
#: tuple-valued rows like ``calibration_levels`` are left to code defaults).
EDITABLE_TUNABLES: tuple[str, ...] = (
    "hold_seconds",
    "living_memory",
    "trigger_hold",
    "door_close_hold",
    "presence_blind_hold",
    "sun_high_deg",
    "sun_low_deg",
    "circadian_tick",
    "evening_cap_threshold",
    "bootstrap_dispersion_max",
    "lux_stale",
    "lux_wedge_warn",
    "deadband_capacity_frac",
    "deadband_floor",
    "min_closed_loop_capacity",
    "daylight_full",
    "calibration_dwell",
    "night_hold",
    "night_fade",
    "sleep_fade",
    "outdoor_on_threshold",
    "outdoor_on_lux",
    "outdoor_full_lux",
    "outdoor_presence_factor",
    "gain_range_stops",
    "gain_reset",
    "slew_step",
    "slew_interval",
    "slew_step_empty",
    "min_delta",
    "min_write_interval",
    "max_inflight",
    "echo_window",
    "override_timeout",
    "startup_grace",
)
