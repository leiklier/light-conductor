"""Modes: sleep, night path, TV, away, outdoor, vacation (ENGINE_SPEC §6).

A mode can override the role FSM entirely. :func:`resolve` returns a
:class:`RoomResolution` when a mode governs the room, or ``None`` to hand
control back to the normal role/tier path. The engine applies master gain
and the evening cap around this; modes only declare intent.

Feature-module discipline: imports only :mod:`model` and :mod:`tunables`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Band, EngineState, Role, RoomConfig, RoomShape, RoomState
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
) -> RoomResolution | None:
    """Mode verdict for ``room``, or ``None`` for the normal role path.

    ``night_expiring`` is set for the one recompute in which the night-path
    episode ends, so its rooms fade out over ``night_fade`` (rule 6.2) rather
    than the ``sleep_fade`` used when sleep first engages (rule 6.1).
    """
    if is_away(state):
        # Everyone gone: every indoor room OFF (rule 6.4). Outdoor rooms keep
        # their dusk background as presence simulation while away_lighting is
        # on, with the occupational switch ignored until someone is home (6.5).
        if room.shape is RoomShape.OUTDOOR:
            if state.away_lighting:
                return _outdoor(room, rs, e, tun, ignore_occupational=True)
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
        return _outdoor(room, rs, e, tun)

    if state.tv_playing and room.tv_mode:
        table = room.profile.tv_output if rs.self_active else room.profile.tv_output_empty
        return RoomResolution(Role.TV, band_outputs=dict(table))

    return None


def _outdoor(
    room: RoomConfig,
    rs: RoomState,
    e: float,
    tun: Tunables,
    ignore_occupational: bool = False,
) -> RoomResolution:
    """Outdoor room dusk logic (rule 6.5).

    ``ignore_occupational`` (set while away, rule 6.4) pins the room to its
    ambient background regardless of the occupational switch.
    """
    if e < tun.outdoor_on_threshold:
        return RoomResolution(Role.OFF, off=True, gain_exempt=True)
    if rs.occupational and not ignore_occupational:
        # "Sitting outside": brighter evening level at a slightly cooler CT.
        return RoomResolution(
            Role.ACTIVE,
            band_outputs=dict(room.profile.out_active_evening),
            ct_override=tun.ct_evening,
            gain_exempt=True,
        )
    # Ambient backdrop: background level, warm.
    return RoomResolution(
        Role.BACKGROUND,
        band_outputs=dict(room.profile.out_background),
        ct_override=tun.ct_min_evening,
        gain_exempt=True,
    )
