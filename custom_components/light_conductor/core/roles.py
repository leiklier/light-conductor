"""Room activity FSM (ENGINE_SPEC §1).

Pure functions over :class:`~.model.RoomState`; the engine calls them and
supplies the cross-room facts (neighbour-active, living-recently-active,
evening) they cannot see. Occupancy resolution, vacancy holds scaled by the
activity episode peak, corridor/door triggers, the blind hold, and gradual
demotion all live here.

Feature-module discipline: imports only :mod:`model` and :mod:`tunables`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .model import (
    Activity,
    Role,
    RoomShape,
    RoomState,
    Vacancy,
    max_activity,
)
from .tunables import Tunables


def hold_scale(peak: Activity | None, tun: Tunables) -> float:
    """Vacancy-hold multiplier from the episode peak (rule 1.3)."""
    if peak is Activity.PASSING:
        return tun.hold_passing_scale
    if peak is Activity.SETTLED:
        return tun.hold_settled_scale
    return 1.0


# --- input folding -------------------------------------------------------


def ingest_presence(
    rs: RoomState, primary: bool | None, fallback: bool | None, now: datetime
) -> None:
    """Fold a :class:`~.events.PresenceChanged` into the room (rule 1.1)."""
    if primary is None and rs.primary is not None:
        rs.primary_blind_since = now  # primary just went blind
    elif primary is not None:
        rs.primary_blind_since = None
    rs.primary = primary
    rs.fallback = fallback
    if primary is not None:
        rs.last_definitive_occupied = bool(primary) or bool(fallback)


def ingest_activity(rs: RoomState, activity: Activity | None) -> None:
    """Fold a :class:`~.events.ActivityChanged` (rule 1.3)."""
    rs.activity = activity
    if rs.self_active:
        rs.episode_peak = max_activity(rs.episode_peak, activity)


def ingest_trigger(rs: RoomState, closing: bool, now: datetime, tun: Tunables) -> None:
    """Fold a corridor/door trigger (rules 1.7, 1.9)."""
    hold = tun.door_close_hold if closing else tun.trigger_hold
    rs.trigger_hold_until = now + timedelta(seconds=hold)


# --- occupancy resolution (rule 1.1) -------------------------------------


def occupancy(rs: RoomState, now: datetime, tun: Tunables) -> bool | None:
    """Resolve room occupancy (rule 1.1). ``None`` = fully blind (rule 1.8).

    An unavailable primary holds its last definitive value for
    ``presence_blind_hold`` before falling back to the fallback entities
    (blind != absent).
    """
    if rs.primary is not None:
        return bool(rs.primary) or bool(rs.fallback)
    if (
        rs.primary_blind_since is not None
        and (now - rs.primary_blind_since).total_seconds() < tun.presence_blind_hold
    ):
        return rs.last_definitive_occupied
    if rs.fallback is not None:
        return bool(rs.fallback)
    return None


# --- self-active stepping ------------------------------------------------


def step(
    rs: RoomState,
    now: datetime,
    tun: Tunables,
    shape: RoomShape,
    hold_seconds: float | None,
    e: float = 0.0,
    dusk: float | None = None,
) -> None:
    """Advance ``self_active`` and its holds up to ``now`` (rules 1.3-1.9)."""
    if shape is RoomShape.OUTDOOR:
        # Outdoor rooms ignore presence SENSING (rule 6.5); modes.py owns their
        # own lighting. But the occupational switch is a *declaration* of
        # presence (rule 1.10): while it is on AND the evening is deep enough
        # to light the balcony itself (``e >= outdoor_on_threshold`` — the same
        # window as §6.5), the room counts as self-active so neighbours can
        # glow ADJACENT and a living-group balcony keeps living_recently_active
        # alive (the balcony-sitting incident — the interior went dark around
        # an occupant the sensors cannot see). Ungated, a switch left on would
        # light the interior in full daylight (the open-loop tier path has no
        # daylight damping for sensorless rooms). The falling edge — switch off
        # OR morning E-descent — stamps last_active_end so living_memory decays
        # normally.
        #
        # §6.5a: with a lux sensor the balcony's own dusk ramp starts earlier
        # than the E gate, but the interior must not follow it into ADJACENT
        # while it is still bright inside, so presence arms only once the ramp
        # is at least ``outdoor_presence_factor`` deep. With no dusk factor
        # (sensorless room) the fallback is binary and this reduces exactly to
        # the pre-6.5a ``e >= outdoor_on_threshold`` gate.
        lit = dusk if dusk is not None else (1.0 if e >= tun.outdoor_on_threshold else 0.0)
        active = rs.occupational and lit >= tun.outdoor_presence_factor
        if rs.self_active and not active:
            rs.last_active_end = now
        rs.self_active = active
        return
    if shape in (RoomShape.CORRIDOR, RoomShape.DOOR):
        _step_trigger(rs, now)
        return
    _step_presence(rs, now, tun, hold_seconds)


def _step_trigger(rs: RoomState, now: datetime) -> None:
    active = rs.trigger_hold_until is not None and now < rs.trigger_hold_until
    if not active and rs.trigger_hold_until is not None:
        rs.trigger_hold_until = None
    if rs.self_active and not active:
        rs.last_active_end = now
    rs.self_active = active


def _step_presence(rs: RoomState, now: datetime, tun: Tunables, hold_seconds: float | None) -> None:
    raw = occupancy(rs, now, tun)
    was_active = rs.self_active
    trigger_active = rs.trigger_hold_until is not None and now < rs.trigger_hold_until

    if raw is True:
        rs.blind_steps = 0
        rs.blind_freeze_until = None
        rs.vacancy_hold_until = None
        rs.episode_peak = (
            (rs.activity or Activity.EMPTY)
            if not was_active
            else max_activity(rs.episode_peak, rs.activity)
        )
        rs.self_active = True
        return

    if raw is False:
        rs.blind_steps = 0
        rs.blind_freeze_until = None
        if was_active and rs.vacancy_hold_until is None:
            base = tun.hold_seconds if hold_seconds is None else hold_seconds
            rs.vacancy_hold_until = now + timedelta(seconds=base * hold_scale(rs.episode_peak, tun))
        if rs.vacancy_hold_until is not None and now < rs.vacancy_hold_until:
            rs.self_active = True
        elif trigger_active:
            rs.self_active = True
            rs.vacancy_hold_until = None
        else:
            if was_active:
                rs.last_active_end = now
            rs.self_active = False
            rs.vacancy_hold_until = None
            rs.episode_peak = None
        return

    # raw is None: fully blind (rule 1.8). Only an ACTIVE room has a role to
    # freeze; a room that was already inactive just follows the normal
    # adjacency/background path (its blindness must not suppress a
    # neighbour-driven role).
    if rs.blind_steps >= _MAX_BLIND_STEPS:
        # Bottomed out at OFF: stop scheduling reviews forever (F7).
        rs.blind_freeze_until = None
        rs.self_active = False
        return
    if not was_active and rs.blind_steps == 0:
        rs.blind_freeze_until = None
        rs.self_active = False
        return
    if rs.blind_freeze_until is None:
        # Freeze the ACTIVE role for presence_blind_hold (self_active stays True).
        rs.blind_freeze_until = now + timedelta(seconds=tun.presence_blind_hold)
        return
    while now >= rs.blind_freeze_until:
        rs.blind_steps += 1
        rs.blind_freeze_until += timedelta(seconds=tun.presence_blind_hold)
    if rs.blind_steps >= 1:
        rs.last_active_end = rs.last_active_end or now
        rs.self_active = False
    if rs.blind_steps >= _MAX_BLIND_STEPS:
        rs.blind_freeze_until = None  # reached OFF; no more demotion reviews (F7)


# --- role resolution -----------------------------------------------------


def demoted_role(
    shape: RoomShape,
    vacancy: Vacancy,
    neighbour_active: bool,
    living_active: bool,
    evening: bool,
) -> Role:
    """The highest role a non-self-active room qualifies for (rules 1.4-1.7)."""
    if shape is RoomShape.CORRIDOR:
        if neighbour_active:
            return Role.ADJACENT
        if living_active and evening:
            return Role.BACKGROUND
        return Role.OFF
    if vacancy is Vacancy.OFF:
        return Role.OFF  # kontor: straight to OFF after its hold (rule 1.4)
    if neighbour_active:
        return Role.ADJACENT
    if living_active:
        return Role.BACKGROUND
    return Role.OFF


def base_role(
    rs: RoomState,
    shape: RoomShape,
    vacancy: Vacancy,
    neighbour_active: bool,
    living_active: bool,
    evening: bool,
) -> Role:
    """The room's role from presence + adjacency (rules 1.2-1.8).

    A fully-blind room that was ACTIVE demotes gradually from ACTIVE, one
    tier per accrued step, never straight to OFF (rule 1.8); this overrides
    the adjacency path because §1.8 freezes and steps down the room's own
    (ACTIVE) role.
    """
    if rs.blind_steps >= 1:
        return lower(Role.ACTIVE, rs.blind_steps)
    if rs.self_active:
        return Role.ACTIVE
    return demoted_role(shape, vacancy, neighbour_active, living_active, evening)


_DEMOTION_LADDER: tuple[Role, ...] = (
    Role.ACTIVE,
    Role.ADJACENT,
    Role.BACKGROUND,
    Role.OFF,
)
#: Blind steps at which a room has demoted all the way to OFF (rule 1.8).
_MAX_BLIND_STEPS: int = len(_DEMOTION_LADDER) - 1


def lower(role: Role, steps: int) -> Role:
    """Demote ``role`` by ``steps`` tiers down the ACTIVE->OFF ladder (rule 1.8)."""
    if role not in _DEMOTION_LADDER:
        return role
    idx = min(_DEMOTION_LADDER.index(role) + steps, len(_DEMOTION_LADDER) - 1)
    return _DEMOTION_LADDER[idx]


def next_review(rs: RoomState, now: datetime) -> datetime | None:
    """Earliest future FSM review for this room (holds / blind clock)."""
    candidates = [
        t
        for t in (rs.vacancy_hold_until, rs.trigger_hold_until, rs.blind_freeze_until)
        if t is not None and t > now
    ]
    return min(candidates) if candidates else None
