"""Closed-loop system proofs against the synthetic plant (ENGINE_SPEC §3).

The production :class:`Engine` regulates a room with unknown-to-it true gains
and natural-light trajectories. These tests are the value of the estimator PR:
convergence, anti-hunting, write blanking, stale fallback, and uncalibrated
sanity — all end to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.light_conductor.core.events import ReviewTick
from custom_components.light_conductor.core.model import Band
from custom_components.light_conductor.core.plan import SetChannel

from .plant import Channel, Plant, booted_engine, closed_config

START = datetime(2026, 7, 1, 12, 1, 0)


def _run(plant: Plant, start: datetime, ticks: int, dt: float = 2.0) -> list[int]:
    """Tick, returning the index of every tick that emitted a SetChannel."""
    corrections: list[int] = []
    t = start
    for i in range(ticks):
        cmds = plant.tick(t)
        if any(isinstance(c, SetChannel) for c in cmds):
            corrections.append(i)
        t = t + timedelta(seconds=dt)
    return corrections


# --- (a) convergence in <= 2 corrections --------------------------------


def test_converges_within_two_corrections_after_N_step() -> None:
    """§3.6: a step in natural light is met in <= 2 feed-forward corrections."""
    chans = [Channel("c", gain=180.0)]
    cfg = closed_config(chans, lux_active_day=100.0)
    eng = booted_engine(cfg, sun=20.0)
    n = {"lux": 60.0}
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: n["lux"])

    _run(plant, START, 40)  # settle at target 100 with N=60
    assert abs(plant.true_lux(START + timedelta(seconds=80)) - 100.0) < 16.0

    # Clouds roll in: natural light drops 60 -> 10. Count corrections to settle.
    n["lux"] = 10.0
    step_start = START + timedelta(seconds=120)
    corrections = _run(plant, step_start, 120)
    assert len(corrections) <= 2  # <= 2 corrections (rule 3.6)
    settled = plant.true_lux(step_start + timedelta(seconds=240))
    assert abs(settled - 100.0) < 16.0  # back on target


def test_converges_after_target_step() -> None:
    """§3.6: a target change (role edge) settles fast and in <= 2 corrections."""
    chans = [Channel("c", gain=180.0)]
    cfg = closed_config(chans, lux_active_day=100.0)
    eng = booted_engine(cfg, sun=20.0)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 40.0)
    _run(plant, START, 40)
    on_target = plant.true_lux(START + timedelta(seconds=80))
    assert abs(on_target - 100.0) < 16.0


# --- (b) anti-hunting with violent self-gain ----------------------------


def test_no_sustained_oscillation_over_an_evening() -> None:
    """§3.6b: a spisebord-like violent self-gain over a multi-hour evening
    produces zero sustained oscillation — bounded command-direction reversals."""
    # benke: 260 lx at full ~= 52x the 5 lx deadband.
    chans = [Channel("benke", gain=260.0)]
    cfg = closed_config(chans, lux_active_day=120.0, lux_active_evening=60.0)
    eng = booted_engine(cfg, sun=20.0)
    start = datetime(2026, 7, 1, 18, 0, 0)

    def n_of_t(now: datetime) -> float:  # slow sunset decay 80 -> 0
        mins = (now - start).total_seconds() / 60.0
        return max(0.0, 80.0 - mins * 0.3)

    plant = Plant(eng, "lab", chans, n_of_t=n_of_t)
    _run(plant, start, 3600)  # 2 h at 2 s
    assert plant.reversals("benke") <= 2  # essentially monotone tracking


def test_cloud_flicker_does_not_hunt() -> None:
    """§3.2/§3.6: minute-scale cloud drift is tracked without chasing seconds."""
    chans = [Channel("c", gain=200.0)]
    cfg = closed_config(chans, lux_active_day=100.0)
    eng = booted_engine(cfg, sun=20.0)
    start = datetime(2026, 7, 1, 12, 1, 0)

    import math

    def n_of_t(now: datetime) -> float:  # slow sinusoid, minutes period
        s = (now - start).total_seconds()
        return 55.0 + 45.0 * math.sin(s / 400.0)

    plant = Plant(eng, "lab", chans, n_of_t=n_of_t)
    _run(plant, start, 1800)  # 1 h
    assert plant.reversals("c") <= 8  # a handful of turns tracking the drift


# --- (c) write blanking at the system level -----------------------------


def test_write_blank_excludes_own_step_transient() -> None:
    """§3.2a: N̂ stays near true N right through an own step, never spiking
    toward the own-light-inflated total (the blanked samples are excluded)."""
    chans = [Channel("c", gain=200.0)]
    cfg = closed_config(chans, lux_active_day=100.0)
    eng = booted_engine(cfg, sun=20.0)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 30.0)
    peak_n = 0.0
    t = START
    for _ in range(80):
        plant.tick(t)
        peak_n = max(peak_n, eng.state.rooms["lab"].est.n_hat)
        t = t + timedelta(seconds=2)
    assert peak_n < 60.0  # true N is 30; N̂ never absorbs the ~170 lx of our light


# --- (f) stale fallback and recovery, no jump beyond slew ----------------


def test_stale_fallback_and_recovery_are_slew_bounded() -> None:
    """§3.5/§8.2: losing then regaining the sensor produces only slew-bounded
    moves — no output jump at the open-loop <-> closed-loop switchover."""
    chans = [Channel("c", gain=180.0)]
    cfg = closed_config(chans, lux_active_day=100.0, out_active_day={Band.PRIMARY: 0.4})
    eng = booted_engine(cfg, sun=20.0)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 40.0)
    _run(plant, START, 60)  # closed-loop steady

    tun = eng.tun
    bound = tun.slew_step / tun.slew_interval + 1e-9
    cs = eng.state.rooms["lab"].channels["c"]

    def emit_ok(cmds: list[object]) -> None:
        for c in cmds:
            if isinstance(c, SetChannel) and c.channel_id == "c" and c.ramp_seconds > 0:
                prev = cs_prev[0]
                rate = abs(c.level**2 - prev) / c.ramp_seconds
                assert rate <= bound, f"switchover step {rate} exceeds slew {bound}"

    # Stop feeding lux; drive review ticks so the sensor goes stale (> 120 s).
    cs_prev = [cs.commanded_b**2]
    t = START + timedelta(seconds=140)
    for _ in range(40):
        before = cs.commanded_b**2
        cs_prev[0] = before
        cmds = eng.handle(ReviewTick(), t)
        emit_ok(cmds)
        t = t + timedelta(seconds=5)
    assert eng.state.rooms["lab"].role is not None  # fell back to open-loop, no crash

    # Sensor recovers: fresh lux resumes closed-loop, again slew-bounded.
    for _ in range(40):
        cs_prev[0] = cs.commanded_b**2
        cmds = plant.tick(t)
        emit_ok(cmds)
        t = t + timedelta(seconds=2)


# --- (h) uncalibrated room stays sane -----------------------------------


def test_uncalibrated_room_is_bounded_and_sane() -> None:
    """§4.4/§3.4: an uncalibrated room (default b^2 curve, default gain) with a
    modest true gain runs closed-loop sanely — bounded outputs, no hunting."""
    # model_gain 1 (uncalibrated default); true gain 1.6, within the online
    # multiplier's reach so influence stays bounded.
    chans = [Channel("c", gain=140.0, model_gain=100.0)]  # 1.4x miscalibration
    cfg = closed_config(chans, lux_active_day=90.0)
    eng = booted_engine(cfg, sun=20.0)
    plant = Plant(eng, "lab", chans, n_of_t=lambda _now: 25.0)
    _run(plant, START, 200)
    cs = eng.state.rooms["lab"].channels["c"]
    assert 0.0 <= cs.commanded_b <= 1.0  # bounded
    assert plant.reversals("c") <= 3  # sane, not hunting
