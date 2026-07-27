"""Calibration sweep proofs against the synthetic plant (ENGINE_SPEC §4.4).

The sweep runs the production :class:`Engine` state machine against a plant
with known true gains and curves, and proves: gain recovery within tolerance,
a transactional commit, every abort path restoring prior state exactly, the
start gate, and the persistence contract (§4.4/rule 5).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise

from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    ForeignChange,
    LuxReport,
    PresenceChanged,
    ReviewTick,
    SleepChanged,
    StartCalibration,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import (
    Band,
    InitialSnapshot,
    RoomCalibration,
)
from custom_components.light_conductor.core.plan import CalibrationResult
from custom_components.light_conductor.core.tunables import Tunables

from .plant import Channel, Plant, closed_config

NIGHT = -10.0
BASE = datetime(2026, 7, 1, 23, 0, 0)


def _cal_engine(
    chans: list[Channel], *, tun: Tunables | None = None, calibrations=None, ambient: float = 0.0
) -> Engine:
    """A night-time engine with the room occupied and a settled dark sensor.

    ``ambient`` is the resting dark lux fed pre-sweep (the off-baseline the
    sweep must subtract, F3a)."""
    eng = Engine(
        closed_config(chans, lux_active_day=100.0),
        InitialSnapshot(sun_elevation=NIGHT, occupancy={"lab": True}),
        tunables=tun,
        calibrations=calibrations,
    )
    eng.handle(SunElevationChanged(NIGHT), BASE)
    eng.handle(PresenceChanged("lab", True), BASE + timedelta(seconds=40))
    # A few settled samples so the estimator is fresh + stable (can_start gate).
    t = BASE + timedelta(seconds=50)
    for _ in range(5):
        eng.handle(LuxReport("lab", ambient), t)
        t = t + timedelta(seconds=2)
    return eng


def _drive_sweep(
    eng: Engine, plant: Plant, start: datetime, ticks: int = 200
) -> list[CalibrationResult]:
    """Feed the plant's true lux each second until the sweep finishes."""
    results: list[CalibrationResult] = []
    t = start
    for _ in range(ticks):
        cmds = plant.tick(t)
        results.extend(c for c in cmds if isinstance(c, CalibrationResult))
        if eng.state.rooms["lab"].cal is None and results:
            break
        t = t + timedelta(seconds=1)
    return results


# --- (g) gain recovery + transactional commit ---------------------------


def test_sweep_commit_resets_bootstrap_gain(  # N1 regression
) -> None:
    """N1: a committed sweep supersedes the bootstrap scalar. gain_mult is an
    ABSOLUTE gain over default curves while bootstrapping (~true gain x margin);
    left in place over freshly measured gains it acts as a huge multiplier and
    parks the room stably dim."""
    chans = [Channel("pri", gain=180.0, band=Band.PRIMARY)]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)

    est = eng.state.rooms["lab"].est
    est.gain_mult = 270.0  # as left behind by a completed first-night bootstrap
    est.bootstrap_confident = True
    est.bootstrap_ratios.extend([179.0, 180.0, 181.0])

    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    results = _drive_sweep(eng, plant, start + timedelta(seconds=1))
    assert len(results) == 1 and results[0].ok

    assert est.gain_mult == 1.0
    assert not est.bootstrap_confident
    assert not est.bootstrap_ratios
    assert eng._photo["lab"].calibrated


def test_sweep_recovers_gains_within_tolerance() -> None:
    """§4.4: the sweep recovers each channel's true gain and commits."""
    chans = [
        Channel("acc", gain=120.0, band=Band.ACCENT),
        Channel("pri", gain=60.0, band=Band.PRIMARY),
    ]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)

    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    results = _drive_sweep(eng, plant, start + timedelta(seconds=1))

    assert len(results) == 1 and results[0].ok
    assert results[0].reason == "ok"
    cal = eng.calibration_of("lab")
    assert abs(cal.gains["acc"] - 120.0) / 120.0 < 0.05
    assert abs(cal.gains["pri"] - 60.0) / 60.0 < 0.05
    # Full coverage on both channels.
    assert dict(results[0].coverage) == {"acc": 1.0, "pri": 1.0}
    # The room is now marked calibrated (photometry committed).
    assert eng._photo["lab"].calibrated


def test_sweep_recovers_a_nonlinear_curve() -> None:
    """§4.4/§4.2: a non-square-law channel's curve is measured, not assumed."""
    chans = [Channel("c", gain=100.0, band=Band.PRIMARY, curve=lambda b: b**3)]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    _drive_sweep(eng, plant, start + timedelta(seconds=1))
    photo = eng._photo["lab"]
    # At b=0.5 a cubic curve gives 0.125 relative flux (square-law would be 0.25).
    assert abs(photo.flux("c", 0.5) - 0.125) < 0.03


# --- start gate (rule 4.4) ----------------------------------------------


def test_rejected_when_sun_too_high() -> None:
    """§4.4: a sweep is rejected unless sun elevation < night_prior_deg."""
    chans = [Channel("c", gain=100.0)]
    eng = Engine(
        closed_config(chans, lux_active_day=100.0),
        InitialSnapshot(sun_elevation=15.0, occupancy={"lab": True}),
    )
    eng.handle(SunElevationChanged(15.0), BASE)
    eng.handle(LuxReport("lab", 50.0), BASE + timedelta(seconds=2))
    out = eng.handle(StartCalibration("lab"), BASE + timedelta(seconds=4))
    results = [c for c in out if isinstance(c, CalibrationResult)]
    assert results and not results[0].ok and results[0].reason == "sun_too_high"
    assert eng.state.rooms["lab"].cal is None


def test_rejected_when_sensor_stale() -> None:
    """§4.4: a sweep needs a fresh, stable sensor."""
    chans = [Channel("c", gain=100.0)]
    eng = Engine(
        closed_config(chans, lux_active_day=100.0),
        InitialSnapshot(sun_elevation=NIGHT, occupancy={"lab": True}),
    )
    eng.handle(SunElevationChanged(NIGHT), BASE)  # never fed a lux report
    out = eng.handle(StartCalibration("lab"), BASE + timedelta(seconds=4))
    results = [c for c in out if isinstance(c, CalibrationResult)]
    assert results and results[0].reason == "sensor_stale"


# --- transactional aborts restore prior state (rule 4.4) -----------------


def test_abort_on_foreign_change_restores_prior_light() -> None:
    """§4.4: a foreign change aborts and restores the exact pre-sweep lights."""
    chans = [Channel("acc", gain=120.0, band=Band.ACCENT), Channel("pri", gain=60.0)]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    # Give the room a distinctive pre-sweep light state to restore to.
    rs = eng.state.rooms["lab"]
    rs.channels["pri"].commanded_b = 0.42
    rs.channels["pri"].on = True

    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    # Advance a couple of dwells, then a foreign change lands mid-sweep.
    t = start + timedelta(seconds=1)
    for _ in range(6):
        plant.tick(t)
        t = t + timedelta(seconds=1)
    assert rs.cal is not None  # still sweeping
    out = eng.handle(ForeignChange("pri", 0.9), t)
    results = [c for c in out if isinstance(c, CalibrationResult)]
    assert results and not results[0].ok and results[0].reason == "foreign_change"
    assert rs.cal is None
    # Prior calibration restored: still uncalibrated (never committed).
    assert not eng._photo["lab"].calibrated
    # Prior light state restored exactly (pri back to 0.42).
    assert abs(rs.channels["pri"].commanded_b - 0.42) < 1e-9


def test_abort_on_sleep_restores_and_stays_uncalibrated() -> None:
    """§4.4: a sleep hard-off aborts the sweep transactionally."""
    chans = [Channel("c", gain=100.0)]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    t = start + timedelta(seconds=1)
    for _ in range(4):
        plant.tick(t)
        t = t + timedelta(seconds=1)
    out = eng.handle(SleepChanged(True), t)
    results = [c for c in out if isinstance(c, CalibrationResult)]
    assert results and results[0].reason == "mode_off"
    assert eng.state.rooms["lab"].cal is None
    assert not eng._photo["lab"].calibrated


def test_abort_on_missing_samples() -> None:
    """§4.4: a dwell that elapses with no lux sample aborts (missing_samples)."""
    chans = [Channel("c", gain=100.0)]
    eng = _cal_engine(chans)
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    # Drive past the settle dwell with review ticks only (no lux) — the sensor
    # is still fresh (< lux_stale) but the dwell collected nothing.
    out = eng.handle(ReviewTick(), start + timedelta(seconds=5))
    results = [c for c in out if isinstance(c, CalibrationResult)]
    assert results and not results[0].ok and results[0].reason == "missing_samples"
    assert eng.state.rooms["lab"].cal is None


def test_abort_restores_a_previous_calibration() -> None:
    """§4.4: aborting a re-calibration rolls back to the earlier calibration."""
    chans = [Channel("c", gain=100.0)]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    # First sweep succeeds.
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    _drive_sweep(eng, plant, start + timedelta(seconds=1))
    first = eng.calibration_of("lab")
    assert eng._photo["lab"].calibrated

    # Second sweep starts then aborts on a foreign change.
    t2 = start + timedelta(seconds=200)
    for _ in range(3):  # refresh the sensor for the start gate
        eng.handle(LuxReport("lab", 0.0), t2)
        t2 = t2 + timedelta(seconds=2)
    eng.handle(StartCalibration("lab"), t2)
    t = t2 + timedelta(seconds=1)
    for _ in range(4):
        plant.tick(t)
        t = t + timedelta(seconds=1)
    eng.handle(ForeignChange("c", 0.9), t)
    # Rolled back to the first calibration exactly, still calibrated.
    assert eng._photo["lab"].calibrated
    back = eng.calibration_of("lab")
    assert back.gains["c"] == first.gains["c"]


def test_other_rooms_unaffected_during_sweep() -> None:
    """§4.4: a sweep suspends only its own room; others keep running."""
    chans = [Channel("c", gain=100.0)]
    cfg = closed_config(chans, lux_active_day=100.0)
    # Two-room config: reuse closed_config room plus an open-loop room.
    from custom_components.light_conductor.core.model import (
        ChannelConfig,
        EngineConfig,
        Profile,
        RoomConfig,
    )

    other = RoomConfig(
        room_id="other",
        channels=(ChannelConfig("o", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(out_active_day={Band.PRIMARY: 0.5}, out_active_evening={Band.PRIMARY: 0.3}),
    )
    two = EngineConfig(rooms=(*cfg.rooms, other))
    eng = Engine(two, InitialSnapshot(sun_elevation=NIGHT, occupancy={"lab": True, "other": True}))
    eng.handle(SunElevationChanged(NIGHT), BASE)
    t = BASE + timedelta(seconds=50)
    for _ in range(5):
        eng.handle(LuxReport("lab", 0.0), t)
        t = t + timedelta(seconds=2)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    eng.handle(StartCalibration("lab"), t)
    # Mid-sweep, the other room is still controlled (its light commanded).
    tt = t + timedelta(seconds=1)
    plant.tick(tt)
    assert eng.state.rooms["lab"].cal is not None
    assert eng.state.rooms["other"].channels["o"].on  # unaffected


# --- start-gate branches + edge cases (rule 4.4) -------------------------


def test_can_start_branches() -> None:
    """§4.4: every rejection reason of the start gate."""
    from custom_components.light_conductor.core import calibration
    from custom_components.light_conductor.core.model import (
        ChannelConfig,
        EngineState,
        EstimatorState,
        Profile,
        RoomConfig,
        RoomState,
    )

    tun = Tunables()
    now = BASE
    room = RoomConfig(
        room_id="r",
        channels=(ChannelConfig("c", fixed_ct=2700),),
        profile=Profile(),
        has_lux_sensor=True,
    )
    no_sensor = RoomConfig(room_id="r", channels=room.channels, profile=Profile())
    rs = RoomState()
    st = EngineState(sun_elevation=NIGHT)

    assert calibration.can_start(rs, no_sensor, st, now, tun) == "no_lux_sensor"
    from custom_components.light_conductor.core.model import CalibrationSession

    rs.cal = CalibrationSession(channel_order=("c",))
    assert calibration.can_start(rs, room, st, now, tun) == "already_calibrating"
    rs.cal = None
    assert calibration.can_start(rs, room, EngineState(sleep=True), now, tun) == "mode_off"
    high = EngineState(sun_elevation=10.0)
    assert calibration.can_start(rs, room, high, now, tun) == "sun_too_high"
    assert calibration.can_start(rs, room, st, now, tun) == "sensor_stale"
    rs.est = EstimatorState(last_report_at=now, l_filt=None)
    assert calibration.can_start(rs, room, st, now, tun) == "lux_unstable"
    rs.est = EstimatorState(last_report_at=now, l_filt=0.0)
    assert calibration.can_start(rs, room, st, now, tun) == ""  # all clear


def test_ingest_lux_no_session_is_noop() -> None:
    """§4.4: a lux sample with no running sweep is harmless."""
    from custom_components.light_conductor.core import calibration
    from custom_components.light_conductor.core.model import RoomState

    rs = RoomState()
    calibration.ingest_lux(rs, 10.0, BASE, Tunables())  # no rs.cal -> no-op


def test_abort_on_sensor_going_stale_mid_sweep() -> None:
    """§4.4: the sensor ageing out mid-sweep aborts (sensor_stale)."""
    chans = [Channel("c", gain=100.0)]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    # Run a few dwells feeding lux, then stop and jump past lux_stale (300 s).
    t = start + timedelta(seconds=1)
    for _ in range(8):
        plant.tick(t)
        t = t + timedelta(seconds=1)
    assert eng.state.rooms["lab"].cal is not None
    out = eng.handle(ReviewTick(), t + timedelta(seconds=330))  # past lux_stale (300 s)
    results = [c for c in out if isinstance(c, CalibrationResult)]
    assert results and results[0].reason == "sensor_stale"


def test_dead_channel_gets_zero_gain_default_curve() -> None:
    """§4.4: a channel that produces no measurable light gets gain 0, no crash."""
    chans = [Channel("dead", gain=0.0, band=Band.PRIMARY)]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    results = _drive_sweep(eng, plant, start + timedelta(seconds=1))
    assert results and results[0].ok
    assert eng.calibration_of("lab").gains["dead"] == 0.0


# --- F3: guards that were mutation-invisible ----------------------------


def test_sweep_subtracts_nonzero_dark_baseline() -> None:
    """§4.4 (F3a): fitted gains are corrected for a nonzero ambient baseline.

    With 2 lx of ambient dark light, a true-gain-100 channel must fit ~100, not
    ~102 — deleting the off-baseline subtraction (calibration.py) fails this."""
    chans = [Channel("c", gain=100.0, band=Band.PRIMARY)]
    eng = _cal_engine(chans, ambient=2.0)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 2.0)  # 2 lx ambient
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    results = _drive_sweep(eng, plant, start + timedelta(seconds=1))
    assert results and results[0].ok
    fitted = eng.calibration_of("lab").gains["c"]
    assert abs(fitted - 100.0) < 1.0  # baseline-corrected (a raw fit would be ~102)


def test_sweep_enforces_monotone_curve_over_noisy_points() -> None:
    """§4.4 (F3b): a raw sweep that DIPS mid-range commits a monotone curve that
    differs from the raw ratios — deleting the cummax enforcement fails this."""
    # A true curve that dips at b=0.5 (a noisy under-read), both 0.25 and 0.5
    # below full: raw ratios 0.34 -> 0.20 are non-monotone AND both < 1, so the
    # min(1.0) clamp cannot hide the decrease — only the cummax can.
    tab = {0.1: 0.10, 0.25: 0.34, 0.5: 0.20, 0.75: 0.62, 1.0: 1.0}
    dip = lambda b: tab.get(round(b, 2), b * b)  # noqa: E731
    chans = [Channel("c", gain=100.0, band=Band.PRIMARY, curve=dip)]
    eng = _cal_engine(chans)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    _drive_sweep(eng, plant, start + timedelta(seconds=1))
    photo = eng._photo["lab"]
    fluxes = [photo.flux("c", b) for b in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0)]
    assert all(b - a >= -1e-9 for a, b in pairwise(fluxes))  # monotone
    # The committed curve lifted the dipped b=0.5 point above its raw 0.20 ratio.
    assert photo.flux("c", 0.5) > 0.30  # cummax carried the 0.34 forward
    assert abs(photo.flux("c", 0.5) - 0.20) > 0.1  # differs from the raw point


# --- F6: an abort tick must not double-command --------------------------


def test_abort_plan_has_no_duplicate_channel_commands() -> None:
    """§4.4 (F6): the abort recompute emits the restore only — the room's normal
    reconcile is suspended that tick, so no channel is commanded twice.

    The prior light (a manual 0.3, ON) is restored on abort, but a sleep abort
    also drives the room OFF; without the suspend, the restore SetChannel and
    the reconcile TurnOff would both target the same channel in one plan (the
    bug). A sleep (not foreign) abort is used so no override masks the reconcile."""
    from custom_components.light_conductor.core.plan import SetChannel, TurnOffChannel

    chans = [Channel("a", gain=120.0, band=Band.ACCENT), Channel("b", gain=60.0)]
    cfg = closed_config(chans, out_active_day={Band.ACCENT: 0.5, Band.PRIMARY: 0.5})
    eng = Engine(cfg, InitialSnapshot(sun_elevation=NIGHT, occupancy={"lab": True}))
    eng.handle(SunElevationChanged(NIGHT), BASE)
    eng.handle(PresenceChanged("lab", True), BASE + timedelta(seconds=40))
    t = BASE + timedelta(seconds=50)
    for _ in range(5):
        eng.handle(LuxReport("lab", 0.0), t)
        t = t + timedelta(seconds=2)
    # A distinctive pre-sweep manual light state (ON) to be restored on abort.
    for cid in ("a", "b"):
        eng.state.rooms["lab"].channels[cid].commanded_b = 0.3
        eng.state.rooms["lab"].channels[cid].on = True

    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0)
    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    t = start + timedelta(seconds=1)
    for _ in range(6):
        plant.tick(t)
        t = t + timedelta(seconds=1)
    out = eng.handle(SleepChanged(True), t)  # mode-off abort (no override latch)
    channel_cmds = [c.channel_id for c in out if isinstance(c, SetChannel | TurnOffChannel)]
    assert len(channel_cmds) == len(set(channel_cmds))  # no channel commanded twice


# --- partial coverage + delta-filtered slow sensor (rule 4.4) ------------


def _field_tun(**over) -> Tunables:
    """Tunables for a slow, delta-filtered sweep: long dwell + tiny write-blank
    so the off-baseline (a None-first publish) lands post-blank."""
    from dataclasses import replace

    return replace(Tunables(), calibration_dwell=90.0, write_blank=0.5, **over)


def _drive_field_sweep(
    eng: Engine, plant: Plant, start: datetime, ticks: int = 600
) -> list[CalibrationResult]:
    """Drive a delta-filtered sweep: lux only on a delta-publish, ReviewTicks
    otherwise so the engine still advances the dwell deadlines (as the real
    controller's scheduled reviews would)."""
    results: list[CalibrationResult] = []
    t = start
    for _ in range(ticks):
        cmds = plant.tick_field(t)
        if not cmds:
            cmds = eng.handle(ReviewTick(), t)
        results.extend(c for c in cmds if isinstance(c, CalibrationResult))
        if eng.state.rooms["lab"].cal is None and results:
            break
        t = t + timedelta(seconds=1)
    return results


def test_fit_channel_partial_coverage_extrapolates_square_law() -> None:
    """§4.4: a channel with only 50/75/100 sampled commits a monotone curve
    whose low end follows a square-law arc scaled to the lowest sampled point."""
    from custom_components.light_conductor.core import calibration
    from custom_components.light_conductor.core.tunables import Tunables

    levels = Tunables().calibration_levels
    # A true square-law channel, gain 100, only the top three levels captured.
    meas = {0.5: 25.0, 0.75: 56.25, 1.0: 100.0}
    gain, curve = calibration._fit_channel(0.0, meas, levels)
    assert abs(gain - 100.0) < 1.0  # recovered from the top sampled level
    pts = dict(curve)
    # Below the lowest sample (0.5) the arc is f_low * (b/0.5)**2 = b**2 here.
    assert abs(pts[0.25] - 0.0625) < 1e-6  # 0.25**2 (scaled b²)
    assert abs(pts[0.10] - 0.01) < 1e-6  # 0.10**2
    # Monotone, spans (0,0)..(1,1).
    fluxes = [f for _b, f in curve]
    assert all(b - a >= -1e-9 for a, b in pairwise(fluxes))
    assert curve[0] == (0.0, 0.0) and abs(curve[-1][1] - 1.0) < 1e-9


def test_fit_channel_extrapolates_above_top_sample() -> None:
    """§4.4: if the brightest level (1.0) was never captured, the gain is
    square-law extrapolated to b=1 and the curve extended to (1, 1)."""
    from custom_components.light_conductor.core import calibration
    from custom_components.light_conductor.core.tunables import Tunables

    levels = Tunables().calibration_levels
    # A square-law channel, gain 100, only 0.5 and 0.75 captured (top < 1.0).
    meas = {0.5: 25.0, 0.75: 56.25}
    gain, curve = calibration._fit_channel(0.0, meas, levels)
    assert abs(gain - 100.0) < 1.0  # g = contrib_top / top**2 = 56.25 / 0.5625
    pts = dict(curve)
    assert abs(pts[1.0] - 1.0) < 1e-9  # extended to (1, 1)
    assert abs(pts[0.5] - 0.25) < 1e-6 and abs(pts[0.75] - 0.5625) < 1e-6
    fluxes = [f for _b, f in curve]
    assert all(b - a >= -1e-9 for a, b in pairwise(fluxes))  # monotone


def test_delta_filtered_slow_sensor_recovers_gain() -> None:
    """§4.4: on a delta-filtered slow sensor (Apollo LTR390 regime) the dim
    levels never clear the on-device delta, so the channel calibrates from its
    bright levels with partial coverage — and still recovers the true gain."""
    chans = [Channel("c", gain=100.0, band=Band.PRIMARY)]
    eng = _cal_engine(chans, tun=_field_tun())
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0, delta=10.0, min_cadence=60.0)

    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    results = _drive_field_sweep(eng, plant, start + timedelta(seconds=1))

    assert results and results[0].ok
    cov = dict(results[0].coverage)
    assert cov["c"] < 1.0  # partial coverage — dim levels were sub-delta
    fitted = eng.calibration_of("lab").gains["c"]
    assert abs(fitted - 100.0) / 100.0 < 0.05  # gain recovered within tolerance


def test_delta_filtered_two_levels_rejects_room() -> None:
    """§4.4: a channel that only captures two levels (< CAL_MIN_POINTS) makes the
    room reject missing_samples with the per-channel coverage map."""
    chans = [Channel("c", gain=100.0, band=Band.PRIMARY)]
    eng = _cal_engine(chans, tun=_field_tun())
    # delta 40: only 75 % (56 lx) and 100 % (100 lx) clear it from the 0 baseline.
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 0.0, delta=40.0, min_cadence=60.0)

    start = BASE + timedelta(seconds=70)
    eng.handle(StartCalibration("lab"), start)
    results = _drive_field_sweep(eng, plant, start + timedelta(seconds=1))

    assert results and not results[0].ok
    assert results[0].reason == "missing_samples"
    assert dict(results[0].coverage)["c"] < 0.6  # only two of five levels
    assert not eng._photo["lab"].calibrated  # nothing committed


# --- persistence contract (rule 5) --------------------------------------


def test_calibration_rejects_corrupt_payloads() -> None:
    """Rule 5 (F2): from_dict validates and raises on malformed calibration."""
    good = RoomCalibration(
        room_id="lab",
        gains={"c": 50.0},
        curves={"c": ((0.0, 0.0), (0.5, 0.25), (1.0, 1.0))},
    ).to_dict()
    RoomCalibration.from_dict(good)  # sanity: the good one loads

    def corrupt(**over):
        import copy

        d = copy.deepcopy(good)
        d.update(over)
        return d

    import pytest

    with pytest.raises(ValueError):  # negative gain
        RoomCalibration.from_dict(corrupt(gains={"c": -5.0}))
    with pytest.raises(ValueError):  # NaN gain
        RoomCalibration.from_dict(corrupt(gains={"c": float("nan")}))
    with pytest.raises(ValueError):  # non-monotone flux (spans 0..1, dips mid)
        RoomCalibration.from_dict(
            corrupt(curves={"c": [[0.0, 0.0], [0.5, 0.9], [0.75, 0.4], [1.0, 1.0]]})
        )
    with pytest.raises(ValueError):  # curve does not span b=0..1
        RoomCalibration.from_dict(corrupt(curves={"c": [[0.1, 0.0], [1.0, 1.0]]}))
    with pytest.raises(ValueError):  # relative flux does not span 0..1
        RoomCalibration.from_dict(corrupt(curves={"c": [[0.0, 0.2], [1.0, 1.0]]}))
    with pytest.raises(ValueError):  # non-monotone in b
        RoomCalibration.from_dict(
            corrupt(curves={"c": [[0.0, 0.0], [0.5, 0.5], [0.3, 0.7], [1.0, 1.0]]})
        )
    with pytest.raises(ValueError):  # fewer than two points
        RoomCalibration.from_dict(corrupt(curves={"c": [[0.0, 0.0]]}))
    with pytest.raises(ValueError):  # non-finite curve point
        RoomCalibration.from_dict(corrupt(curves={"c": [[0.0, 0.0], [1.0, float("inf")]]}))
    with pytest.raises(ValueError):  # gains/curves cover different channels
        RoomCalibration.from_dict(corrupt(gains={"c": 5.0, "d": 5.0}))


def test_corrupt_persisted_calibration_leaves_room_uncalibrated() -> None:
    """Rule 5 (F2): a malformed (non-monotone) calibration is not loaded."""
    chans = [Channel("c", gain=100.0)]
    cfg = closed_config(chans, lux_active_day=100.0)
    bad = RoomCalibration("lab", gains={"c": 50.0}, curves={"c": ((0.0, 0.0), (1.0, 0.5))})
    eng = Engine(cfg, calibrations={"lab": bad})  # last point flux != 1 -> invalid
    assert not eng._photo["lab"].calibrated
    assert eng._photo["lab"].gain("c") == 100.0  # config default retained


def test_calibration_roundtrips_and_validates() -> None:
    """Rule 5: calibration is plain data; from_dict round-trips; matches guards."""
    cal = RoomCalibration(
        room_id="lab",
        gains={"a": 12.0, "b": 3.5},
        curves={"a": ((0.0, 0.0), (1.0, 1.0)), "b": ((0.0, 0.0), (0.5, 0.2), (1.0, 1.0))},
    )
    restored = RoomCalibration.from_dict(cal.to_dict())
    assert restored.gains == cal.gains
    assert restored.curves == cal.curves
    assert restored.matches(("a", "b"))
    assert not restored.matches(("a", "c"))  # channel-set mismatch


def test_persisted_calibration_loads_when_channels_match() -> None:
    """Rule 5: a matching persisted calibration loads and marks the room calibrated."""
    chans = [Channel("c", gain=100.0)]
    cfg = closed_config(chans, lux_active_day=100.0)
    cal = RoomCalibration("lab", gains={"c": 77.0}, curves={"c": ((0.0, 0.0), (1.0, 1.0))})
    eng = Engine(cfg, calibrations={"lab": cal})
    assert eng._photo["lab"].calibrated
    assert eng._photo["lab"].gain("c") == 77.0


def test_persisted_calibration_ignored_on_channel_mismatch() -> None:
    """Rule 5: a mismatched calibration is dropped; the room stays uncalibrated."""
    chans = [Channel("c", gain=100.0)]
    cfg = closed_config(chans, lux_active_day=100.0)
    bad = RoomCalibration("lab", gains={"WRONG": 77.0}, curves={"WRONG": ((0.0, 0.0), (1.0, 1.0))})
    eng = Engine(cfg, calibrations={"lab": bad})
    assert not eng._photo["lab"].calibrated
    assert eng._photo["lab"].gain("c") == 100.0  # config default, not the bad 77
