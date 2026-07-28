"""§4.7 daylight-aware open-loop, end to end against the real Engine.

An untrusted lux-sensor room (fresh sensor, not yet calibrated/bootstrap-
confident) runs the open-loop tables scaled by the daylight factor D; a stale
sensor, a trusted room, and mode paths (outdoor/night/TV) are never scaled.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    LuxReport,
    ReviewTick,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import (
    Band,
    ChannelConfig,
    EngineConfig,
    InitialSnapshot,
    Profile,
    RoomConfig,
    RoomShape,
    Vacancy,
)

from .plant import Channel, booted_engine, closed_config

DAY = 20.0  # sun high ⇒ E = 0
START = datetime(2026, 7, 1, 12, 1, 0)


def _commanded(eng: Engine, cid: str = "c") -> float:
    return eng.state.rooms["lab"].channels[cid].commanded_b


def _feed(eng: Engine, lux: float, t: datetime, n: int = 4, dt: float = 2.0) -> datetime:
    for _ in range(n):
        eng.handle(LuxReport("lab", lux), t)
        t = t + timedelta(seconds=dt)
    return t


# --- untrusted room is daylight-scaled ----------------------------------


def test_untrusted_room_scaled_by_daylight() -> None:
    """§4.7: an uncalibrated lux room damps its open-loop output in daylight."""
    chans = [Channel("c", gain=180.0, model_gain=1.0)]  # untrusted: model gain 1.0
    cfg = closed_config(chans, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=False)

    # Bright daylight N̂ ≈ 150 ⇒ D = 1 - 150/200 = 0.25 ⇒ 0.8 x 0.25 = 0.2.
    _feed(eng, 150.0, START, n=1)
    assert abs(_commanded(eng) - 0.2) < 0.03
    # And the room is still open-loop (no closed-loop target published).
    diag = eng.handle(ReviewTick(), START + timedelta(seconds=2))
    d = next(c for c in diag if hasattr(c, "rooms")).rooms[0]
    assert d.target_lux is None  # open-loop path
    assert d.natural_lux is not None  # N̂ still surfaced for the untrusted room


def test_untrusted_room_floors_at_min_factor_when_very_bright() -> None:
    """§4.7: N̂ ≥ daylight_full floors the output at daylight_min_factor (0)."""
    chans = [Channel("c", gain=180.0, model_gain=1.0)]
    cfg = closed_config(chans, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=False)
    _feed(eng, 250.0, START, n=1)  # N̂ ≥ 200 ⇒ D = 0
    assert _commanded(eng) < 0.03  # damped to the floor (channel off / dim floor)


def test_untrusted_room_full_output_in_the_dark() -> None:
    """§4.7: with no natural light D → 1 — the tables run at full strength."""
    chans = [Channel("c", gain=180.0, model_gain=1.0)]
    cfg = closed_config(chans, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=False)
    _feed(eng, 0.0, START, n=1)  # N̂ ≈ 0 ⇒ D = 1
    assert abs(_commanded(eng) - 0.8) < 0.03


# --- stale sensor falls back to UNSCALED open-loop ----------------------


def test_stale_sensor_is_not_daylight_scaled() -> None:
    """§4.7/§3.5: a stale sensor drops to unscaled open-loop (D → 1)."""
    chans = [Channel("c", gain=180.0, model_gain=1.0)]
    cfg = closed_config(chans, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=False)
    _feed(eng, 150.0, START, n=1)  # scaled while fresh
    assert _commanded(eng) < 0.5
    # Let the sensor age out (> lux_stale 300 s) with review ticks only.
    t = START + timedelta(seconds=400)
    for _ in range(3):
        eng.handle(ReviewTick(), t)
        t = t + timedelta(seconds=5)
    # Stale ⇒ open-loop unscaled ⇒ back toward full 0.8 (N̂ no longer applied).
    assert _commanded(eng) > 0.7


# --- a trusted (calibrated) room is on the closed-loop path --------------


def test_trusted_room_uses_closed_loop_not_daylight_scaling() -> None:
    """§4.7: a calibrated room regulates in lux (closed loop) — never the
    open-loop daylight path (its diagnostics carry a closed-loop target)."""
    chans = [Channel("c", gain=180.0)]  # calibrated to true gain
    cfg = closed_config(chans, lux_active_day=100.0, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=True)
    _feed(eng, 60.0, START, n=3)
    diag = eng.handle(ReviewTick(), START + timedelta(seconds=8))
    d = next(c for c in diag if hasattr(c, "rooms")).rooms[0]
    assert d.target_lux is not None  # closed-loop path, not open-loop daylight


# --- capacity gate: low-capacity calibrated rooms stay open-loop ---------


def _target_of(diag: list) -> float | None:
    return next(c for c in diag if hasattr(c, "rooms")).rooms[0].target_lux


def test_low_capacity_calibrated_room_stays_open_loop() -> None:
    """§4.5/§4.7: a calibrated room below the capacity gate (kjøkken-like, C≈2 <
    4 lx) runs the daylight-aware open-loop path, NOT closed loop — servoing ~1
    lx targets against ~1 lx quantization would never visibly light."""
    chans = [Channel("c", gain=2.0)]  # calibrated but tiny capacity C≈2
    cfg = closed_config(chans, lux_active_day=100.0, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=True)
    _feed(eng, 60.0, START, n=3)
    diag = eng.handle(ReviewTick(), START + timedelta(seconds=8))
    assert _target_of(diag) is None  # below the gate → open-loop, no closed-loop target
    # Output is daylight-scaled (N̂≈60 ⇒ D≈0.7 ⇒ 0.8·0.7≈0.56), not lux-servoed.
    assert 0.4 < _commanded(eng) < 0.7


def test_capacity_above_gate_uses_closed_loop() -> None:
    """§4.5: a calibrated room above the gate (C≈10 ≥ 4) regulates in lux."""
    chans = [Channel("c", gain=10.0)]  # C≈10 ≥ min_closed_loop_capacity
    cfg = closed_config(chans, lux_active_day=6.0, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=True)
    _feed(eng, 3.0, START, n=3)
    diag = eng.handle(ReviewTick(), START + timedelta(seconds=8))
    assert _target_of(diag) is not None  # closed-loop path (above the capacity gate)


def test_capacity_exactly_at_gate_uses_closed_loop() -> None:
    """§4.5: the gate is inclusive — C == min_closed_loop_capacity closes the loop."""
    chans = [Channel("c", gain=4.0)]  # C = 4.0 == min_closed_loop_capacity default
    cfg = closed_config(chans, lux_active_day=3.0, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=True)
    _feed(eng, 1.0, START, n=3)
    diag = eng.handle(ReviewTick(), START + timedelta(seconds=8))
    assert _target_of(diag) is not None  # ≥ is inclusive: at-boundary room is closed-loop


def test_capacity_gate_respects_the_tunable() -> None:
    """§4.5: lowering min_closed_loop_capacity lets a tiny room close the loop."""
    from datetime import datetime

    from custom_components.light_conductor.core.engine import Engine
    from custom_components.light_conductor.core.events import PresenceChanged
    from custom_components.light_conductor.core.model import InitialSnapshot
    from custom_components.light_conductor.core.tunables import Tunables

    from .plant import calibration_for

    chans = [Channel("c", gain=2.0)]  # C≈2
    cfg = closed_config(chans, lux_active_day=1.5, out_active_day={Band.PRIMARY: 0.8})
    tun = Tunables(min_closed_loop_capacity=1.0)  # gate below C=2
    eng = Engine(
        cfg,
        InitialSnapshot(sun_elevation=DAY, occupancy={"lab": True}),
        tunables=tun,
        calibrations={"lab": calibration_for(cfg, "lab")},
    )
    base = datetime(2026, 7, 1, 12, 0, 0)
    eng.handle(SunElevationChanged(DAY), base)
    eng.handle(PresenceChanged("lab", True), base + timedelta(seconds=40))
    _feed(eng, 1.0, START, n=3)
    diag = eng.handle(ReviewTick(), START + timedelta(seconds=8))
    assert _target_of(diag) is not None  # gate lowered → closed loop even at C≈2


# --- a mode path (outdoor) is never daylight-scaled ---------------------


def _outdoor_lux_cfg() -> EngineConfig:
    prof = Profile(
        vacancy=Vacancy.DIM,
        out_background={Band.PRIMARY: 0.3},
        out_active_evening={Band.PRIMARY: 0.6},
    )
    room = RoomConfig(
        room_id="lab",
        channels=(ChannelConfig("c", band=Band.PRIMARY, fixed_ct=2700),),
        profile=prof,
        shape=RoomShape.OUTDOOR,
        has_lux_sensor=True,
    )
    return EngineConfig(rooms=(room,))


def test_outdoor_mode_output_ignores_daylight_factor() -> None:
    """§4.7: outdoor (a §6 mode table) is not daylight-scaled even with a bright
    sensor — its dusk background comes straight from the profile table."""
    eng = Engine(_outdoor_lux_cfg(), InitialSnapshot(sun_elevation=-8.0))  # E = 1 (dusk)
    base = datetime(2026, 7, 1, 22, 0, 0)
    eng.handle(SunElevationChanged(-8.0), base)
    # Feed a bright sensor reading; the outdoor mode path must ignore it.
    t = _feed(eng, 150.0, base + timedelta(seconds=40), n=2)
    eng.handle(ReviewTick(), t + timedelta(seconds=60))  # past startup grace
    # out_background is 0.3 — unscaled by D (which would give ~0.075).
    assert abs(eng.state.rooms["lab"].channels["c"].commanded_b - 0.3) < 0.03


def test_daylight_latch_prevents_hunting_while_observations_pend() -> None:
    """§4.7 latch invariant: with a high-own-gain untrusted room under swinging
    daylight, the daylight factor must not re-command output while a shadow
    observation is settling — command direction reversals stay tiny and
    observations still arm (the latch neither oscillates nor starves)."""
    import math
    from itertools import pairwise

    chans = [Channel("c", gain=180.0, model_gain=1.0)]
    cfg = closed_config(chans, out_active_day={Band.PRIMARY: 0.8})
    eng = booted_engine(cfg, sun=DAY, calibrated=False)

    t = START
    seen_pending = False
    history: list[float] = []
    for i in range(240):  # 40 min at 10 s cadence
        lux = 75.0 + 75.0 * math.sin(i / 24.0)  # slow 0-150 lx swell
        eng.handle(LuxReport("lab", lux), t)
        est = eng.state.rooms["lab"].est
        seen_pending = seen_pending or est.pending_valid
        history.append(_commanded(eng))
        t = t + timedelta(seconds=10)

    deltas = [b - a for a, b in pairwise(history) if abs(b - a) > 1e-9]
    reversals = sum(1 for a, b in pairwise(deltas) if a * b < 0)
    assert reversals <= 4, f"daylight loop hunting: {reversals} reversals"
    assert seen_pending  # the latch never starved bootstrap observations
    # And the output genuinely tracked daylight (not frozen throughout).
    assert max(history) - min(history) > 0.2
