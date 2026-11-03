"""Manual override & reconciliation (ENGINE_SPEC §9).

A non-echo channel change latches the *room* OVERRIDDEN (rule 9.1): the
engine adopts the observed levels as the room's goal and stops adjusting it.
Mode hard-offs win exactly once, at their ONSET edge — the engine releases
every latch when sleep/away/vacation engages (rule 9.2) — while a latch
minted DURING a standing mode is deliberate manual control and is respected
(rules 6.1/6.4). The latch otherwise releases on an OFF-worthy vacancy
(presence-capable rooms only), a master power cycle, or ``override_timeout``
(rule 9.2). The timeout hand-back is DEFERRED while the room is occupied
(self-active: occupancy True, inside its vacancy hold, or inside a trigger
hold — D30): it releases at the first moment the room stops being self-active.
Release is handled by the engine re-entering normal control with slew ramps
(no jumps).

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
    cs.reconcile = False  # a level the user set is not a reconcile target


def timed_out(rs: RoomState, now: datetime, tun: Tunables) -> bool:
    """Whether the override has exceeded ``override_timeout`` (rule 9.2)."""
    return (
        rs.override_since is not None
        and (now - rs.override_since).total_seconds() >= tun.override_timeout
    )


def timeout_due(rs: RoomState, now: datetime, tun: Tunables, occupied: bool) -> bool:
    """Whether the timeout hands the room back NOW (rule 9.2, D30).

    The user decided manual overrides must not hand back while someone is
    still in the room: an elapsed timeout is deferred while ``occupied`` (the
    room is self-active — occupancy True, inside its vacancy hold, or inside a
    trigger hold) and fires at the first recompute in which it is not. A room
    that can never be self-active (no presence, no live triggers — soverom with
    door lighting off) keeps the plain timeout.
    """
    return timed_out(rs, now, tun) and not occupied


def should_release(
    rs: RoomState,
    off_worthy: bool,
    presence_capable: bool,
    now: datetime,
    tun: Tunables,
    occupied: bool = False,
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
    hold the latch until timeout/mode-onset/master-cycle. The timeout itself
    waits while the room is ``occupied`` (D30, :func:`timeout_due`).
    """
    if not rs.overridden:
        return False
    if timeout_due(rs, now, tun, occupied):
        return True
    return off_worthy and presence_capable


def override_review(rs: RoomState, now: datetime, tun: Tunables) -> datetime | None:
    """When the override timeout will fire, for scheduling (rule 9.2).

    ``None`` once the deadline has passed: a DEFERRED hand-back (D30) is not a
    timer — it fires on the recompute in which the room stops being
    self-active, which the occupancy event or the hold-expiry review (rule 1.3,
    :func:`~.roles.next_review`) already brings. A past instant here would ask
    the adapter for an immediate review on every recompute — a busy loop.
    """
    if rs.overridden and rs.override_since is not None:
        due = rs.override_since + timedelta(seconds=tun.override_timeout)
        if due > now:
            return due
    return None
