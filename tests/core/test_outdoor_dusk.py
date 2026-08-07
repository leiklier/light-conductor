"""§6.5a: outdoor rooms measure dusk from a lux sensor.

The factor unit-tests pin the union semantics (a sensor may only ever light the
balcony EARLIER than the §6.5 circadian gate); the engine tests drive the real
Engine so the mode path, the presence mirroring (§1.10) and the closed-loop
exclusion are checked where they actually live.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.light_conductor.core import modes, targets
from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    ForeignChange,
    HomeChanged,
    LuxReport,
    OccupationalChanged,
    ReviewTick,
    SleepChanged,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import (
    Band,
    ChannelConfig,
    EngineConfig,
    EngineState,
    InitialSnapshot,
    Profile,
    Role,
    RoomConfig,
    RoomShape,
    RoomState,
    Vacancy,
)
from custom_components.light_conductor.core.tunables import Tunables

TUN = Tunables()
DAY = 20.0  # sun high ⇒ E = 0, so only the measured ramp can light the balcony
START = datetime(2026, 7, 1, 12, 0, 0)


# --- the factor (rule 6.5a) ---------------------------------------------


def test_factor_ramps_between_the_dusk_bounds() -> None:
    """0 at outdoor_on_lux, 1 at outdoor_full_lux, linear between."""
    f = targets.outdoor_dusk_factor
    assert f(TUN.outdoor_on_lux, 0.0, TUN) == 0.0
    assert f(TUN.outdoor_full_lux, 0.0, TUN) == 1.0
    mid = (TUN.outdoor_on_lux + TUN.outdoor_full_lux) / 2
    assert abs(f(mid, 0.0, TUN) - 0.5) < 1e-9
    assert f(500.0, 0.0, TUN) == 0.0  # midday: clamped, not negative
    assert f(0.0, 0.0, TUN) == 1.0  # full dark: clamped at 1


def test_no_sensor_is_the_pre_6_5a_circadian_gate() -> None:
    """A sensorless outdoor room keeps the all-or-nothing E gate."""
    f = targets.outdoor_dusk_factor
    assert f(None, TUN.outdoor_on_threshold - 0.01, TUN) == 0.0
    assert f(None, TUN.outdoor_on_threshold, TUN) == 1.0


def test_dead_of_night_backstops_a_falsely_bright_sensor() -> None:
    """At the circadian plateau the balcony is lit as §6.5 always lit it."""
    assert targets.outdoor_dusk_factor(500.0, 1.0, TUN) == 1.0


def test_measurement_governs_below_the_plateau() -> None:
    """Between the gates the sensor decides BOTH ways: a bright reading keeps the
    balcony dark even where the old E gate would have lit it (the summer morning
    that ran until 08:28 local at 155 lx)."""
    assert targets.outdoor_dusk_factor(155.0, 0.9, TUN) == 0.0
    assert targets.outdoor_dusk_factor(1.0, 0.1, TUN) == 1.0


# --- the mode (rule 6.5 x 6.5a) -----------------------------------------


def _balkong() -> RoomConfig:
    return RoomConfig(
        room_id="balkong",
        channels=(ChannelConfig("balkong_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(
            out_active_evening={Band.PRIMARY: 0.5},
            out_background={Band.PRIMARY: 0.2},
        ),
        shape=RoomShape.OUTDOOR,
        presence_capable=False,
        has_lux_sensor=True,
    )


def test_mode_scales_both_tiers_by_the_dusk_factor() -> None:
    """Half-dusk halves the backdrop AND the sitting-outside level."""
    room, state = _balkong(), EngineState()
    ambient = modes.resolve(room, RoomState(), state, 0.0, TUN, dusk=0.5)
    assert ambient.band_outputs == {Band.PRIMARY: 0.1}
    assert ambient.role is Role.BACKGROUND and ambient.gain_exempt
    sitting = modes.resolve(room, RoomState(occupational=True), state, 0.0, TUN, dusk=0.5)
    assert sitting.band_outputs == {Band.PRIMARY: 0.25}
    assert sitting.role is Role.ACTIVE and sitting.ct_override == TUN.ct_evening


def test_mode_off_at_zero_dusk_and_unscaled_at_full() -> None:
    room, state = _balkong(), EngineState()
    assert modes.resolve(room, RoomState(occupational=True), state, 0.0, TUN, dusk=0.0).off
    full = modes.resolve(room, RoomState(occupational=True), state, 0.0, TUN, dusk=1.0)
    assert full.band_outputs == {Band.PRIMARY: 0.5}


def test_away_background_is_scaled_too() -> None:
    """§6.4 presence simulation follows the same ramp (occupational ignored)."""
    state = EngineState(anyone_home=False, away_lighting=True)
    res = modes.resolve(_balkong(), RoomState(occupational=True), state, 0.0, TUN, dusk=0.5)
    assert res.band_outputs == {Band.PRIMARY: 0.1}


# --- end to end through the engine --------------------------------------


def _config(*, lux: bool = True) -> EngineConfig:
    """Balcony (outdoor, optional lux sensor) + one indoor neighbour."""
    balkong = RoomConfig(
        room_id="balkong",
        channels=(ChannelConfig("balkong_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(
            out_active_evening={Band.PRIMARY: 0.5},
            out_background={Band.PRIMARY: 0.2},
        ),
        shape=RoomShape.OUTDOOR,
        presence_capable=False,
        has_lux_sensor=lux,
        living_group=True,
    )
    stue = RoomConfig(
        room_id="stue",
        channels=(ChannelConfig("stue_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(vacancy=Vacancy.DIM, out_active_day={Band.PRIMARY: 1.0}),
        shape=RoomShape.PRESENCE,
        neighbours=("balkong",),
        living_group=True,
    )
    return EngineConfig(rooms=(balkong, stue))


def _booted(*, lux: bool = True, occupational: bool = False) -> tuple[Engine, datetime]:
    eng = Engine(
        _config(lux=lux),
        InitialSnapshot(sun_elevation=DAY, occupational={"balkong": occupational}),
    )
    t = START
    eng.handle(SunElevationChanged(DAY), t)
    t += timedelta(seconds=40)  # past the startup grace
    eng.handle(ReviewTick(), t)
    return eng, t


def _feed(eng: Engine, lux: float, t: datetime, n: int = 8, dt: float = 30.0) -> datetime:
    """Settle the estimator's low-pass at ``lux`` (rule 3.2)."""
    for _ in range(n):
        t += timedelta(seconds=dt)
        eng.handle(LuxReport("balkong", lux), t)
    return t


def _commanded(eng: Engine) -> float:
    return eng.state.rooms["balkong"].channels["balkong_taklys"].commanded_b


def test_balcony_lights_before_the_sun_ramp_when_the_window_goes_dark() -> None:
    """The reported case: E = 0 (bright by the sun ramp), but measured dusk."""
    eng, t = _booted(occupational=True)
    t = _feed(eng, 40.0, t)  # daylight at the window
    assert _commanded(eng) == 0.0
    t = _feed(eng, 1.0, t, n=40)  # dusk, well past the low-pass
    assert abs(_commanded(eng) - 0.5) < 0.05  # full out_active_evening


def test_output_tracks_the_ramp_between_the_bounds() -> None:
    """Half-way down the dusk window ⇒ about half the sitting-outside level."""
    eng, t = _booted(occupational=True)
    mid = (TUN.outdoor_on_lux + TUN.outdoor_full_lux) / 2
    t = _feed(eng, mid, t, n=40)
    assert abs(_commanded(eng) - 0.25) < 0.05


def test_stale_sensor_falls_back_to_the_circadian_gate() -> None:
    """§3.5: a sensor that stops reporting leaves §6.5 exactly as it was."""
    eng, t = _booted(occupational=True)
    t = _feed(eng, 1.0, t, n=40)
    assert _commanded(eng) > 0.0
    # Sun still high (E = 0) and the sensor ages out ⇒ the balcony goes dark.
    t += timedelta(seconds=TUN.lux_stale + 60)
    eng.handle(ReviewTick(), t)
    assert _commanded(eng) == 0.0


def test_sensorless_outdoor_room_is_unchanged() -> None:
    """No sensor ⇒ pre-6.5a behaviour: dark by day, lit past the E gate."""
    eng, t = _booted(lux=False, occupational=True)
    assert _commanded(eng) == 0.0
    eng.handle(SunElevationChanged(-10.0), t + timedelta(seconds=10))  # E = 1
    assert abs(_commanded(eng) - 0.5) < 0.02  # out_active_evening, on the §8.3 grid


def test_presence_mirroring_waits_for_a_deeper_ramp() -> None:
    """§1.10: the interior must not follow the balcony while it is still light."""
    eng, t = _booted(occupational=True)
    # Shallow ramp: the balcony is lit, but its occupant does not yet count as
    # presence for the neighbours.
    shallow = TUN.outdoor_on_lux - 0.2 * (TUN.outdoor_on_lux - TUN.outdoor_full_lux)
    t = _feed(eng, shallow, t, n=40)
    assert _commanded(eng) > 0.0
    assert eng.state.rooms["balkong"].self_active is False
    assert eng.state.rooms["stue"].role is not Role.ADJACENT
    # Deep ramp: presence mirrors and the neighbour glows.
    t = _feed(eng, TUN.outdoor_full_lux, t, n=40)
    assert eng.state.rooms["balkong"].self_active is True
    assert eng.state.rooms["stue"].role is Role.ADJACENT


def test_outdoor_room_never_enters_the_closed_loop() -> None:
    """§6.5a: the sensor is a dusk measurement, not a control feedback path."""
    eng, t = _booted(occupational=True)
    t = _feed(eng, 1.0, t, n=40)
    rs = eng.state.rooms["balkong"]
    assert rs.est.bootstrap_ratios == []  # no gain learning armed
    assert rs.est.bootstrap_confident is False
    diag = next(
        c for c in eng.handle(ReviewTick(), t + timedelta(seconds=5)) if hasattr(c, "rooms")
    )
    balkong = next(d for d in diag.rooms if d.room_id == "balkong")
    assert balkong.target_lux is None  # no closed-loop target...
    assert balkong.natural_lux is not None  # ...but the dusk reading is published


def test_occupational_toggle_reaches_the_scaled_tier() -> None:
    """Sitting outside raises the tier without leaving the ramp."""
    eng, t = _booted(occupational=False)
    t = _feed(eng, TUN.outdoor_full_lux, t, n=40)
    assert abs(_commanded(eng) - 0.2) < 0.05  # out_background at full dusk
    t += timedelta(seconds=10)
    eng.handle(OccupationalChanged("balkong", True), t)
    assert abs(_commanded(eng) - 0.5) < 0.05


def test_morning_release_follows_the_light_not_the_sun_ramp() -> None:
    """The balcony goes dark when the window brightens, without waiting for E to
    fall under outdoor_on_threshold (E = 0.9 here, well inside the old gate)."""
    eng = Engine(
        _config(),
        InitialSnapshot(sun_elevation=DAY, occupational={"balkong": True}),
    )
    t = START
    # E = 0.9 by the sun ramp: the old gate would hold the balcony fully lit.
    elevation = TUN.sun_low_deg + 0.1 * (TUN.sun_high_deg - TUN.sun_low_deg)
    eng.handle(SunElevationChanged(elevation), t)
    t += timedelta(seconds=40)
    t = _feed(eng, 1.0, t, n=40)
    assert _commanded(eng) > 0.0  # lit through the night
    t = _feed(eng, 155.0, t, n=60)  # morning light at the window
    assert _commanded(eng) == 0.0


# --- §6.5b: manual light action declares presence -------------------------


def _dusk_engine(*, occupational: bool = False) -> tuple[Engine, datetime]:
    """Balcony + neighbour, fully dark by measurement (dusk factor 1)."""
    eng, t = _booted(occupational=occupational)
    t = _feed(eng, TUN.outdoor_full_lux, t, n=40)
    return eng, t


def test_button_on_at_dusk_declares_sitting_outside() -> None:
    """ON edge: the room goes occupational and the conductor's sitting tier
    takes over (the latch releases at D_out > 0)."""
    eng, t = _dusk_engine()
    assert abs(_commanded(eng) - 0.2) < 0.05  # backdrop, not sitting
    # Backdrop must first be suppressed (hardware toggle: press 1 = off) —
    # occupational is off, so this is NOT a declaration and the latch holds.
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.0), t)
    rs = eng.state.rooms["balkong"]
    assert rs.occupational is False and rs.overridden is True
    assert _commanded(eng) == 0.0  # stays dark: backdrop suppressed, not relit
    # Press 2 = on: the declaration.
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.9), t)
    rs = eng.state.rooms["balkong"]
    assert rs.occupational is True
    assert rs.overridden is False  # released: the tier governs, not the 0.9
    assert abs(_commanded(eng) - 0.5) < 0.05  # out_active_evening
    assert rs.self_active is True  # §1.10 presence mirrors at full dusk


def test_button_off_while_sitting_returns_the_backdrop() -> None:
    """OFF edge while occupational: declared absence — mode resolution returns
    the dusk backdrop instead of leaving the room latched dark."""
    eng, t = _dusk_engine(occupational=True)
    assert abs(_commanded(eng) - 0.5) < 0.05
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.0), t)
    rs = eng.state.rooms["balkong"]
    assert rs.occupational is False
    assert rs.overridden is False
    assert abs(_commanded(eng) - 0.2) < 0.05  # backdrop, within the same cycle


def test_daylight_button_press_is_never_countered() -> None:
    """ON edge at D_out = 0: occupational flips on, but the latch KEEPS the
    user's level — releasing would resolve OFF and counter the press."""
    eng, t = _booted()  # sun high, no lux fed => stale => E gate => dusk 0
    t += timedelta(seconds=TUN.lux_stale + 60)
    eng.handle(ReviewTick(), t)
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.7), t)
    rs = eng.state.rooms["balkong"]
    assert rs.occupational is True  # the declaration is still made
    assert rs.overridden is True  # ...but the latch protects the press
    assert rs.channels["balkong_taklys"].commanded_b == 0.7  # adopted, not off


def test_dial_while_sitting_keeps_occupational_and_latches() -> None:
    """No edge (lit -> lit): brightness adjustment, not a presence change."""
    eng, t = _dusk_engine(occupational=True)
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.9), t)
    rs = eng.state.rooms["balkong"]
    assert rs.occupational is True
    assert rs.overridden is True
    assert rs.channels["balkong_taklys"].commanded_b == 0.9


def test_switch_off_releases_a_dialed_override() -> None:
    """The same arbitration from the switch entity: HomeKit-off after a dial
    returns the backdrop instead of burning the dialed level for 4 h."""
    eng, t = _dusk_engine(occupational=True)
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.9), t)  # dial: latch at 0.9
    assert eng.state.rooms["balkong"].overridden is True
    t += timedelta(seconds=10)
    eng.handle(OccupationalChanged("balkong", False), t)
    rs = eng.state.rooms["balkong"]
    assert rs.overridden is False
    assert abs(_commanded(eng) - 0.2) < 0.05  # backdrop


def test_unchanged_resubmit_never_releases() -> None:
    """The switch's restore re-submit at boot must not clear latches."""
    eng, t = _dusk_engine(occupational=True)
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.9), t)  # dial: latch
    t += timedelta(seconds=10)
    eng.handle(OccupationalChanged("balkong", True), t)  # same state re-submitted
    assert eng.state.rooms["balkong"].overridden is True
    assert eng.state.rooms["balkong"].channels["balkong_taklys"].commanded_b == 0.9


def test_daylight_latch_survives_reviews_but_sleep_still_wins() -> None:
    """The respected daylight latch is not countered by later recomputes; a
    sleep hard-off still releases and darkens (§6.1 wins over 6.5b)."""
    eng, t = _booted()
    t += timedelta(seconds=TUN.lux_stale + 60)
    eng.handle(ReviewTick(), t)
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.7), t)
    t += timedelta(seconds=300)
    eng.handle(ReviewTick(), t)
    rs = eng.state.rooms["balkong"]
    assert rs.overridden is True
    assert rs.channels["balkong_taklys"].commanded_b == 0.7
    t += timedelta(seconds=10)
    eng.handle(SleepChanged(True), t)
    rs = eng.state.rooms["balkong"]
    assert rs.overridden is False
    assert _commanded(eng) == 0.0


def test_morning_descent_still_cleans_up_an_undeclared_override() -> None:
    """With occupational OFF the dusk-OFF stays a hard-off: a stray dial from
    the evening is released and darkened when daylight returns (pre-6.5b)."""
    eng, t = _dusk_engine()  # backdrop lit, occupational off
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.9), t)  # dial the backdrop up
    assert eng.state.rooms["balkong"].overridden is True
    assert eng.state.rooms["balkong"].occupational is False  # no edge: was lit
    t = _feed(eng, 155.0, t, n=60)  # morning light returns
    rs = eng.state.rooms["balkong"]
    assert rs.overridden is False
    assert _commanded(eng) == 0.0


# --- review round (F1, F2, F4, F5, F8) ------------------------------------


def test_shallow_dusk_press_keeps_the_users_level() -> None:
    """F1: below outdoor_presence_factor the sitting tier quantizes toward the
    dim floor — releasing would rewrite a 90 % press to ~2 %. The latch must
    keep the adopted level across the whole shallow half of the ramp."""
    for lux, dusk in ((14.35, 0.05), (12.4, 0.2)):
        eng, t = _booted()
        t = _feed(eng, lux, t, n=40)
        # Ensure the room is dark so the press is an ON edge (suppress any
        # shallow backdrop the ramp may have lit).
        if _commanded(eng) > 0.0:
            t += timedelta(seconds=10)
            eng.handle(ForeignChange("balkong_taklys", 0.0), t)
        t += timedelta(seconds=10)
        eng.handle(ForeignChange("balkong_taklys", 0.9), t)
        rs = eng.state.rooms["balkong"]
        assert rs.occupational is True, dusk  # the declaration is still made
        assert rs.overridden is True, dusk  # ...but the latch protects the press
        assert rs.channels["balkong_taklys"].commanded_b == 0.9, dusk
        # And later recomputes never step it down either.
        t += timedelta(seconds=300)
        eng.handle(ReviewTick(), t)
        assert eng.state.rooms["balkong"].channels["balkong_taklys"].commanded_b == 0.9, dusk


def test_press_during_sleep_or_away_mints_no_declaration() -> None:
    """F2: the hard-off wins a moment later and would strand occupational ON
    (every press under a hard-off is an ON edge) — lighting the interior
    around a phantom occupant at the next dusk. No declaration is minted."""
    for mode_event in (SleepChanged(True), HomeChanged(False)):
        eng, t = _dusk_engine()
        t += timedelta(seconds=10)
        eng.handle(mode_event, t)
        if isinstance(mode_event, SleepChanged):
            assert _commanded(eng) == 0.0  # sleep hard-off
        else:
            assert abs(_commanded(eng) - 0.2) < 0.05  # away keeps the 6.4 backdrop
        for _ in range(3):  # presses under the mode: edges of either direction
            t += timedelta(seconds=10)
            eng.handle(ForeignChange("balkong_taklys", 0.9), t)
            t += timedelta(seconds=10)
            eng.handle(ForeignChange("balkong_taklys", 0.0), t)
        assert eng.state.rooms["balkong"].occupational is False, mode_event


def test_respected_latch_survives_off_worthy_on_a_presence_capable_room() -> None:
    """F4: adding an occupancy fallback to the balcony (presence_capable=True)
    must not re-arm the daylight release-and-counter via should_release."""
    cfg = _config()
    balkong = cfg.rooms[0]
    assert balkong.room_id == "balkong"
    from dataclasses import replace

    cfg = EngineConfig(rooms=(replace(balkong, presence_capable=True), cfg.rooms[1]))
    eng = Engine(cfg, InitialSnapshot(sun_elevation=DAY))
    t = START
    eng.handle(SunElevationChanged(DAY), t)
    t += timedelta(seconds=40)
    t += timedelta(seconds=TUN.lux_stale + 60)
    eng.handle(ReviewTick(), t)
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.7), t)
    t += timedelta(seconds=300)
    eng.handle(ReviewTick(), t)
    rs = eng.state.rooms["balkong"]
    assert rs.overridden is True
    assert rs.channels["balkong_taklys"].commanded_b == 0.7


def test_respected_latch_still_times_out() -> None:
    """F4: the respected latch is not immortal — override_timeout releases it
    and the daylight-OFF then darkens the room."""
    eng, t = _booted()
    t += timedelta(seconds=TUN.lux_stale + 60)
    eng.handle(ReviewTick(), t)
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", 0.7), t)
    t += timedelta(seconds=TUN.override_timeout + 60)
    eng.handle(ReviewTick(), t)
    rs = eng.state.rooms["balkong"]
    assert rs.overridden is False
    assert _commanded(eng) == 0.0


def test_occupational_edge_is_inert_inside_startup_grace() -> None:
    """F5: a restore re-submit racing a foreign change at boot must not clear
    the latch — the edge arbitration is gated on the startup grace."""
    eng = Engine(_config(), InitialSnapshot(sun_elevation=-10.0))  # E = 1: dusk
    t = START
    eng.handle(SunElevationChanged(-10.0), t)
    t += timedelta(seconds=5)  # inside startup_grace (30 s)
    eng.handle(ForeignChange("balkong_taklys", 0.9), t)
    assert eng.state.rooms["balkong"].overridden is True
    t += timedelta(seconds=5)
    eng.handle(OccupationalChanged("balkong", True), t)  # restored ON re-submit
    rs = eng.state.rooms["balkong"]
    assert rs.occupational is True
    assert rs.overridden is True  # NOT released inside the grace


def test_none_level_is_no_declaration() -> None:
    """F8: a wall event during a Plejd blip reports level None — that is an
    availability artefact, not an off-press; occupational must not clear."""
    eng, t = _dusk_engine(occupational=True)
    t += timedelta(seconds=10)
    eng.handle(ForeignChange("balkong_taklys", None), t)
    assert eng.state.rooms["balkong"].occupational is True
