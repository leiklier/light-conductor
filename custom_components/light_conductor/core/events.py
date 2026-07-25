"""Input events for the lighting engine.

The adapter translates HA state into these events and stamps each
``handle(event, now)`` call with an aware ``datetime`` (rule 0: time enters
only through event timestamps). The adapter is responsible for:

- aggregating fallback occupancy entities into one boolean before emitting
  :class:`PresenceChanged` (rule 1.1);
- echo suppression via its write ledger (rule 8.4): a state report matching
  a recent own command never reaches the engine. :class:`ForeignChange`
  therefore always means "someone else did this" (rule 9.1). Wall-event
  entities (rule 9.4) are surfaced as ``ForeignChange`` too, even inside
  echo tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Activity


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for all engine input events."""


# --- presence / activity / triggers (§1) --------------------------------


@dataclass(frozen=True, slots=True)
class PresenceChanged(Event):
    """Room occupancy update (rule 1.1).

    ``primary`` is the presence-conductor room occupancy (``None`` =
    unavailable/blind). ``fallback`` is the OR of the room's fallback
    occupancy entities (``None`` = none configured / all unavailable).
    """

    room_id: str
    primary: bool | None
    fallback: bool | None = None


@dataclass(frozen=True, slots=True)
class ActivityChanged(Event):
    """Rich activity classification changed (rule 1.3). ``None`` = blind."""

    room_id: str
    activity: Activity | None


@dataclass(frozen=True, slots=True)
class TriggerFired(Event):
    """Momentary trigger for a corridor / door-triggered room (rule 1.7/1.9).

    ``closing`` marks a door's closing edge (shortened hold, rule 1.9).
    """

    room_id: str
    closing: bool = False


@dataclass(frozen=True, slots=True)
class ForeignChange(Event):
    """A non-echo channel state change: latches an override (rule 9.1).

    ``level`` is the observed normalized output (``None`` / 0 => off).
    ``wall_event`` marks a Plejd wall-controller event (rule 9.4).
    """

    channel_id: str
    level: float | None
    ct: int | None = None
    wall_event: bool = False


# --- environment (§2, §6) -----------------------------------------------


@dataclass(frozen=True, slots=True)
class SunElevationChanged(Event):
    """Sun elevation in degrees (rule 2.3). Drives the ``E_sun`` term."""

    elevation_deg: float


@dataclass(frozen=True, slots=True)
class ReviewTick(Event):
    """A scheduled re-evaluation fired (rule 0 / 2.3 circadian tick).

    The adapter arms this from the most recent :class:`~.plan.ScheduleReview`
    command; the engine reads nothing but ``now``.
    """


@dataclass(frozen=True, slots=True)
class SleepChanged(Event):
    """Sleep mode toggled (rule 6.1)."""

    active: bool


@dataclass(frozen=True, slots=True)
class HomeChanged(Event):
    """Home-level presence (rule 6.4). ``None`` fails safe as home."""

    anyone_home: bool | None


@dataclass(frozen=True, slots=True)
class VacationChanged(Event):
    """Vacation mode toggled (rule 6.6)."""

    active: bool


@dataclass(frozen=True, slots=True)
class TvChanged(Event):
    """Any configured TV player started/stopped (rule 6.3)."""

    playing: bool


@dataclass(frozen=True, slots=True)
class NightTriggerFired(Event):
    """A night-path trigger fired while asleep (rule 6.2)."""


@dataclass(frozen=True, slots=True)
class OccupationalChanged(Event):
    """An outdoor room's ``occupational`` switch toggled (rule 6.5)."""

    room_id: str
    on: bool


# --- master gain / enable (§7, §10) -------------------------------------


@dataclass(frozen=True, slots=True)
class MasterGainChanged(Event):
    """Master-gain dimmer moved (rule 7.1). Implies the master light is on."""

    pct: float


@dataclass(frozen=True, slots=True)
class MasterPowerChanged(Event):
    """Master light powered on/off (rule 7.2). Off => gain 0, on => restore."""

    on: bool


@dataclass(frozen=True, slots=True)
class SetEnabled(Event):
    """Master enable switch (rule 10). Off => observe-only (no commands)."""

    enabled: bool


@dataclass(frozen=True, slots=True)
class SetAwayLighting(Event):
    """Away-lighting switch (rule 6.4, §10). On => outdoor presence simulation
    while away; off => outdoor rooms go dark on away too."""

    on: bool


# --- estimator placeholder (§3, deferred) --------------------------------


@dataclass(frozen=True, slots=True)
class LuxReport(Event):
    """A room lux-sensor reading (rule 3). Placeholder for the estimator PR.

    Stored on the room but ignored while the room runs open-loop; the field
    is the seam where closed-loop control reads measured illuminance.
    """

    room_id: str
    lux: float | None
