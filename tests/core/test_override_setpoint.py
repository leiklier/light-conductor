"""§9.1/§9.2/§8.5 (D28): latch re-stamps and availability-recovery belief.

Plejd's ~3-min true-state poll flips a lit lamp's brightness between integer
and float (184 -> 184.355 -> 184). Any of those flips that reached the engine
as a ForeignChange re-stamped ``override_since``, so the 4 h timeout never
fired while a latched lamp was lit. And after an HA restart the engine — seeded
while the lights were unavailable — believed every channel off.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    ForeignChange,
    LuxReport,
    ObservedLevel,
    ReviewTick,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import (
    Band,
    ChannelConfig,
    EngineConfig,
    InitialSnapshot,
    Profile,
    RoomCalibration,
    RoomConfig,
    RoomShape,
    Vacancy,
)
from custom_components.light_conductor.core.plan import SetChannel, TurnOffChannel

from .helpers import apartment, at, offs, sets

DAY_SUN = 20.0
NIGHT_SUN = -8.0
POLL = timedelta(minutes=3)


def _engine() -> Engine:
    eng = Engine(apartment())
    eng.handle(SunElevationChanged(DAY_SUN), at(1, 12, 0))
    return eng


def _flip(level: float, k: int) -> float:
    """The poll's integer/float alternation around one 8-bit brightness."""
    return level if k % 2 else level + 0.355 / 255


def test_poll_flips_on_an_adopted_level_do_not_restamp_and_the_timeout_fires() -> None:
    """§9.1/§9.2 (D28): a blind room latched at a user level and then re-reported
    every 3 min at that level (integer/float) releases at override_since + 4 h.
    beta.19 re-stamped on every flip and the latch stood for as long as the lamp
    was lit."""
    eng = _engine()
    t0 = at(1, 12, 5)
    level = 184 / 255
    eng.handle(ForeignChange("soverom_taklys", level), t0)
    rs = eng.state.rooms["soverom"]
    assert rs.overridden and rs.override_since == t0

    t = t0
    for k in range(int(4 * 3600 / 180) - 1):
        t += POLL
        eng.handle(ForeignChange("soverom_taklys", _flip(level, k)), t)
        assert rs.override_since == t0, f"re-stamped by poll flip #{k}"
    cmds = eng.handle(ReviewTick(), t0 + timedelta(hours=4))
    assert not rs.overridden
    assert "soverom_taklys" in offs(cmds)  # back under control (blind room: OFF)


def test_wall_event_on_a_latched_room_still_restamps() -> None:
    """§9.4: a wall press is always user intent, even at the adopted level."""
    eng = _engine()
    t0 = at(1, 12, 5)
    eng.handle(ForeignChange("soverom_taklys", 0.5), t0)
    t1 = t0 + timedelta(minutes=30)
    eng.handle(ForeignChange("soverom_taklys", 0.5, wall_event=True), t1)
    assert eng.state.rooms["soverom"].override_since == t1


def test_a_real_dial_move_after_adoption_restamps_and_adopts() -> None:
    """§9.1: a level materially different from the adopted one is new intent."""
    eng = _engine()
    t0 = at(1, 12, 5)
    eng.handle(ForeignChange("soverom_taklys", 0.5), t0)
    t1 = t0 + timedelta(minutes=30)
    eng.handle(ForeignChange("soverom_taklys", 0.8), t1)
    rs = eng.state.rooms["soverom"]
    assert rs.override_since == t1
    assert rs.channels["soverom_taklys"].commanded_b == 0.8
    # Off after on is also a move: latched room turned off by hand re-stamps.
    t2 = t1 + timedelta(minutes=5)
    eng.handle(ForeignChange("soverom_taklys", 0.0), t2)
    assert rs.override_since == t2 and not rs.channels["soverom_taklys"].on
    # ...and a repeated off report does not.
    eng.handle(ForeignChange("soverom_taklys", 0.0), t2 + POLL)
    assert rs.override_since == t2


def test_a_ct_change_at_the_adopted_level_still_restamps() -> None:
    """D28 leaves CT-only semantics alone: a different CT is not a re-statement."""
    eng = Engine(apartment(), InitialSnapshot(occupancy={"kjokken": True}))  # not off-worthy
    eng.handle(SunElevationChanged(DAY_SUN), at(1, 12, 0))
    t0 = at(1, 12, 5)
    eng.handle(ForeignChange("kjokken_downlights", 0.4, ct=3000), t0)
    t1 = t0 + timedelta(minutes=10)
    eng.handle(ForeignChange("kjokken_downlights", 0.4, ct=2400), t1)
    assert eng.state.rooms["kjokken"].override_since == t1
    # The same CT again (the poll) does not.
    eng.handle(ForeignChange("kjokken_downlights", 0.401, ct=2410), t1 + POLL)
    assert eng.state.rooms["kjokken"].override_since == t1


def test_observed_level_corrects_belief_without_latching() -> None:
    """§8.5 (D28): a lamp that comes back lit while the engine believed it off
    is adopted as belief — no latch — and, once the startup grace is over, a
    room whose goal is 0 turns it off quietly with a slew_step_empty ramp."""
    eng = Engine(apartment())  # snapshot: every channel off (lights were unavailable)
    t0 = at(1, 12, 0)
    eng.handle(SunElevationChanged(DAY_SUN), t0)
    # kontor: vacancy off, nobody there -> its goal is 0.
    cmds = eng.handle(ObservedLevel("kontor_taklys", 0.6), t0 + timedelta(seconds=10))
    rs = eng.state.rooms["kontor"]
    cs = rs.channels["kontor_taklys"]
    assert cs.on and cs.commanded_b == 0.6
    assert not rs.overridden and rs.override_since is None
    assert "kontor_taklys" not in offs(cmds)  # inside the startup grace: no writes

    cmds = eng.handle(ReviewTick(), t0 + timedelta(seconds=31))
    off = next(c for c in cmds if isinstance(c, TurnOffChannel))
    assert off.channel_id == "kontor_taklys"
    assert abs(off.ramp_seconds - 0.36 / eng.tun.slew_step_empty) < 1e-9  # quiet reconcile
    assert not rs.overridden


def test_observed_level_in_an_active_room_reconciles_with_the_empty_slew() -> None:
    """§11.1/§8.5: an occupied room whose lamp came back at the wrong level
    converges on slew_step_empty, once, then returns to the occupied slew."""
    snap = InitialSnapshot(sun_elevation=DAY_SUN, occupancy={"kontor": True})
    eng = Engine(apartment(), snap)
    t0 = at(1, 12, 0)
    eng.handle(SunElevationChanged(DAY_SUN), t0)
    eng.handle(ObservedLevel("kontor_taklys", 0.2), t0 + timedelta(seconds=10))
    cmds = eng.handle(ReviewTick(), t0 + timedelta(seconds=31))
    cmd = sets(cmds)["kontor_taklys"]
    cs = eng.state.rooms["kontor"].channels["kontor_taklys"]
    assert abs(cmd.ramp_seconds - (cmd.level**2 - 0.04) / eng.tun.slew_step_empty) < 1e-9
    assert not cs.reconcile


def test_observed_off_after_a_lit_belief_relights_the_room() -> None:
    """§8.5 (D28): the reverse divergence — the lamp came back OFF (power cut)
    while the engine believed it lit — is corrected too: the room's goal is lit,
    so the engine turns it back on instead of trusting a stale belief forever."""
    snap = InitialSnapshot(
        sun_elevation=DAY_SUN, occupancy={"kontor": True}, channels={"kontor_taklys": (0.6, None)}
    )
    eng = Engine(apartment(), snap)
    t0 = at(1, 12, 0)
    eng.handle(SunElevationChanged(DAY_SUN), t0)
    eng.handle(ReviewTick(), t0 + timedelta(seconds=31))
    cmds = eng.handle(ObservedLevel("kontor_taklys", 0.0), t0 + timedelta(seconds=60))
    assert isinstance(
        next(c for c in cmds if getattr(c, "channel_id", None) == "kontor_taklys"), SetChannel
    )


# --- the estimator sees the corrected belief --------------------------------

SOFAKROK_CURVE = (
    (0.0, 0.0),
    (0.1, 0.12558575979686565),
    (0.25, 0.3402061965551038),
    (0.5, 0.7441424873953103),
    (0.75, 1.0),
    (1.0, 1.0),
)
GAIN = 8.89166628476232


def _sofakrok() -> tuple[EngineConfig, dict[str, RoomCalibration]]:
    room = RoomConfig(
        room_id="sofakrok",
        channels=(ChannelConfig("sofakrok_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(vacancy=Vacancy.DIM, lux_active_day=8.0, lux_active_evening=3.5),
        shape=RoomShape.PRESENCE,
        has_lux_sensor=True,
    )
    cal = RoomCalibration(
        "sofakrok", {"sofakrok_taklys": GAIN}, {"sofakrok_taklys": SOFAKROK_CURVE}
    )
    return EngineConfig(rooms=(room,)), {"sofakrok": cal}


def test_estimator_stops_counting_the_own_lamp_as_daylight() -> None:
    """§3.2/§8.5 (D28): while the engine believed the lamp off, the sensor's
    reading of it was all "natural" light (N̂ ≈ L). The observed level re-bases
    Â at once, so N̂ drops to the real natural share without waiting minutes
    for the filter."""
    cfg, cals = _sofakrok()
    eng = Engine(cfg, InitialSnapshot(sun_elevation=NIGHT_SUN), calibrations=cals)
    t = datetime(2026, 11, 1, 17, 14, 18)
    eng.handle(SunElevationChanged(NIGHT_SUN), t)
    level = 0.3
    own = GAIN * 0.3402061965551038 + (0.3 - 0.25) / 0.25 * GAIN * (
        0.7441424873953103 - 0.3402061965551038
    )
    natural = 0.2
    for _ in range(10):  # the sensor sees lamp + night
        t += timedelta(seconds=30)
        eng.handle(LuxReport("sofakrok", own + natural), t)
    est = eng.state.rooms["sofakrok"].est
    assert est.n_hat > own * 0.8  # believed off: own light counted as daylight
    eng.handle(ObservedLevel("sofakrok_taklys", level), t + timedelta(seconds=1))
    assert abs(est.n_hat - natural) < 0.3
    assert est.pending_valid is False
