"""§2.3/§2.4/§4.5 (D33): darkness E and wind-down W are separate factors.

E = max(E_sun, E_clock) keeps meaning "it is dark" (CT, outdoor gates, the
corridor evening gate). W = max(E_clock, min(E_sun, dusk_ceiling)) means "it
is late" (tier interpolation, closed-loop targets, the evening cap, the boost
hand-over). The evening cap and the boost hand-over are continuous in W: as W
ramps, no channel moves by more than one §8.3 grid level per circadian tick.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.light_conductor.const import build_engine_config, build_tunables
from custom_components.light_conductor.core import (
    circadian,
    ct_policy,
    estimator,
    governor,
    photometry,
    targets,
)
from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    PresenceChanged,
    ReviewTick,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import (
    ChannelState,
    InitialSnapshot,
    Role,
    RoomCalibration,
)
from custom_components.light_conductor.core.plan import Plan, ScheduleReview
from custom_components.light_conductor.core.tunables import Tunables

from .live_config import LIVE_OPTIONS, SOFAKROK_CURVE, SOFAKROK_GAIN

TZ = ZoneInfo("Europe/Oslo")
SUN_DOWN = -20.0
TUN = Tunables()


def _t(h: int, m: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


# --- the factors ------------------------------------------------------------


@pytest.mark.parametrize("elev", [None, -20.0, -4.0, 0.0, 3.0, 9.0, 30.0])
@pytest.mark.parametrize("hm", [(3, 0), (6, 45), (12, 0), (16, 0), (20, 30), (22, 45)])
def test_w_is_e_at_the_default_ceiling(elev, hm) -> None:
    """D33: dusk_ceiling 1.0 (the default) keeps W ≡ E — beta.19's behaviour."""
    now = _t(*hm)
    assert circadian.wind_down(elev, now, TUN) == circadian.factor(elev, now, TUN)


def test_the_sun_alone_carries_w_only_to_the_ceiling() -> None:
    """D33: sun down at 17:00 makes E = 1, but W = dusk_ceiling until the clock
    ramp takes it the rest of the way 20:00 -> 22:30."""
    tun = replace(TUN, dusk_ceiling=0.4)
    assert circadian.factor(SUN_DOWN, _t(17), tun) == 1.0
    assert circadian.wind_down(SUN_DOWN, _t(17), tun) == 0.4
    assert circadian.wind_down(SUN_DOWN, _t(21, 15), tun) == 0.5  # clock ramp halfway
    assert circadian.wind_down(SUN_DOWN, _t(22, 30), tun) == 1.0
    assert circadian.wind_down(30.0, _t(12), tun) == 0.0
    assert circadian.wind_down(3.0, _t(12), tun) == 0.4  # E_sun 0.5 capped


def test_dusk_ceiling_is_validated() -> None:
    with pytest.raises(ValueError):
        replace(TUN, dusk_ceiling=1.2)
    with pytest.raises(ValueError):
        replace(TUN, dusk_ceiling=-0.1)


# --- self-scheduling ----------------------------------------------------------


def _live(tun: Tunables, occupied: tuple[str, ...] = (), *, outdoor: bool = True) -> Engine:
    snap = InitialSnapshot(
        sun_elevation=SUN_DOWN,
        anyone_home=True,
        occupancy={r: r in occupied for r in ("kjokken", "spisebord", "sofakrok", "kontor")},
    )
    opts = dict(LIVE_OPTIONS)
    if not outdoor:  # a lit balcony ticks on its own (§6.5 self-schedule)
        opts["rooms"] = [r for r in LIVE_OPTIONS["rooms"] if r["shape"] != "outdoor"]
    return Engine(build_engine_config(None, opts), snap, tun)


def _review(cmds) -> datetime | None:
    return next((c.at for c in cmds if isinstance(c, ScheduleReview)), None)


def test_w_ramp_is_self_scheduled_while_e_sits_at_one() -> None:
    """§2.3/D33: with the sun down E is plateaued at 1, yet W climbs 20:00 ->
    22:30 on the clock — the engine must tick through it (beta.19 judged the
    ramp on E alone and slept until 06:00)."""
    tun = replace(build_tunables(LIVE_OPTIONS), dusk_ceiling=0.4)
    eng = _live(tun, ("kjokken",), outdoor=False)
    eng.handle(SunElevationChanged(SUN_DOWN), _t(19, 0))
    plateau = eng.handle(ReviewTick(), _t(19, 1))
    assert _review(plateau) == _t(20, 0)  # W flat at 0.4: next clock boundary
    eng.handle(ReviewTick(), _t(20, 0))
    ramp = eng.handle(ReviewTick(), _t(20, 1))
    assert _review(ramp) == _t(20, 1) + timedelta(seconds=tun.circadian_tick)
    assert 0.0 < eng.state.last_w < 1.0 and eng.state.last_e == 1.0


# --- continuity: ≤ one grid level per circadian tick ------------------------

#: role scenario -> rooms occupied (the room under test is the first word).
SCENARIOS = {
    "kjokken ACTIVE": ("kjokken",),
    "kjokken ADJACENT": ("spisebord",),
    "kjokken BACKGROUND": ("kontor",),
    "spisebord ACTIVE": ("spisebord",),
    "spisebord ADJACENT": ("kjokken",),
    "sofakrok ACTIVE": ("sofakrok",),
    "sofakrok ADJACENT": ("kjokken",),
    "gang ADJACENT": ("kjokken",),
    "kontor ACTIVE": ("kontor",),
}


def _level(ch, cs: ChannelState, tun: Tunables) -> int:
    """Grid index of the emitted level; -1 = off."""
    if not cs.on:
        return -1
    grid = governor.level_grid(ch.dim_floor, tun.quant_step, tun.quant_rel)
    return governor._nearest(grid, cs.commanded_b)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("ramp", ["evening", "morning"])
def test_open_loop_moves_at_most_one_level_per_tick(scenario: str, ramp: str) -> None:
    """D33 (property): W swept through the evening (20:00-22:30) and the faster
    morning (06:00-07:30) clock ramps with the sun down (E fixed at 1,
    dusk_ceiling 0 so W = E_clock): every channel of the live configuration —
    the kitchen bench strip's 0.8x-0.5 mapping and its boost hand-over
    included — changes by at most one §8.3 grid level per circadian tick, in
    ACTIVE, ADJACENT and BACKGROUND. beta.19's cap cliff at E = 0.5 moved the
    kitchen 0.625 -> 0.25 (eleven levels) in one write."""
    tun = replace(build_tunables(LIVE_OPTIONS), dusk_ceiling=0.0)
    room_id = scenario.split()[0]
    occupied = SCENARIOS[scenario]
    eng = _live(tun, occupied)
    start, end = (_t(19, 55), _t(22, 35)) if ramp == "evening" else (_t(5, 55), _t(7, 35))
    t = start
    eng.handle(SunElevationChanged(SUN_DOWN), t)
    room = eng.config.room(room_id)
    rs = eng.state.rooms[room_id]
    prev: dict[str, int] = {}
    roles_seen = set()
    while t < end:
        t += timedelta(seconds=tun.circadian_tick)
        for r in occupied:
            eng.handle(PresenceChanged(r, True), t)
        eng.handle(ReviewTick(), t)
        roles_seen.add(rs.role)
        for ch in room.channels:
            idx = _level(ch, rs.channels[ch.channel_id], tun)
            if ch.channel_id in prev:
                assert abs(idx - prev[ch.channel_id]) <= 1, (scenario, ramp, t, ch.channel_id)
            prev[ch.channel_id] = idx
    assert Role(scenario.split()[1].lower()) in roles_seen


def _sofakrok_engine(tun: Tunables) -> Engine:
    cal = RoomCalibration(
        "sofakrok",
        {"light.sofakrok_taklys": SOFAKROK_GAIN},
        {"light.sofakrok_taklys": SOFAKROK_CURVE},
    )
    return Engine(build_engine_config(None, LIVE_OPTIONS), None, tun, {"sofakrok": cal})


@pytest.mark.parametrize("role", [Role.ACTIVE, Role.ADJACENT, Role.BACKGROUND])
@pytest.mark.parametrize("g", [1.0, 2.0])
def test_closed_loop_goal_moves_at_most_one_level_per_tick(role: Role, g: float) -> None:
    """D33 (property): the closed loop's feed-forward goal for sofakrok's live
    calibration — target lux interpolated by W, demand band-filled and capped
    by cap(W) — moves by at most one grid level per tick at the fastest W ramp,
    including at master gain 2, where the cap binds. (The deadband still
    decides WHEN a correction lands; this is the level it lands on.)"""
    tun = build_tunables(LIVE_OPTIONS)
    eng = _sofakrok_engine(tun)
    room = eng.config.room("sofakrok")
    rs = eng.state.rooms["sofakrok"]
    photo = eng._photo["sofakrok"]
    capacity = eng._room_capacity(rs, room, photo)
    ch = room.channels[0]
    cs = ChannelState()
    dw = tun.circadian_tick / ((tun.morning_full_min - tun.morning_start_min) * 60.0)
    steps = int(1.0 / dw) + 1
    prev = None
    for k in range(steps + 1):
        w = min(1.0, k * dw)
        t_prime = estimator.target_lux(rs, role, room.profile, w, g, tun, capacity)
        goal = eng._closed_loop_goal(room, rs, t_prime, w, photo)[ch.channel_id]
        governor.plan_channel(Plan(), ch, cs, True, goal, None, photo, tun)
        idx = _level(ch, cs, tun)
        if prev is not None:
            assert abs(idx - prev) <= 1, (role, g, w)
        prev = idx


# --- the specific cliffs ------------------------------------------------------


def test_the_kitchen_cap_cliff_is_gone() -> None:
    """D33: around W = 0.5 the kitchen's ACTIVE output is the interpolation
    (0.625), not the cap (0.25) one step later."""
    cfg = build_engine_config(None, LIVE_OPTIONS)
    profile = cfg.room("kjokken").profile
    below = targets.apply_evening_cap(
        targets.role_outputs(profile, Role.ACTIVE, 0.499, TUN), 0.499, profile, TUN
    )
    above = targets.apply_evening_cap(
        targets.role_outputs(profile, Role.ACTIVE, 0.501, TUN), 0.501, profile, TUN
    )
    for band, v in below.items():
        assert abs(v - above[band]) < 0.002
        assert abs(above[band] - 0.625) < 0.002


def test_the_bench_strip_hands_over_to_its_evening_value() -> None:
    """§4.5/D26/D33: the kitchen bench strip (boost, 0.8x-0.5, evening 0.85)
    fades from its tier to its evening value across the window below
    boost_evening_max instead of jumping 0.06 -> 0.18 at it."""
    eng = _live(build_tunables(LIVE_OPTIONS))
    room = eng.config.room("kjokken")
    profile = room.profile
    out = []
    for k in range(0, 101):
        w = k / 100
        bands = targets.apply_evening_cap(
            targets.role_outputs(profile, Role.ACTIVE, w, TUN), w, profile, TUN
        )
        b = photometry.allocate(
            room.channels,
            bands,
            targets.boost_weight(w, TUN),
            TUN,
            targets.boost_evening_value(Role.ACTIVE, profile, 1.0),
        )["light.kjokken_benkebelysning"]
        out.append(b)
    assert max(abs(b - a) for a, b in itertools.pairwise(out)) < 0.02
    assert abs(out[50] - (0.8 * 0.85 - 0.5)) < 1e-9  # W >= boost_evening_max: as before
    assert abs(out[0] - 0.3) < 1e-9  # W = 0: the day tier, mapped


def test_ct_follows_darkness_not_wind_down() -> None:
    """§5/D33: at 18:00 with the sun down and dusk_ceiling 0.4 the room is still
    bright (W = 0.4) but its CT is already evening-warm (E = 1)."""
    tun = replace(build_tunables(LIVE_OPTIONS), dusk_ceiling=0.4)
    eng = _live(tun, ("kjokken",))
    eng.handle(SunElevationChanged(SUN_DOWN), _t(18, 0))
    eng.handle(ReviewTick(), _t(18, 1))
    cs = eng.state.rooms["kjokken"].channels["light.kjokken_downlights"]
    assert cs.commanded_b > 0.6  # W = 0.4: 0.6·1.0 + 0.4·0.25 = 0.7
    ch = eng.config.room("kjokken").channels[0]
    assert cs.commanded_ct == ct_policy.ct_target(ch, 1.0, cs.commanded_b, 2700, tun)
