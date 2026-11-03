"""§9.2 (D30): the timeout hand-back waits while someone is in the room.

User decision: a manual override must not hand back while someone is still in
the room. With D28, ``override_timeout`` (4 h) counts from the last genuine
touch; once it has elapsed the release waits for the room to stop being
self-active, then normal control resumes with slew ramps.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    DoorLightingChanged,
    ForeignChange,
    PresenceChanged,
    ReviewTick,
    SleepChanged,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import InitialSnapshot

from .helpers import apartment, at, offs, review, sets

NIGHT_SUN = -8.0
TIMEOUT = timedelta(hours=4)


def _latched_sofakrok() -> tuple[Engine, object]:
    """sofakrok occupied all evening, its lamp dialled by hand at 18:00."""
    eng = Engine(apartment(), InitialSnapshot(occupancy={"sofakrok": True}))
    t = at(1, 17, 59)
    eng.handle(SunElevationChanged(NIGHT_SUN), t)
    t0 = at(1, 18, 0)
    eng.handle(ForeignChange("sofakrok_taklys", 0.8), t0)
    assert eng.state.rooms["sofakrok"].overridden
    return eng, t0


def test_occupied_at_the_timeout_stays_latched() -> None:
    """D30: four hours later the occupant is still on the sofa — the dial stands
    (beta.19 handed the room back and dimmed it to the 22:00 tier)."""
    eng, t0 = _latched_sofakrok()
    cmds = eng.handle(ReviewTick(), t0 + TIMEOUT)
    rs = eng.state.rooms["sofakrok"]
    assert rs.overridden
    assert "sofakrok_taklys" not in sets(cmds) and "sofakrok_taklys" not in offs(cmds)
    # No busy loop: a deferred hand-back schedules nothing in the past.
    nxt = review(cmds)
    assert nxt is None or nxt > t0 + TIMEOUT
    cmds = eng.handle(ReviewTick(), t0 + TIMEOUT + timedelta(minutes=30))
    assert rs.overridden


def test_it_releases_when_the_room_stops_being_self_active() -> None:
    """D30: the occupant leaves at 22:30; the vacancy hold runs out and the
    room is handed back at that instant (the hold expiry review brings it),
    re-entering normal control with a slew ramp — not at the next timeout."""
    eng, t0 = _latched_sofakrok()
    eng.handle(ReviewTick(), t0 + TIMEOUT)
    left = t0 + TIMEOUT + timedelta(minutes=30)
    cmds = eng.handle(PresenceChanged("sofakrok", False), left)
    rs = eng.state.rooms["sofakrok"]
    assert rs.overridden  # inside the vacancy hold: still self-active
    hold_end = rs.vacancy_hold_until
    assert hold_end is not None and review(cmds) <= hold_end
    cmds = eng.handle(ReviewTick(), hold_end)
    assert not rs.overridden
    cmd = sets(cmds)["sofakrok_taklys"]  # demoted tier, ramped
    assert cmd.level < 0.8 and cmd.ramp_seconds > 0.0


def test_return_inside_the_hold_keeps_it_deferred() -> None:
    """D30: coming back inside the vacancy hold is still occupancy."""
    eng, t0 = _latched_sofakrok()
    eng.handle(PresenceChanged("sofakrok", False), t0 + TIMEOUT + timedelta(minutes=1))
    eng.handle(PresenceChanged("sofakrok", True), t0 + TIMEOUT + timedelta(minutes=2))
    eng.handle(ReviewTick(), t0 + TIMEOUT + timedelta(minutes=10))
    assert eng.state.rooms["sofakrok"].overridden


def test_a_blind_room_keeps_the_plain_timeout() -> None:
    """D30: soverom with door lighting off can never be self-active, so its
    timeout fires on time, exactly as before."""
    eng = Engine(apartment())
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 17, 59))
    eng.handle(DoorLightingChanged("soverom", False), at(1, 17, 59, 30))
    t0 = at(1, 18, 0)
    eng.handle(ForeignChange("soverom_taklys", 0.6), t0)
    cmds = eng.handle(ReviewTick(), t0 + TIMEOUT - timedelta(seconds=1))
    assert eng.state.rooms["soverom"].overridden
    cmds = eng.handle(ReviewTick(), t0 + TIMEOUT)
    assert not eng.state.rooms["soverom"].overridden
    assert "soverom_taklys" in offs(cmds)


def test_a_trigger_hold_defers_too() -> None:
    """D30: a door-shaped room inside its trigger hold is self-active."""
    eng = Engine(apartment())
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 17, 59))
    t0 = at(1, 18, 0)
    eng.handle(ForeignChange("soverom_taklys", 0.6), t0)
    from custom_components.light_conductor.core.events import TriggerFired

    eng.handle(TriggerFired("soverom"), t0 + TIMEOUT - timedelta(seconds=10))
    eng.handle(ReviewTick(), t0 + TIMEOUT)
    rs = eng.state.rooms["soverom"]
    assert rs.overridden
    cmds = eng.handle(ReviewTick(), rs.trigger_hold_until)
    assert not rs.overridden and "soverom_taklys" in offs(cmds)


def test_a_respected_standing_hard_off_defers_while_occupied() -> None:
    """D30 under standing sleep: the 03:00 reading light latched in an occupied
    room is not hard-offed at 07:00 while the reader is still there; it goes
    when they leave."""
    eng = Engine(apartment(), InitialSnapshot(occupancy={"kontor": True}))
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 2, 59))
    eng.handle(SleepChanged(True), at(1, 3, 0))
    t0 = at(1, 3, 1)
    eng.handle(ForeignChange("kontor_taklys", 0.5), t0)
    eng.handle(ReviewTick(), t0 + TIMEOUT)
    rs = eng.state.rooms["kontor"]
    assert rs.overridden
    eng.handle(PresenceChanged("kontor", False), t0 + TIMEOUT + timedelta(minutes=5))
    cmds = eng.handle(ReviewTick(), rs.vacancy_hold_until)
    assert not rs.overridden and "kontor_taklys" in offs(cmds)
