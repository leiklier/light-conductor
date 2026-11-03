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

#: Fallback dropouts this short do not restart a raw spell (rule 1.1, D31).
#: The raw mmWave fallbacks re-trigger through near-instant off->on toggles —
#: about half of all kjøkken/sofakrok off-gaps are under 50 ms and ~60-73 %
#: under 2 s — so an unbridged "continuously on" would almost never accumulate
#: even on a person sitting still. An engineering constant, not a §12 tunable:
#: the operator surface is ``fallback_min_on``.
FALLBACK_BRIDGE = 2.0


def hold_scale(peak: Activity | None, tun: Tunables) -> float:
    """Vacancy-hold multiplier from the episode peak (rule 1.3)."""
    if peak is Activity.PASSING:
        return tun.hold_passing_scale
    if peak is Activity.SETTLED:
        return tun.hold_settled_scale
    return 1.0


# --- input folding -------------------------------------------------------


def ingest_presence(
    rs: RoomState,
    primary: bool | None,
    fallback: bool | None,
    now: datetime,
    tun: Tunables | None = None,
    min_on: float | None = None,
) -> None:
    """Fold a :class:`~.events.PresenceChanged` into the room (rule 1.1).

    Tracks the fallback's on-spell for episode confirmation (D31): a rising
    edge starts a new spell unless the fallback dropped out at most
    ``FALLBACK_BRIDGE`` ago, in which case the old spell continues. A spell
    that ends having persisted ``fallback_min_on`` confirms the room's current
    episode — evaluated HERE, before the falling edge is folded, because the
    vacancy hold this very edge starts must already be the normal one.
    """
    was_on = rs.fallback is True
    if fallback is True and not was_on:
        bridged = (
            rs.fallback_on_since is not None
            and rs.fallback_off_at is not None
            and (now - rs.fallback_off_at).total_seconds() <= FALLBACK_BRIDGE
        )
        if not bridged:
            rs.fallback_on_since = now
    elif was_on and fallback is not True:
        if rs.self_active and fallback_persisted(rs, now, tun, min_on):
            rs.episode_confirmed = True
        rs.fallback_off_at = now
    if primary is None and rs.primary is not None:
        rs.primary_blind_since = now  # primary just went blind
    elif primary is not None:
        rs.primary_blind_since = None
    rs.primary = primary
    rs.fallback = fallback
    if primary is not None:
        rs.last_definitive_occupied = bool(primary) or bool(fallback)


def _min_on(tun: Tunables | None, min_on: float | None) -> float:
    """The room's effective fallback persistence: its override, else the tunable."""
    if min_on is not None:
        return min_on
    return tun.fallback_min_on if tun is not None else 0.0


def fallback_persisted(
    rs: RoomState, now: datetime, tun: Tunables | None, min_on: float | None = None
) -> bool:
    """Whether the fallback's current spell is CONFIRMED evidence (D31).

    True while the fallback is on and its spell (dropouts up to
    ``FALLBACK_BRIDGE`` bridged) has lasted the room's effective
    ``fallback_min_on``. A ``fallback_min_on`` of 0 makes every spell
    confirmed (beta.19). Called on the falling edge BEFORE it is folded (the
    spell's full length), and by the engine for the away debounce (§6.4, D29).
    """
    if rs.fallback is not True:
        return False
    need = _min_on(tun, min_on)
    if need <= 0.0:
        return True
    return rs.fallback_on_since is not None and (now - rs.fallback_on_since).total_seconds() >= need


def ingest_activity(rs: RoomState, activity: Activity | None) -> None:
    """Fold a :class:`~.events.ActivityChanged` (rule 1.3)."""
    rs.activity = activity
    if rs.self_active:
        rs.episode_peak = max_activity(rs.episode_peak, activity)


def ingest_trigger(
    rs: RoomState,
    closing: bool,
    now: datetime,
    tun: Tunables,
    shape: RoomShape = RoomShape.DOOR,
) -> None:
    """Fold a corridor/door trigger (rules 1.7, 1.9).

    The door-lighting toggle gates INGESTION (rule 1.9): with it off no pulse —
    opening or closing — mints a hold, so the room simply has no trigger input
    while the occupant has switched the behaviour away.

    The closing edge depends on the room's shape (D34). A DOOR-shaped room
    (soverom) shortens to ``door_close_hold``: the door closing behind you
    means you left — or shut yourself in, and the dial is yours. A CORRIDOR
    ignores it: its triggers are pass-through contacts (the front door on
    gang), and closing the front door behind you must not darken the hallway
    15 s later — only the opening edge pulses ``trigger_hold``. A PRESENCE
    room with triggers keeps the door behaviour (its presence sensing carries
    the occupant past the short hold anyway).
    """
    if not rs.door_lighting:
        return
    if closing and shape is RoomShape.CORRIDOR:
        return
    hold = tun.door_close_hold if closing else tun.trigger_hold
    rs.trigger_hold_until = now + timedelta(seconds=hold)


# --- occupancy resolution (rule 1.1) -------------------------------------


def occupancy(
    rs: RoomState, now: datetime, tun: Tunables, min_on: float | None = None
) -> bool | None:
    """Resolve room occupancy (rule 1.1). ``None`` = fully blind (rule 1.8).

    The fallbacks are OR-ed with the primary and count at once — they are the
    fast detectors (presence-conductor lags them by 8-27 s). What D31 limits is
    the HOLD an unconfirmed raw-only episode gets (:func:`_step_presence`), not
    the turn-on. An unavailable primary holds its last definitive value for
    ``presence_blind_hold`` before falling back to the fallback entities alone
    (blind != absent). ``min_on`` is unused here (kept for call symmetry).
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
    fallback_min_on: float | None = None,
) -> None:
    """Advance ``self_active`` and its holds up to ``now`` (rules 1.3-1.9).

    ``fallback_min_on`` is the room's override of the §1.1 fallback
    persistence (D31); ``None`` uses the tunable.
    """
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
    _step_presence(rs, now, tun, hold_seconds, fallback_min_on)


def _step_trigger(rs: RoomState, now: datetime) -> None:
    active = rs.trigger_hold_until is not None and now < rs.trigger_hold_until
    if not active and rs.trigger_hold_until is not None:
        rs.trigger_hold_until = None
    if rs.self_active and not active:
        rs.last_active_end = now
    rs.self_active = active


def _step_presence(
    rs: RoomState,
    now: datetime,
    tun: Tunables,
    hold_seconds: float | None,
    fallback_min_on: float | None = None,
) -> None:
    raw = occupancy(rs, now, tun, fallback_min_on)
    was_active = rs.self_active
    trigger_active = rs.trigger_hold_until is not None and now < rs.trigger_hold_until

    if raw is True:
        rs.blind_steps = 0
        rs.blind_freeze_until = None
        rs.vacancy_hold_until = None
        if not was_active:
            rs.episode_confirmed = False  # a new episode starts unconfirmed (D31)
        rs.episode_peak = (
            (rs.activity or Activity.EMPTY)
            if not was_active
            else max_activity(rs.episode_peak, rs.activity)
        )
        # Confirmation (D31): presence-conductor saw someone, or a raw spell
        # has persisted fallback_min_on. Once confirmed, the episode keeps its
        # normal holds until it ends.
        if rs.primary is True or fallback_persisted(rs, now, tun, fallback_min_on):
            rs.episode_confirmed = True
        rs.self_active = True
        return

    if raw is False:
        rs.blind_steps = 0
        rs.blind_freeze_until = None
        if was_active and rs.vacancy_hold_until is None:
            base = tun.hold_seconds if hold_seconds is None else hold_seconds
            hold = base * hold_scale(rs.episode_peak, tun)
            if (
                not rs.episode_confirmed
                and rs.primary is not None  # blind path unchanged
                and _min_on(tun, fallback_min_on) > 0.0
            ):
                # Provisional hold (D31): an episode only unconfirmed raw
                # evidence ever supported — the raw sensors' blips — releases
                # after fallback_provisional_hold instead of the full hold.
                hold = min(hold, tun.fallback_provisional_hold)
            rs.vacancy_hold_until = now + timedelta(seconds=hold)
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
            rs.episode_confirmed = False
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


def next_review(
    rs: RoomState, now: datetime, tun: Tunables | None = None, min_on: float | None = None
) -> datetime | None:
    """Earliest future FSM review for this room (holds / blind clock / D31).

    With ``tun`` supplied, a fallback spell that is on but has not yet
    persisted schedules the instant it will (rule 1.1, D31): the episode is
    confirmed and the away debounce (§6.4) sees confirmed evidence on time,
    without waiting for the next unrelated event.
    """
    pending = None
    need = _min_on(tun, min_on)
    if need > 0.0 and rs.fallback is True and rs.fallback_on_since is not None:
        pending = rs.fallback_on_since + timedelta(seconds=need)
    candidates = [
        t
        for t in (rs.vacancy_hold_until, rs.trigger_hold_until, rs.blind_freeze_until, pending)
        if t is not None and t > now
    ]
    return min(candidates) if candidates else None
