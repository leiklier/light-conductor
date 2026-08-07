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
from itertools import pairwise
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
    #: Affine per-channel RESPONSE MAPPING (rule 4.5). In the open-loop path the
    #: channel's command is ``clamp(response_slope · out + response_offset, 0, 1)``
    #: where ``out`` is the channel's post-weight band output (after weight share
    #: and the boost evening lockout). It aligns fixtures whose physical dimming
    #: curves differ (a steep LED strip vs. flat spots) so a lone-channel band no
    #: longer blasts over its neighbours. Defaults (1.0/0.0) are an exact no-op.
    #: A zero band output ALWAYS stays 0 — a positive offset must never light a
    #: channel whose band is off (the mapping applies only when ``out > 0``). The
    #: CLOSED-loop path is untouched: there the calibrated lux curves own the
    #: physical response (§3.1, ADR D16).
    response_slope: float = 1.0
    response_offset: float = 0.0

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
    #: Whether the room can OBSERVE vacancy (a presence or occupancy sensor is
    #: configured). Blind rooms (door/corridor with triggers only) decay to the
    #: OFF role on hold expiry without anyone having left — their manual
    #: overrides must not release on that decay (rule 9.2).
    presence_capable: bool = True

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
    #: When the engine last EMITTED a write for this room (§6.5b stale-zero
    #: guard). Unlike est.last_own_command_at this is unconditional.
    last_own_write_at: datetime | None = None
    # Override latch (rule 9).
    overridden: bool = False
    override_since: datetime | None = None
    channels: dict[str, ChannelState] = field(default_factory=dict)
    # Closed-loop estimator (§3) and calibration sweep (§4.4).
    est: EstimatorState = field(default_factory=lambda: EstimatorState())
    cal: CalibrationSession | None = None


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


class PhotometricModel(FluxModel, Protocol):
    """Flux conversions plus the calibrated lux gain (rules 3.1, 4.5).

    The estimator and calibration modules need the observation model
    (``gain``) on top of the flux curve, but must not import
    :mod:`photometry`; :class:`~.photometry.RoomPhotometry` satisfies this
    structurally, so it is passed in as this protocol (house discipline).
    """

    def gain(self, channel_id: str) -> float: ...


# ---------------------------------------------------------------------------
# Estimator & calibration state (§3, §4.4)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EstimatorState:
    """Per-room closed-loop estimator state (ENGINE_SPEC §3).

    All time-derived quantities advance only through event timestamps
    (rule 0). ``None`` filter/report stamps mean "no sample yet" — the room
    then runs open-loop (rule 3.5) until the first fresh :class:`LuxReport`.
    """

    #: Asymmetric-low-pass filtered lux ``L_filt`` (rule 3.2); None until seed.
    l_filt: float | None = None
    #: Artificial estimate ``Â`` filtered with the *same* low-pass as ``L_filt``
    #: so both lag together — the residual ``L_filt - Â_filt`` then stays
    #: consistent through an own-command transient instead of spiking (3.2).
    a_filt: float = 0.0
    #: Published natural-light estimate ``N̂`` (rule 3.2/3.3).
    n_hat: float = 0.0
    #: Online per-room scalar gain multiplier. For a calibrated room it is
    #: bounded [0.5, 2.0] of the calibrated gains (rule 3.4); for an
    #: uncalibrated room it holds the first-night bootstrap gain over the
    #: default b² curves once ``bootstrap_confident`` (rule 3.5/4.4).
    gain_mult: float = 1.0
    #: True once the first-night bootstrap has a conservative room-scalar gain
    #: and the room may enter closed-loop control (rule 3.5/4.4). Per-run only —
    #: never persisted; a restart re-learns from open-loop.
    bootstrap_confident: bool = False
    #: Own-step gain ratios (ΔL / Δflux) collected while bootstrapping (3.5/4.4).
    bootstrap_ratios: list[float] = field(default_factory=list)
    #: Last lux sample arrival (any sample, incl. blanked) — staleness (3.5).
    last_report_at: datetime | None = None
    #: Last sample folded into ``l_filt`` — the low-pass ``dt`` source (3.2).
    last_filt_at: datetime | None = None
    #: Last own channel command in the room — the write-blank window (3.2a).
    last_own_command_at: datetime | None = None
    #: When the sustained control error may be acted on — the deadband must
    #: stay violated until this instant before a correction lands (rule 3.6).
    #: A role/mode edge re-bases it to the shortened ``error_sustain_fast``.
    error_sustain_until: datetime | None = None
    # --- online gain-refinement pending step (rule 3.4) -------------------
    #: ``L_filt`` captured when a feed-forward step was emitted.
    pending_l_before: float | None = None
    #: Predicted ΔL of that step at ``gain_mult == 1`` (calibrated gains only).
    pending_base_delta: float | None = None
    #: When the step is deemed settled and the observation may be taken.
    pending_settle_at: datetime | None = None
    #: Cleared the moment a second own command lands inside the settle window
    #: (a non-quiet window never updates the gain, rule 3.4).
    pending_valid: bool = False
    #: Whether the pending observation feeds the first-night bootstrap (armed on
    #: observed ΔL) rather than the calibrated §3.4 refine (rule 3.5/4.4).
    pending_shadow: bool = False
    #: Latched §4.7 daylight factor held steady while a shadow observation
    #: settles: N̂-driven damping would otherwise nudge the open-loop output by
    #: sub-min_delta amounts each tick, re-commanding and disrupting the
    #: first-night bootstrap measurement. ``None`` until the first daylight scale.
    daylight_latch: float | None = None


_CAL_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class RoomCalibration:
    """Persisted photometric calibration for one room (ENGINE_SPEC §4.4).

    Plain data bound to the room's channel set: ``gains`` is ``g_i`` (lux at
    the sensor at full output) and ``curves`` is the per-channel relative-flux
    piecewise ``(b, flux)`` points ``f_i``. The adapter stores :meth:`to_dict`
    and reloads via :meth:`from_dict`, which **validates** the payload and
    raises :class:`ValueError` on anything malformed so the adapter discards it
    (the room then stays uncalibrated, rule 5 of the estimator brief).
    :meth:`matches` guards the channel-set contract; :meth:`validate` guards the
    numeric contract (finite, positive gains; monotone curves spanning b=0..1).
    """

    room_id: str
    gains: Mapping[str, float]
    curves: Mapping[str, tuple[tuple[float, float], ...]]

    def matches(self, channel_ids: tuple[str, ...]) -> bool:
        """Whether this calibration is bound to exactly ``channel_ids``."""
        ids = set(channel_ids)
        return set(self.gains) == ids and set(self.curves) == ids

    def validate(self) -> None:
        """Raise :class:`ValueError` unless the calibration is well-formed (rule 5).

        Every gain must be finite and > 0; every curve must have finite points,
        be non-decreasing in both ``b`` and flux, start at ``b=0`` and end at
        ``b=1`` (within epsilon). A corrupt store (NaN, negative gain,
        non-monotone or truncated curve) is rejected, not silently trusted.
        """
        from math import isfinite

        if set(self.gains) != set(self.curves):
            raise ValueError("calibration gains and curves cover different channels")
        for cid, g in self.gains.items():
            if not isfinite(g) or g <= 0.0:
                raise ValueError(f"channel {cid}: gain must be finite and > 0 (got {g})")
        for cid, pts in self.curves.items():
            if len(pts) < 2:
                raise ValueError(f"channel {cid}: curve needs >= 2 points")
            for b, f in pts:
                if not (isfinite(b) and isfinite(f)):
                    raise ValueError(f"channel {cid}: non-finite curve point")
            bs = [b for b, _f in pts]
            fs = [f for _b, f in pts]
            if abs(bs[0]) > _CAL_EPS or abs(bs[-1] - 1.0) > _CAL_EPS:
                raise ValueError(f"channel {cid}: curve must span b=0..1")
            if abs(fs[0]) > _CAL_EPS or abs(fs[-1] - 1.0) > _CAL_EPS:
                raise ValueError(f"channel {cid}: relative flux must span 0..1")
            if any(b1 - b0 < -_CAL_EPS for b0, b1 in pairwise(bs)):
                raise ValueError(f"channel {cid}: curve not monotone in b")
            if any(f1 - f0 < -_CAL_EPS for f0, f1 in pairwise(fs)):
                raise ValueError(f"channel {cid}: curve not monotone in flux")

    def is_valid(self) -> bool:
        """Non-raising :meth:`validate` (for the engine's silent load path)."""
        try:
            self.validate()
        except ValueError:
            return False
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "room_id": self.room_id,
            "gains": dict(self.gains),
            "curves": {cid: [list(p) for p in pts] for cid, pts in self.curves.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RoomCalibration:
        """Rebuild + validate a persisted calibration; raises on corruption (rule 5)."""
        gains = {str(k): float(v) for k, v in dict(data["gains"]).items()}  # type: ignore[arg-type]
        curves = {
            str(cid): tuple((float(b), float(f)) for b, f in pts)  # type: ignore[misc]
            for cid, pts in dict(data["curves"]).items()  # type: ignore[arg-type]
        }
        cal = cls(room_id=str(data["room_id"]), gains=gains, curves=curves)
        cal.validate()
        return cal


class CalPhase(StrEnum):
    """Calibration sweep phase (rule 4.4)."""

    SETTLE_OFF = "settle_off"  # all channels off, waiting for the room to settle
    DWELL = "dwell"  # one channel at one level, collecting lux
    DONE = "done"  # terminal (result emitted, session torn down)


@dataclass(slots=True)
class CalibrationSession:
    """In-flight calibration sweep for one room (rule 4.4).

    Transactional: ``prior_cal`` / ``prior_light`` snapshot the pre-sweep
    world so any abort restores it exactly. ``measurements[cid][level]`` is the
    settled lux recorded for a channel at a commanded level.
    """

    channel_order: tuple[str, ...]
    phase: CalPhase = CalPhase.SETTLE_OFF
    channel_index: int = 0
    level_index: int = 0
    deadline: datetime | None = None
    samples: list[float] = field(default_factory=list)
    measurements: dict[str, dict[float, float]] = field(default_factory=dict)
    off_baseline: float | None = None
    prior_cal: RoomCalibration | None = None
    #: Whether the room was already calibrated before this sweep (rollback flag).
    prior_calibrated: bool = False
    #: cid -> (commanded_b, commanded_ct, on) captured at sweep start.
    prior_light: dict[str, tuple[float, int | None, bool]] = field(default_factory=dict)
    #: Set by a foreign change in the room — the next step aborts (rule 4.4).
    foreign: bool = False


@dataclass(frozen=True, slots=True)
class RoomDiagnostics:
    """Published per-room diagnostics (rule 10)."""

    room_id: str
    role: Role
    overridden: bool
    target_output: float  # highest band's normalized target (open-loop)
    #: Estimated natural light N̂ at the sensor, rounded to 0.1 lx; None when the
    #: room has no fresh sensor (§3). The adapter buckets + rate-limits before
    #: the recorder (rule 10).
    natural_lux: float | None
    #: Closed-loop lux target T', rounded to 0.1 lx; None on the open-loop path.
    target_lux: float | None = None
