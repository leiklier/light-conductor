"""§6.2 (D32): an open night-path episode is held while a path room is occupied.

Since the bedroom door sensor came back the path opens correctly, but
``night_hold`` (600 s after the last trigger) ran out while someone was up:
11-02 the path faded 06:57:06 and the kitchen was lit by hand 06:57:37; 11-01
it went off 08:10:48 and sofakrok was lit by hand 08:19:15.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    NightTriggerFired,
    PresenceChanged,
    ReviewTick,
    SleepChanged,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import Role

from .helpers import apartment, at, offs, review, sets

NIGHT_SUN = -8.0
HOLD = timedelta(seconds=600)


def _episode() -> tuple[Engine, object]:
    eng = Engine(apartment())
    eng.handle(SunElevationChanged(NIGHT_SUN), at(2, 6, 0))
    eng.handle(SleepChanged(True), at(2, 6, 1))
    t = at(2, 6, 43, 42)
    eng.handle(NightTriggerFired(), t)
    assert eng.state.night_active
    return eng, t


def test_an_occupied_path_room_holds_the_episode() -> None:
    """D32: someone in the kitchen when night_hold runs out keeps the path lit
    (at night_output) until night_hold after they leave — then it fades over
    night_fade."""
    eng, t = _episode()
    level = eng.state.rooms["kjokken"].channels["kjokken_downlights"].commanded_b
    eng.handle(PresenceChanged("kjokken", True), t + timedelta(minutes=5))
    cmds = eng.handle(ReviewTick(), t + HOLD)  # the beta.19 fade instant
    assert eng.state.night_active
    assert eng.state.rooms["kjokken"].role is Role.NIGHT_PATH
    assert "kjokken_downlights" not in sets(cmds) and "kjokken_downlights" not in offs(cmds)
    assert eng.state.rooms["kjokken"].channels["kjokken_downlights"].commanded_b == level
    nxt = review(cmds)
    assert nxt is not None and nxt > t + HOLD  # it keeps re-checking itself

    left = t + timedelta(minutes=25)
    eng.handle(PresenceChanged("kjokken", False), left)
    eng.handle(ReviewTick(), left + HOLD - timedelta(seconds=1))
    assert eng.state.night_active
    cmds = eng.handle(ReviewTick(), left + HOLD)
    assert not eng.state.night_active
    off = [c for c in cmds if getattr(c, "channel_id", None) == "kjokken_downlights"]
    assert off and off[0].ramp_seconds == eng.tun.night_fade


def test_presence_never_opens_an_episode() -> None:
    """D32: only the configured night triggers open the path."""
    eng = Engine(apartment())
    eng.handle(SunElevationChanged(NIGHT_SUN), at(2, 3, 0))
    eng.handle(SleepChanged(True), at(2, 3, 1))
    eng.handle(PresenceChanged("sofakrok", True), at(2, 3, 30))
    assert not eng.state.night_active
    assert eng.state.rooms["sofakrok"].role is Role.OFF


def test_the_raw_fallback_does_not_hold_the_path() -> None:
    """D32: only presence-conductor's primary counts — the raw kitchen sensor
    was on ~91 min during one night's sleep."""
    eng, t = _episode()
    eng.handle(PresenceChanged("kjokken", False, True), t + timedelta(minutes=5))
    eng.handle(ReviewTick(), t + HOLD)
    assert not eng.state.night_active


def test_a_room_outside_the_path_does_not_hold_it() -> None:
    """D32: kontor is not a night-path room; its occupant does not hold the path."""
    eng, t = _episode()
    eng.handle(PresenceChanged("kontor", True), t + timedelta(minutes=5))
    eng.handle(ReviewTick(), t + HOLD)
    assert not eng.state.night_active


def test_presence_ending_inside_the_hold_still_extends_from_its_end() -> None:
    """D32: the occupant gets the full night_hold from the moment they leave,
    even when no review ran in between."""
    eng, t = _episode()
    eng.handle(PresenceChanged("sofakrok", True), t + timedelta(minutes=8))
    left = t + timedelta(minutes=9)
    eng.handle(PresenceChanged("sofakrok", False), left)
    assert eng.state.night_hold_until == left + HOLD
