"""§8: write governor — slew sizing, quantization, min-delta, off-is-off."""

from __future__ import annotations

from itertools import pairwise
from math import isclose, sqrt

from custom_components.light_conductor.core import governor
from custom_components.light_conductor.core.model import (
    ChannelConfig,
    ChannelState,
    Profile,
    RoomConfig,
)
from custom_components.light_conductor.core.photometry import RoomPhotometry
from custom_components.light_conductor.core.plan import Plan, SetChannel, TurnOffChannel
from custom_components.light_conductor.core.tunables import Tunables

TUN = Tunables()
CH = ChannelConfig("c", fixed_ct=None, ct_range=(2200, 4000), dim_floor=0.05)


def _photo() -> RoomPhotometry:
    return RoomPhotometry(RoomConfig("r", (CH,), Profile()))


def _plan(cs: ChannelState, active: bool, b: float, ct: int | None = None, fade=None) -> Plan:
    plan = Plan()
    governor.plan_channel(plan, CH, cs, active, b, ct, _photo(), TUN, fade)
    return plan


def test_turn_on_from_off_crosses() -> None:
    """§8.6: goal > 0 from off emits a SetChannel (crossing on)."""
    cs = ChannelState()
    cmd = _plan(cs, True, 0.6).commands[0]
    assert isinstance(cmd, SetChannel)
    assert cs.on and cs.commanded_b > 0.0


def test_off_is_off() -> None:
    """§8.6: goal 0 turns the channel off, never brightness 0."""
    cs = ChannelState(commanded_b=0.6, on=True)
    cmds = _plan(cs, False, 0.0).commands
    assert isinstance(cmds[0], TurnOffChannel)
    assert not cs.on and cs.commanded_b == 0.0
    # Already off: no command.
    assert _plan(ChannelState(), False, 0.0).commands == []


def test_dim_floor_floors_lit_channel() -> None:
    """§4.1/4.6: a lit channel below the dim floor is floored up, not off."""
    cs = ChannelState()
    cmd = _plan(cs, True, 0.01).commands[0]
    assert isinstance(cmd, SetChannel)
    assert cmd.level >= CH.dim_floor


def test_sub_step_jitter_emits_nothing() -> None:
    """§8.3: a goal that stays inside the lit channel's sticky cell (here +0.01
    flux at b=0.6, ~1.4 % in b against a ~5.6 % grid gap) emits nothing."""
    photo = _photo()
    cs = ChannelState(commanded_b=0.611593, commanded_ct=3000, on=True)
    goal_b = photo.command_for_flux("c", photo.flux("c", 0.611593) + 0.01)
    plan = Plan()
    governor.plan_channel(plan, CH, cs, True, goal_b, None, photo, TUN)
    assert plan.commands == []


def _lit(flux: float) -> ChannelState:
    """A channel already lit at ``flux`` (b² curve) — a MOVE, not a turn-on, so
    the §8.2 turn-on cap (D26) does not apply and the slew formula is visible."""
    return ChannelState(commanded_b=sqrt(flux), on=True)


def _grid() -> tuple[float, ...]:
    return governor.level_grid(CH.dim_floor, TUN.quant_step, TUN.quant_rel)


def test_slew_ramp_numeric_active_and_empty() -> None:
    """§8.2: ramp_seconds = |Δflux| / slew * interval — concrete values.

    Both ends sit exactly on the §8.3 grid (b 0.505449 -> 0.895433), so the
    flux step is exact: occupied rooms ramp at slew_step 0.1/s, empty ones at
    slew_step_empty 0.25/s."""
    from dataclasses import replace

    tun = replace(TUN, slew_step=0.1, slew_interval=1.0, slew_step_empty=0.25)
    photo = _photo()
    lo, hi = 0.505449, 0.895433
    assert lo in _grid() and hi in _grid()
    step = hi * hi - lo * lo

    active = Plan()
    governor.plan_channel(active, CH, _lit(lo * lo), True, hi, None, photo, tun)
    assert isclose(active.commands[0].ramp_seconds, step / 0.1)

    empty = Plan()
    governor.plan_channel(empty, CH, _lit(lo * lo), False, hi, None, photo, tun)
    assert isclose(empty.commands[0].ramp_seconds, step / 0.25)


def test_slew_ramp_scales_linearly_with_step() -> None:
    """§8.2 (mutation-sensitive): the ramp is proportional to the flux step."""
    from dataclasses import replace

    tun = replace(TUN, slew_step=0.1, slew_interval=1.0)
    photo = _photo()
    lo, mid, hi = 0.505449, 0.740027, 0.895433
    big = Plan()
    governor.plan_channel(big, CH, _lit(lo * lo), True, hi, None, photo, tun)
    small = Plan()
    governor.plan_channel(small, CH, _lit(lo * lo), True, mid, None, photo, tun)
    ratio = (hi * hi - lo * lo) / (mid * mid - lo * lo)
    assert isclose(big.commands[0].ramp_seconds / small.commands[0].ramp_seconds, ratio)


def test_zero_dim_floor_quantizes_to_off() -> None:
    """§8.6 (F8): a positive goal that quantizes to nothing turns off, never
    emits SetChannel(level=0)."""
    ch = ChannelConfig("c", fixed_ct=2700, dim_floor=0.0)
    photo = RoomPhotometry(RoomConfig("r", (ch,), Profile()))
    cs = ChannelState(commanded_b=0.5, on=True)
    plan = Plan()
    governor.plan_channel(plan, ch, cs, True, 0.004, None, photo, TUN)  # nearest level is 0
    assert isinstance(plan.commands[0], TurnOffChannel)
    assert not any(isinstance(c, SetChannel) for c in plan.commands)
    assert not cs.on


def test_ct_min_delta_gate() -> None:
    """§5.4: CT is only rewritten when it moves >= ct_min_delta."""
    photo = _photo()
    cs = ChannelState(commanded_b=0.6, commanded_ct=3000, on=True)
    # Same brightness, CT nudged 50 K (< 100): no rewrite, so no command at all.
    plan = Plan()
    governor.plan_channel(plan, CH, cs, True, 0.6, 3050, photo, TUN)
    assert plan.commands == []
    # CT moves 200 K: a command carrying the new CT is emitted.
    plan2 = Plan()
    governor.plan_channel(plan2, CH, cs, True, 0.6, 3250, photo, TUN)
    assert isinstance(plan2.commands[0], SetChannel)
    assert plan2.commands[0].ct == 3250


def test_fade_override() -> None:
    """Mode transitions pass an explicit fade (sleep/night)."""
    cmd = _plan(ChannelState(), False, 0.5, fade=4.0).commands[0]
    assert cmd.ramp_seconds == 4.0


# --- §8.2 (D26): the turn-on ramp cap -------------------------------------


def test_turn_on_ramp_is_capped_at_on_ramp_max() -> None:
    """§8.2/D26: lighting a dark channel is capped at on_ramp_max — the
    full-range ramp (flux 1.0 / slew_step 0.1 = 10 s) read as sluggish."""
    from dataclasses import replace

    tun = replace(TUN, slew_step=0.1, slew_interval=1.0, on_ramp_max=3.0)
    photo = _photo()
    plan = Plan()
    governor.plan_channel(plan, CH, ChannelState(), True, 1.0, None, photo, tun)
    assert plan.commands[0].ramp_seconds == 3.0  # capped, not 10.0

    # A turn-on already shorter than the cap is untouched (grid level 0.459499).
    short = Plan()
    governor.plan_channel(short, CH, ChannelState(), True, 0.459499, None, photo, tun)
    assert isclose(short.commands[0].ramp_seconds, 0.459499**2 / 0.1)  # ~2.1 s


def test_dim_and_off_ramps_are_not_capped() -> None:
    """§8.2/D26: only a TURN-ON is capped — a dim and a fade to off keep their
    full slew-derived ramp (that continuity is what the slew bound is for)."""
    from dataclasses import replace

    tun = replace(TUN, slew_step=0.1, slew_interval=1.0, on_ramp_max=3.0)
    photo = _photo()
    dim = Plan()
    lit = ChannelState(commanded_b=1.0, on=True)
    governor.plan_channel(dim, CH, lit, True, 0.740027, None, photo, tun)
    assert isclose(dim.commands[0].ramp_seconds, (1.0 - 0.740027**2) / 0.1)  # ~4.5 s

    off = Plan()
    governor.plan_channel(
        off, CH, ChannelState(commanded_b=1.0, on=True), True, 0.0, None, photo, tun
    )
    assert isinstance(off.commands[0], TurnOffChannel)
    assert off.commands[0].ramp_seconds == 10.0  # 1.0 / 0.1


def test_mode_fade_still_wins_over_the_turn_on_cap() -> None:
    """§6.1/§8.2: an explicit mode fade is the ramp, cap or no cap."""
    from dataclasses import replace

    tun = replace(TUN, on_ramp_max=3.0)
    plan = Plan()
    governor.plan_channel(plan, CH, ChannelState(), False, 0.5, None, _photo(), tun, 10.0)
    assert plan.commands[0].ramp_seconds == 10.0


# --- §8.3 (D27): the command-space grid -----------------------------------

#: sofakrok's live calibrated curve (diagnostics 2026-11-01): near-linear at the
#: dim end and flat above b = 0.75 — the opposite shape to the default b².
SOFAKROK_CURVE = (
    (0.0, 0.0),
    (0.1, 0.12558575979686565),
    (0.25, 0.3402061965551038),
    (0.5, 0.7441424873953103),
    (0.75, 1.0),
    (1.0, 1.0),
)
FLOOR_CH = ChannelConfig("c", fixed_ct=2700, dim_floor=0.02)


def _bound(goal: float) -> float:
    """The §8.3 rendering bound: |b_emitted - b_goal| <= max(0.01, 0.08·b_goal)."""
    return max(0.01, 0.08 * goal)


def _photo_for(ch: ChannelConfig) -> RoomPhotometry:
    return RoomPhotometry(RoomConfig("r", (ch,), Profile()))


def _step(ch: ChannelConfig, cs: ChannelState, goal: float, photo=None) -> Plan:
    plan = Plan()
    governor.plan_channel(plan, ch, cs, True, goal, None, photo or _photo_for(ch), TUN)
    return plan


def test_dim_goals_render_near_intended_from_off() -> None:
    """§8.3/D27: the live dim tiers (background 8 %, night 2-5 %, TV 5/10 %,
    kjøkken ADJACENT ~12 %) render near their goal — beta.19's 0.03 flux grid
    rendered every one of them as the 2 % floor."""
    for goal in (0.05, 0.08, 0.10, 0.12, 0.15):
        cs = ChannelState()
        cmd = _step(FLOOR_CH, cs, goal).commands[0]
        assert isinstance(cmd, SetChannel)
        assert abs(cmd.level - goal) <= _bound(goal), (goal, cmd.level)
        assert cs.commanded_b == cmd.level


def test_from_the_floor_a_dim_goal_emits() -> None:
    """§8.3/D27: a lamp at the dim floor is never stranded there. beta.19's first
    grid level above the floor (b ≈ 0.173) was a 0.0296 flux move — under its
    0.03 gate — so a goal of 0.15 emitted nothing, forever."""
    cs = ChannelState(commanded_b=0.02, on=True)
    plan = _step(FLOOR_CH, cs, 0.15)
    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert isinstance(cmd, SetChannel)
    assert abs(cmd.level - 0.15) <= _bound(0.15)


def test_from_mid_dim_a_lower_goal_emits() -> None:
    """§8.3/D27: from b = 0.173 (beta.19's lowest non-floor level) a goal of
    0.08 must step down — the old gate measured that as a 0.0296 flux move and
    held the lamp at 17 %."""
    cs = ChannelState(commanded_b=0.173, on=True)
    plan = _step(FLOOR_CH, cs, 0.08)
    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert isinstance(cmd, SetChannel)
    assert abs(cmd.level - 0.08) <= _bound(0.08)


def test_repeated_identical_goals_emit_nothing() -> None:
    """§8.3: once a goal is rendered, re-submitting it is a no-op (write economy)."""
    for goal in (0.02, 0.05, 0.08, 0.3, 0.77, 1.0):
        cs = ChannelState()
        assert len(_step(FLOOR_CH, cs, goal).commands) == 1
        for _ in range(3):
            assert _step(FLOOR_CH, cs, goal).commands == []


def test_grid_is_bounded_and_spans_the_range() -> None:
    """§8.3/D27: ≤ 50 levels for any dim floor (write volume), the dim floor
    and full output are levels, steps are 0.01 at the dim end and ~10 % above."""
    for floor in (0.0, 0.02, 0.05):
        grid = governor.level_grid(floor, TUN.quant_step, TUN.quant_rel)
        assert len(grid) <= 50
        assert grid[0] == floor and grid[-1] == 1.0
        assert all(b > a for a, b in pairwise(grid))
    grid = governor.level_grid(0.02, TUN.quant_step, TUN.quant_rel)
    assert len(grid) == 33
    assert grid[-2:] == (0.895433, 1.0)  # no sliver gap below full output
    assert grid[:4] == (0.02, 0.03, 0.04, 0.05)
    big = [b / a - 1.0 for a, b in pairwise(grid) if a >= 0.1 and b < 1.0]
    assert all(isclose(r, 0.1, abs_tol=1e-4) for r in big)


def _sweep_goals() -> list[float]:
    return [0.02 + i * 0.0025 for i in range(393)]  # 0.02 .. 1.0


def _starting_states() -> list[ChannelState]:
    """Off, the floor, on-grid and OFF-grid lit levels (adopted dials, seeds)."""
    lit = (0.02, 0.0353, 0.0706, 0.12, 0.173, 0.3, 0.4627, 0.7216, 0.95, 1.0)
    return [ChannelState()] + [ChannelState(commanded_b=b, on=True) for b in lit]


def _check_render_and_gate(ch: ChannelConfig, photo: RoomPhotometry) -> None:
    grid = governor.level_grid(ch.dim_floor, TUN.quant_step, TUN.quant_rel)
    for start in _starting_states():
        for goal in _sweep_goals():
            cs = ChannelState(start.commanded_b, start.commanded_ct, start.on)
            rendered = governor.quantize(ch, cs, goal, TUN)
            plan = _step(ch, cs, goal, photo)
            # Rendering bound on whatever the channel is now held at / sent to.
            assert abs(cs.commanded_b - goal) <= _bound(goal) + 1e-9, (start, goal, cs)
            # Never stranded: a rendering that is not the current level IS a
            # command, and it lands on a grid level; a held level emits nothing.
            if start.on and rendered == start.commanded_b:
                assert plan.commands == [], (start, goal)
            else:
                assert len(plan.commands) == 1, (start, goal)
                assert cs.commanded_b in grid
            # And the channel now sits where the quantizer says it should.
            assert _step(ch, cs, goal, photo).commands == [], (start, goal)


def test_render_bound_and_gate_on_the_default_curve() -> None:
    """§8.3/D27 (property): over the whole range, from off, the floor, on- and
    off-grid lit levels, every goal is rendered within max(0.01, 0.08·b), a
    differing quantized level always emits, and a settled goal never re-emits."""
    _check_render_and_gate(FLOOR_CH, _photo_for(FLOOR_CH))


def test_render_bound_and_gate_on_a_calibrated_curve() -> None:
    """§8.3/D27: the grid lives in command space, so the bound holds on
    sofakrok's measured (near-linear, flat-topped) curve exactly as on b²."""
    ch = ChannelConfig("c", fixed_ct=2700, dim_floor=0.02, curve=SOFAKROK_CURVE)
    _check_render_and_gate(ch, _photo_for(ch))


def test_calibrated_curve_ramp_is_sized_in_flux() -> None:
    """§8.2/§4.3: slew sizing still uses the channel's flux curve — on sofakrok's
    curve 0.1 -> 0.5 is a 0.6186 flux move (b² would say 0.24)."""
    ch = ChannelConfig("c", fixed_ct=2700, dim_floor=0.02, curve=SOFAKROK_CURVE)
    photo = _photo_for(ch)
    cs = ChannelState(commanded_b=0.1, on=True)
    plan = _step(ch, cs, 0.505449, photo)
    cmd = plan.commands[0]
    expected = (photo.flux("c", 0.505449) - photo.flux("c", 0.1)) / TUN.slew_step
    assert isclose(cmd.ramp_seconds, expected)


def test_sub_step_jitter_around_a_boundary_emits_nothing() -> None:
    """§8.3/D27: goal jitter straddling the midpoint between two levels (a
    drifting own-sensor daylight factor) never re-commands a settled lamp."""
    lo, hi = 0.459499, 0.505449
    mid = (lo + hi) / 2
    gap = hi - lo
    cs = ChannelState()
    _step(FLOOR_CH, cs, lo)  # settle on the lower level
    emitted = 0
    for k in range(40):
        goal = mid + (0.15 if k % 2 else -0.15) * gap  # 0.3 of a step p2p
        emitted += len(_step(FLOOR_CH, cs, goal).commands)
    assert emitted == 0
    # A real move past the hysteresis band does step.
    assert len(_step(FLOOR_CH, cs, hi).commands) == 1
