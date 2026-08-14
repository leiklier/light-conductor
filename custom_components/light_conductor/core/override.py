"""Manual override & reconciliation (ENGINE_SPEC §9).

A non-echo channel change latches the *room* OVERRIDDEN (rule 9.1): the
engine adopts the observed levels as the room's goal and stops adjusting it.
Mode hard-offs win exactly once, at their ONSET edge — the engine releases
every latch when sleep/away/vacation engages (rule 9.2) — while a latch
minted DURING a standing mode is deliberate manual control and is respected
(rules 6.1/6.4). The latch otherwise releases on an OFF-worthy vacancy
(presence-capable rooms only), a master power cycle, or ``override_timeout``
(rule 9.2). Release is handled by the engine re-entering normal control with
slew ramps (no jumps).

Feature-module discipline: imports only :mod:`model` and :mod:`tunables`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .model import ChannelState, RoomState
from .tunables import Tunables


def latch(rs: RoomState, now: datetime) -> None:
    """Latch the room OVERRIDDEN (rule 9.1)."""
    rs.overridden = True
    rs.override_since = now


def release(rs: RoomState) -> None:
    """Clear the override latch (rule 9.2)."""
    rs.overridden = False
    rs.override_since = None


def adopt(cs: ChannelState, level: float | None, ct: int | None) -> None:
    """Adopt an observed channel level as the engine's goal (rule 9.1).

    A ``None``/zero level means the channel was turned off manually.
    """
    if level is None or level <= 0.0:
        cs.on = False
        cs.commanded_b = 0.0
    else:
        cs.on = True
        cs.commanded_b = max(0.0, min(1.0, level))
    if ct is not None:
        cs.commanded_ct = ct


def timed_out(rs: RoomState, now: datetime, tun: Tunables) -> bool:
    """Whether the override has exceeded ``override_timeout`` (rule 9.2)."""
    return (
        rs.override_since is not None
        and (now - rs.override_since).total_seconds() >= tun.override_timeout
    )


def should_release(
    rs: RoomState,
    off_worthy: bool,
    presence_capable: bool,
    now: datetime,
    tun: Tunables,
) -> bool:
    """Whether a latched override should clear now (rule 9.2).

    Mode ONSETS (sleep/away/vacation engaging) release at the event fold, and
    master power cycles at their event site — neither is re-checked here, so
    a latch minted WHILE a mode stands (the 03:00 reading light) survives the
    standing mode (rules 6.1/6.4; only its timeout ends it, enforced by the
    engine's respect branch). ``off_worthy`` is True when the room's natural
    role has decayed to OFF (hold expiry at the OFF tier) — it releases the
    latch only for ``presence_capable`` rooms, where OFF-decay means the room
    was OBSERVED vacant. In a blind room (door/corridor triggers only)
    OFF-decay merely means the trigger hold ran out while the occupant may
    still be there; releasing would instantly counter their manual setting
    with the vacancy tier (the soverom wall-dial incident), so blind rooms
    hold the latch until timeout/mode-onset/master-cycle.
    """
    if not rs.overridden:
        return False
    if timed_out(rs, now, tun):
        return True
    return off_worthy and presence_capable


def override_review(rs: RoomState, now: datetime, tun: Tunables) -> datetime | None:
    """When the override timeout will fire, for scheduling (rule 9.2)."""
    if rs.overridden and rs.override_since is not None:
        return rs.override_since + timedelta(seconds=tun.override_timeout)
    return None
