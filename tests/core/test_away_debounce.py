"""§6.4 (D29): the away ONSET is debounced.

presence-conductor's anyone_home reads off for 31-35 s after every HA restart
(longer than the 30 s startup grace) and flaps off for 2-7 min while the
untracked occupant is home alone (10-25 19:58:44 -> 20:01:46; 10-26 16:34:35
-> 16:36:53 and 16:40:34 -> 16:47:24). Each flap turned the whole house off
and released every latch.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    ForeignChange,
    HomeChanged,
    PresenceChanged,
    ReviewTick,
    SunElevationChanged,
    VacationChanged,
)
from custom_components.light_conductor.core.model import InitialSnapshot, Role
from custom_components.light_conductor.core.plan import TurnOffChannel

from .helpers import apartment, at, offs, review, sets

NIGHT_SUN = -8.0
DEBOUNCE = 600.0


def _lit_evening() -> tuple[Engine, object]:
    """An evening house: sofakrok occupied, kitchen latched by hand."""
    eng = Engine(apartment())
    t = at(1, 21, 0)
    eng.handle(SunElevationChanged(NIGHT_SUN), t)
    eng.handle(PresenceChanged("sofakrok", True), t + timedelta(seconds=31))
    eng.handle(PresenceChanged("kjokken", True), t + timedelta(seconds=32))
    eng.handle(ForeignChange("kjokken_taklys", 0.7), t + timedelta(seconds=33))
    t += timedelta(seconds=40)
    assert eng.state.rooms["kjokken"].overridden
    return eng, t


def test_a_flap_shorter_than_the_window_changes_nothing() -> None:
    """D29: the untracked occupant home alone — anyone_home False for 7 min with
    the radar quiet — neither hard-offs the house nor releases a latch."""
    eng, t = _lit_evening()
    eng.handle(PresenceChanged("sofakrok", False), t)
    eng.handle(PresenceChanged("kjokken", False), t)
    cmds = eng.handle(HomeChanged(False), t)
    assert not eng.state.away
    assert review(cmds) <= t + timedelta(seconds=DEBOUNCE)  # the expiry is self-scheduled
    for k in range(1, 8):
        cmds = eng.handle(ReviewTick(), t + timedelta(minutes=k))
        assert "kjokken_taklys" not in offs(cmds)
    cmds = eng.handle(HomeChanged(True), t + timedelta(minutes=7))
    assert not eng.state.away
    assert eng.state.rooms["kjokken"].overridden  # the latch survived the flap
    assert not any(isinstance(c, TurnOffChannel) and c.channel_id == "kjokken_taklys" for c in cmds)


def test_a_genuine_departure_engages_at_the_window_end() -> None:
    """D29: an empty house for away_debounce engages away — the onset edge
    releases every latch and the indoor rooms go dark — and not a second
    sooner."""
    eng, t = _lit_evening()
    eng.handle(PresenceChanged("sofakrok", False), t)
    eng.handle(PresenceChanged("kjokken", False), t)
    eng.handle(HomeChanged(False), t)
    eng.handle(ReviewTick(), t + timedelta(seconds=DEBOUNCE - 1))
    assert not eng.state.away and eng.state.rooms["kjokken"].overridden
    cmds = eng.handle(ReviewTick(), t + timedelta(seconds=DEBOUNCE))
    assert eng.state.away
    assert not eng.state.rooms["kjokken"].overridden
    assert "kjokken_taklys" in offs(cmds)
    assert eng.state.rooms["sofakrok"].role is Role.OFF


def test_presence_during_the_window_cancels_and_restarts_it() -> None:
    """D29: a room's primary presence while anyone_home reads False is someone
    home — the window is cancelled and restarts when the room empties."""
    eng, t = _lit_evening()
    eng.handle(PresenceChanged("sofakrok", False), t)
    eng.handle(PresenceChanged("kjokken", False), t)
    eng.handle(HomeChanged(False), t)
    eng.handle(PresenceChanged("kjokken", True), t + timedelta(minutes=5))
    assert eng.state.away_pending_since is None
    left = t + timedelta(minutes=8)
    cmds = eng.handle(PresenceChanged("kjokken", False), left)
    assert eng.state.away_pending_since == left
    assert review(cmds) <= left + timedelta(seconds=DEBOUNCE)
    eng.handle(ReviewTick(), t + timedelta(seconds=DEBOUNCE))  # the ORIGINAL expiry
    assert not eng.state.away
    eng.handle(ReviewTick(), left + timedelta(seconds=DEBOUNCE))
    assert eng.state.away


def test_restart_with_anyone_home_reading_off_is_not_a_departure() -> None:
    """D29: the restart signature — the snapshot (and the first ~35 s) read
    anyone_home off — must not hard-off a house whose lamps the engine now
    knows are lit (D28)."""
    snap = InitialSnapshot(
        sun_elevation=NIGHT_SUN,
        anyone_home=False,
        occupancy={"sofakrok": True, "kjokken": True},
        channels={"sofakrok_taklys": (0.3, None), "kjokken_downlights": (0.25, 2400)},
    )
    eng = Engine(apartment(), snap)
    boot = at(1, 21, 0)
    every: list = []
    every += eng.handle(SunElevationChanged(NIGHT_SUN), boot)
    every += eng.handle(ReviewTick(), boot + timedelta(seconds=31))  # grace over
    every += eng.handle(HomeChanged(True), boot + timedelta(seconds=35))
    every += eng.handle(ReviewTick(), boot + timedelta(seconds=DEBOUNCE + 5))
    assert not eng.state.away
    turned_off = {c.channel_id for c in every if isinstance(c, TurnOffChannel)}
    assert not turned_off & {"sofakrok_taklys", "kjokken_downlights"}


def test_a_boot_into_an_empty_house_counts_the_window_from_boot() -> None:
    """D29: anyone_home False from the very start engages away at boot +
    away_debounce (the expiry is scheduled from the first recompute)."""
    eng = Engine(apartment(), InitialSnapshot(sun_elevation=NIGHT_SUN, anyone_home=False))
    boot = at(1, 21, 0)
    cmds = eng.handle(SunElevationChanged(NIGHT_SUN), boot)
    assert eng.state.away_pending_since == boot
    assert review(cmds) <= boot + timedelta(seconds=DEBOUNCE)
    eng.handle(ReviewTick(), boot + timedelta(seconds=DEBOUNCE))
    assert eng.state.away


def test_arrival_is_instant() -> None:
    """§6.4: coming home clears away at once — no debounce on the way in."""
    eng, t = _lit_evening()
    eng.handle(PresenceChanged("sofakrok", False), t)
    eng.handle(PresenceChanged("kjokken", False), t)
    eng.handle(HomeChanged(False), t)
    eng.handle(ReviewTick(), t + timedelta(seconds=DEBOUNCE))
    assert eng.state.away
    back = t + timedelta(seconds=DEBOUNCE + 60)
    eng.handle(PresenceChanged("kjokken", True), back)  # radar first: still away
    assert eng.state.away
    cmds = eng.handle(HomeChanged(True), back + timedelta(seconds=1))
    assert not eng.state.away
    assert "kjokken_downlights" in sets(cmds)


def test_unknown_home_cancels_the_window() -> None:
    """§6.4: None fails safe as home — it cancels a running window."""
    eng, t = _lit_evening()
    eng.handle(PresenceChanged("sofakrok", False), t)
    eng.handle(PresenceChanged("kjokken", False), t)
    eng.handle(HomeChanged(False), t)
    eng.handle(HomeChanged(None), t + timedelta(minutes=2))
    eng.handle(ReviewTick(), t + timedelta(seconds=DEBOUNCE))
    assert not eng.state.away and eng.state.away_pending_since is None


def test_vacation_is_not_debounced() -> None:
    """§6.6: vacation is away at once, onset release included."""
    eng, t = _lit_evening()
    cmds = eng.handle(VacationChanged(True), t)
    assert not eng.state.rooms["kjokken"].overridden
    assert "kjokken_taklys" in offs(cmds) and "sofakrok_taklys" in offs(cmds)


# --- review fix 4: confirmed fallback evidence also cancels -----------------


def _empty_house() -> tuple[Engine, object]:
    eng, t = _lit_evening()
    eng.handle(PresenceChanged("sofakrok", False, False), t)
    eng.handle(PresenceChanged("kjokken", False, False), t)
    eng.handle(HomeChanged(False), t)
    return eng, t


def test_a_confirmed_raw_spell_cancels_and_restarts_the_window() -> None:
    """D29/D31: presence-conductor zeroes every room while its anyone_home is
    off, so during the real flaps only the raw sensors saw the untracked
    occupant. A raw spell persisting fallback_min_on is someone home: the
    window is cancelled and restarts when the spell ends."""
    eng, t = _empty_house()
    on = t + timedelta(minutes=5)
    cmds = eng.handle(PresenceChanged("kjokken", False, True), on)
    assert eng.state.away_pending_since == t  # not yet confirmed
    confirm = on + timedelta(seconds=eng.tun.fallback_min_on)
    assert review(cmds) <= confirm  # the confirmation instant is self-scheduled
    eng.handle(ReviewTick(), confirm)
    assert eng.state.away_pending_since is None
    off = on + timedelta(minutes=3)
    eng.handle(PresenceChanged("kjokken", False, False), off)
    assert eng.state.away_pending_since == off
    eng.handle(ReviewTick(), t + timedelta(seconds=DEBOUNCE))  # the original expiry
    assert not eng.state.away
    eng.handle(ReviewTick(), off + timedelta(seconds=DEBOUNCE))
    assert eng.state.away


def test_an_unconfirmed_blip_does_not_cancel_the_window() -> None:
    """D29: the raw sensors also blip in an empty house; a blip shorter than
    fallback_min_on is no evidence and the departure engages on time."""
    eng, t = _empty_house()
    eng.handle(PresenceChanged("kjokken", False, True), t + timedelta(minutes=5))
    eng.handle(PresenceChanged("kjokken", False, False), t + timedelta(minutes=5, seconds=8))
    assert eng.state.away_pending_since == t
    eng.handle(ReviewTick(), t + timedelta(seconds=DEBOUNCE))
    assert eng.state.away
