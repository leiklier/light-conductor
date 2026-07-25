"""Configuration and runtime state for the lighting core.

All identifiers (``room_id``, ``channel_id``) are opaque strings; the core
never inspects them (ENGINE_SPEC §0). Config dataclasses are frozen; runtime
state is mutable and owned by the engine.

This module is the single dependency every feature module is allowed to
import (alongside :mod:`tunables` and :mod:`plan`) — feature modules never
import one another (house discipline, mirrors sonos-conductor).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Role(StrEnum):
    """Room activity role (ENGINE_SPEC §1.2).

    Priority order (highest first): NIGHT_PATH > TV > ACTIVE > ADJACENT >
    BACKGROUND > OFF.
    """

    ACTIVE = "active"
    ADJACENT = "adjacent"
    BACKGROUND = "background"
    OFF = "off"
    NIGHT_PATH = "night_path"
    TV = "tv"


class Activity(StrEnum):
    """Rich activity classification (presence-conductor room_activity, §1.1)."""

    EMPTY = "empty"
    PASSING = "passing"
    ACTIVE = "active"
    SETTLED = "settled"


#: Severity ordering for episode-peak tracking (rule 1.3).
ACTIVITY_SEVERITY: dict[Activity, int] = {
    Activity.EMPTY: 0,
    Activity.PASSING: 1,
    Activity.ACTIVE: 2,
    Activity.SETTLED: 3,
}


def max_activity(a: Activity | None, b: Activity | None) -> Activity | None:
    """The more severe of two activities; ``None`` carries no information."""
    if a is None:
        return b
    if b is None:
        return a
    return a if ACTIVITY_SEVERITY[a] >= ACTIVITY_SEVERITY[b] else b


class Vacancy(StrEnum):
    """How a room behaves once its ACTIVE hold expires (rule 1.4)."""

    DIM = "dim"  # living-area rooms: never below BACKGROUND while living active
    OFF = "off"  # kontor: straight to OFF after the hold


class Band(StrEnum):
    """Allocation band (rule 4.5). Fill order: accent -> primary -> boost."""

    ACCENT = "accent"
    PRIMARY = "primary"
    BOOST = "boost"


#: Fill order for band allocation (rule 4.5).
BAND_ORDER: tuple[Band, ...] = (Band.ACCENT, Band.PRIMARY, Band.BOOST)


class RoomShape(StrEnum):
    """How a room derives its role — configuration, not a subclass (§1)."""

    PRESENCE = "presence"  # presence-driven (kjøkken, kontor, sofakrok, spisebord)
    CORRIDOR = "corridor"  # no presence input; adjacency + evening + triggers (§1.7)
    DOOR = "door"  # door-triggered, no presence (soverom, §1.9)
    OUTDOOR = "outdoor"  # balkong: ignores presence, dusk-driven (§6.5)


# ---------------------------------------------------------------------------
# Configuration (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """One HA ``light`` entity in a room (rule 4.1).

    ``fixed_ct`` is the declared kelvin of a non-CT channel; a CT-capable
    channel sets ``ct_range`` (min_kelvin, max_kelvin) and leaves
    ``fixed_ct`` None. ``curve`` is an optional piecewise-linear list of
    ``(b, flux)`` points overriding the default square-law model (rule 4.2).
    """

    channel_id: str
    band: Band = Band.PRIMARY
    fixed_ct: int | None = 2700
    ct_range: tuple[int, int] | None = None
    dim_floor: float = 0.02
    curve: tuple[tuple[float, float], ...] | None = None
    #: Within-band aesthetic share (rule 4.5); default equal. This drives
    #: allocation — NOT the calibrated sensor gain, which is an observation
    #: model only (§3.1: the sensor's placement, not the channel's presence).
    weight: float = 1.0
    #: Calibrated lux gain at the sensor (observation model, §3.1). 1.0 until
    #: a calibration sweep (§4.4) lands; never used for allocation (rule 4.5).
    gain: float = 1.0

    @property
    def ct_capable(self) -> bool:
        return self.ct_range is not None


BandMap = Mapping[Band, float]


@dataclass(frozen=True, slots=True)
class Profile:
    """A room's tier values and open-loop tables (§2, §4.6, §6).

    Lux tiers (``lux_*``) are carried for the closed-loop path (§2.1) but are
    unused while every room runs open-loop; the open-loop tables
    (``out_*``) are the authority now (§4.6). Each ``out_*`` maps a band to a
    normalized channel output in [0, 1].
    """

    vacancy: Vacancy = Vacancy.DIM
    # Open-loop tables (rule 4.6), per band.
    out_active_day: BandMap = field(default_factory=dict)
    out_active_evening: BandMap = field(default_factory=dict)
    out_background: BandMap = field(default_factory=dict)
    # Evening cap (rule 2.4): clamp on normalized output once E >= threshold.
    evening_output_cap: float = 1.0
    # Mode outputs.
    night_output: BandMap = field(default_factory=dict)  # rule 6.2 (fixed dim warm)
    tv_output: BandMap = field(default_factory=dict)  # rule 6.3 (room occupied)
    tv_output_empty: BandMap = field(default_factory=dict)  # rule 6.3 (room empty)
    # Closed-loop lux tiers (structure for §2.1; unused open-loop).
    lux_active_day: float = 0.0
    lux_active_evening: float = 0.0
    lux_background: float = 0.0
    lux_max: float = 1000.0


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """A controlled room (§0)."""

    room_id: str
    channels: tuple[ChannelConfig, ...]
    profile: Profile
    shape: RoomShape = RoomShape.PRESENCE
    neighbours: tuple[str, ...] = ()
    #: True when this room is part of the "living group" gating BACKGROUND
    #: and living_recently_active (rules 1.6, 2.4).
    living_group: bool = False
    #: Per-room ACTIVE hold override (rule 1.3); None = tunable default.
    hold_seconds: float | None = None
    #: Membership in the night-path set (rule 6.2).
    night_path: bool = False
    #: Room participates in TV mode when a TV plays (rule 6.3).
    tv_mode: bool = False
    #: Whether a usable lux sensor exists (§3.5). Always False this PR —
    #: every room runs open-loop; the flag is the closed-loop seam.
    has_lux_sensor: bool = False

    def channel(self, channel_id: str) -> ChannelConfig | None:
        return next((c for c in self.channels if c.channel_id == channel_id), None)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Full static configuration handed to the engine."""

    rooms: tuple[RoomConfig, ...]

    #: Rooms whose lights ignore master-off (rule 7.2) — outdoor rooms only.
    def room(self, room_id: str) -> RoomConfig | None:
        return next((r for r in self.rooms if r.room_id == room_id), None)

    def channel_room(self, channel_id: str) -> RoomConfig | None:
        return next((r for r in self.rooms if r.channel(channel_id) is not None), None)


# ---------------------------------------------------------------------------
# Runtime state (mutable, engine-owned)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChannelState:
    """Ledger baseline for one channel (engine-side, rules 8.2/8.3).

    ``commanded_b`` / ``commanded_ct`` are the last values the engine told
    the adapter to move *toward*; slew, min-delta and CT min-delta reference
    them. This is distinct from the adapter's echo ledger (§8.4).
    """

    commanded_b: float = 0.0
    commanded_ct: int | None = None
    on: bool = False


@dataclass(slots=True)
class RoomState:
    """Mutable per-room runtime state (rules 1, 6, 9)."""

    # Presence resolution (rule 1.1).
    primary: bool | None = None
    fallback: bool | None = None
    #: When the primary went unavailable (None); drives presence_blind_hold.
    primary_blind_since: datetime | None = None
    #: Last definitive resolved occupancy (held while blind, rule 1.1/1.8).
    last_definitive_occupied: bool = False
    # Activity (rule 1.3).
    activity: Activity | None = None
    episode_peak: Activity | None = None
    # Holds (absolute expiry instants; None = not held).
    vacancy_hold_until: datetime | None = None
    trigger_hold_until: datetime | None = None
    # Unknown-presence demotion clock (rule 1.8).
    blind_freeze_until: datetime | None = None
    #: Extra demotion tiers accrued while fully blind (rule 1.8).
    blind_steps: int = 0
    # Living memory (rule 1.6): when this room last left ACTIVE.
    last_active_end: datetime | None = None
    self_active: bool = False
    role: Role = Role.OFF
    # Outdoor occupational switch (rule 6.5).
    occupational: bool = False
    # Override latch (rule 9).
    overridden: bool = False
    override_since: datetime | None = None
    channels: dict[str, ChannelState] = field(default_factory=dict)


@dataclass(slots=True)
class EngineState:
    """Aggregate engine state; the adapter reads this to publish entities."""

    enabled: bool = True
    # Circadian inputs (rule 2.3).
    sun_elevation: float | None = None
    # Modes (rule 6).
    sleep: bool = False
    anyone_home: bool | None = None
    vacation: bool = False
    #: Outdoor presence-simulation while away (rule 6.4, §10 switch; default on).
    away_lighting: bool = True
    tv_playing: bool = False
    night_active: bool = False
    night_hold_until: datetime | None = None
    # Master gain (rule 7).
    master_on: bool = True
    master_pct: float = 50.0
    #: Circadian factor at the previous recompute (rule 7.3): neutral drift is
    #: edge-triggered on the E>0 -> E==0 morning transition, so booting at
    #: midday never clobbers a restored gain. ``None`` until the first review.
    last_e: float | None = None
    started: bool = False
    start_at: datetime | None = None
    rooms: dict[str, RoomState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InitialSnapshot:
    """Point-in-time world state to seed the engine at startup (§11).

    Occupancy/levels are adopted as ledger baselines so a restart never
    flashes the lights (rule 11.1). Override latches are never restored
    (rule 11.2).
    """

    enabled: bool = True
    sun_elevation: float | None = None
    sleep: bool = False
    anyone_home: bool | None = None
    vacation: bool = False
    away_lighting: bool = True
    tv_playing: bool = False
    master_on: bool = True
    master_pct: float = 50.0
    #: room_id -> primary occupancy (rule 11.1).
    occupancy: Mapping[str, bool | None] = field(default_factory=dict)
    activity: Mapping[str, Activity | None] = field(default_factory=dict)
    occupational: Mapping[str, bool] = field(default_factory=dict)
    #: channel_id -> (normalized level, ct) currently reported; adopted as
    #: the ledger baseline (rule 11.1). Level 0 / absent => channel off.
    channels: Mapping[str, tuple[float, int | None]] = field(default_factory=dict)


class FluxModel(Protocol):
    """The flux conversions the governor needs (rules 4.3, 8.2).

    :class:`~.photometry.RoomPhotometry` satisfies this structurally, so the
    governor stays a feature module importing only model/tunables/plan.
    """

    def flux(self, channel_id: str, b: float) -> float: ...

    def command_for_flux(self, channel_id: str, f: float) -> float: ...


@dataclass(frozen=True, slots=True)
class RoomDiagnostics:
    """Published per-room diagnostics (rule 10)."""

    room_id: str
    role: Role
    overridden: bool
    target_output: float  # highest band's normalized target (open-loop)
    natural_lux: float | None  # placeholder until the estimator PR (§3)
