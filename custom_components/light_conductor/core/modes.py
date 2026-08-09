"""Modes: sleep, night path, TV, away, outdoor, vacation (ENGINE_SPEC §6).

A mode can override the role FSM entirely. :func:`resolve` returns a
:class:`RoomResolution` when a mode governs the room, or ``None`` to hand
control back to the normal role/tier path. The engine applies master gain
and the evening cap around this; modes only declare intent.

Feature-module discipline: imports only :mod:`model` and :mod:`tunables`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .model import Band, EngineState, Role, RoomConfig, RoomShape, RoomState, TvState
from .tunables import Tunables


@dataclass(frozen=True, slots=True)
class RoomResolution:
    """A mode's verdict for one room.

    ``band_outputs`` None means "use the tier machinery" (only for the
    normal path, never returned by a mode). ``off`` forces every channel
    off. ``gain_exempt`` excludes the room from master-gain scaling (night
    path and outdoor, rules 7.4/7.2). ``suppress_override`` lets night path
    win over a latched override (rule 9.1). ``fade`` overrides the ramp.
    """

    role: Role
    band_outputs: dict[Band, float] | None = None
    ct_override: int | None = None
    gain_exempt: bool = False
    off: bool = False
    fade: float | None = None
    suppress_override: bool = False
    #: An OFF that must NOT release a latched override (rule 6.5b): the outdoor
    #: daylight-OFF while occupational is on — a declared occupant's manual
    #: daylight level must stand. Sleep/away hard-offs never set this.
    respect_override: bool = False


def is_away(state: EngineState) -> bool:
    """Away/vacation predicate (rules 6.4, 6.6).

    ``anyone_home is None`` fails safe as home (rule 6.4).
    """
    return state.vacation or state.anyone_home is False


def resolve(
    room: RoomConfig,
    rs: RoomState,
    state: EngineState,
    e: float,
    tun: Tunables,
    night_expiring: bool = False,
    dusk: float | None = None,
    tv: TvState = TvState.OFF,
) -> RoomResolution | None:
    """Mode verdict for ``room``, or ``None`` for the normal role path.

    ``night_expiring`` is set for the one recompute in which the night-path
    episode ends, so its rooms fade out over ``night_fade`` (rule 6.2) rather
    than the ``sleep_fade`` used when sleep first engages (rule 6.1).

    ``dusk`` is the outdoor dusk factor (§6.5a), computed by the engine because
    it depends on sensor freshness; ``None`` keeps the pre-6.5a E gate.

    ``tv`` is the *effective* tri-state TV input (rule 6.3): the raw state held
    at PLAYING while the §6.3a pause grace runs. The engine owns that clock, so
    it passes the resolved value in rather than reading ``state.tv``.
    """
    if is_away(state):
        # Everyone gone: every indoor room OFF (rule 6.4). Outdoor rooms keep
        # their dusk background as presence simulation while away_lighting is
        # on, with the occupational switch ignored until someone is home (6.5).
        if room.shape is RoomShape.OUTDOOR:
            if state.away_lighting:
                return _outdoor(room, rs, e, tun, ignore_occupational=True, dusk=dusk)
            return RoomResolution(Role.OFF, off=True, gain_exempt=True)
        return RoomResolution(Role.OFF, off=True)

    if state.sleep:
        if state.night_active and room.night_path:
            return RoomResolution(
                Role.NIGHT_PATH,
                band_outputs=dict(room.profile.night_output),
                ct_override=tun.ct_min_evening,
                gain_exempt=True,  # rule 7.4
                fade=tun.night_fade,
                suppress_override=True,  # rule 9.1
            )
        # Sleep with no night-path role: OFF. A night-path room whose episode
        # just expired fades over night_fade (rule 6.2); otherwise sleep_fade
        # (rule 6.1).
        fade = tun.night_fade if (room.night_path and night_expiring) else tun.sleep_fade
        return RoomResolution(Role.OFF, off=True, fade=fade)

    if room.shape is RoomShape.OUTDOOR:
        return _outdoor(room, rs, e, tun, dusk=dusk)

    if tv is TvState.PLAYING and room.tv_mode:
        table = room.profile.tv_output if rs.self_active else room.profile.tv_output_empty
        return RoomResolution(Role.TV, band_outputs=dict(table))

    # TV ON (paused / powered on, not playing) does NOT resolve the room: it
    # caps the normal tier path (rule 6.3), applied by the engine via
    # :func:`tv_cap` once the outputs exist.
    return None


def tv_cap(room: RoomConfig, rs: RoomState, tv: TvState) -> dict[Band, float] | None:
    """Per-band output ceiling while the TV is ON but not playing (rule 6.3).

    ``None`` means "no ceiling" — the TV is off or playing (playing is a mode
    resolution, not a cap), or the room does not participate in TV mode. The
    caller applies it to the *final* per-channel outputs of the normal tier
    path: ``b_i <- min(b_i, cap[band_i])``. A cap can only take light away, so
    a room the tier path already leaves dark stays dark and a TV switched on in
    daylight (where §4.7 has already damped the room) changes nothing. An unset
    (empty) paused table is likewise no ceiling — the adapter always supplies
    one, so this is the core-default "TV ON does nothing" behaviour.
    """
    if tv is not TvState.ON or not room.tv_mode:
        return None
    table = room.profile.tv_output_paused if rs.self_active else room.profile.tv_output_paused_empty
    return dict(table)


def _outdoor(
    room: RoomConfig,
    rs: RoomState,
    e: float,
    tun: Tunables,
    ignore_occupational: bool = False,
    dusk: float | None = None,
) -> RoomResolution:
    """Outdoor room dusk logic (rules 6.5, 6.5a).

    ``ignore_occupational`` (set while away, rule 6.4) pins the room to its
    ambient background regardless of the occupational switch.

    ``dusk`` in [0, 1] is the engine's dusk factor (§6.5a): 0 = still daylight
    (room OFF), 1 = full dark (the room's tier as prescribed), between = the
    tier scaled down so the balcony eases in as the light goes rather than
    snapping on at a sun-ramp threshold. ``None`` (no sensor context — the
    pre-6.5a caller) falls back to the all-or-nothing E gate.
    """
    if dusk is None:
        dusk = 1.0 if e >= tun.outdoor_on_threshold else 0.0
    if dusk <= 0.0:
        # Daylight OFF. While an occupant is DECLARED (occupational on, not
        # away), a latched manual level stands — hard-offing would counter the
        # very press that §6.5b just turned into the declaration. With no
        # declared occupant this stays the pre-6.5b hard-off, so a stray
        # dialed level is still cleaned up on the morning descent.
        return RoomResolution(
            Role.OFF,
            off=True,
            gain_exempt=True,
            respect_override=rs.occupational and not ignore_occupational,
        )
    if rs.occupational and not ignore_occupational:
        # "Sitting outside": brighter evening level at a slightly cooler CT.
        return RoomResolution(
            Role.ACTIVE,
            band_outputs=_scale(room.profile.out_active_evening, dusk),
            ct_override=tun.ct_evening,
            gain_exempt=True,
        )
    # Ambient backdrop: background level, warm.
    return RoomResolution(
        Role.BACKGROUND,
        band_outputs=_scale(room.profile.out_background, dusk),
        ct_override=tun.ct_min_evening,
        gain_exempt=True,
    )


def _scale(table: Mapping[Band, float], factor: float) -> dict[Band, float]:
    """Scale a band table by the dusk factor (rule 6.5a)."""
    if factor >= 1.0:
        return dict(table)
    return {b: v * factor for b, v in table.items()}
