"""§8: write governor — slew sizing, quantization, min-delta, off-is-off."""

from __future__ import annotations

from math import sqrt

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


def test_min_delta_skips_tiny_moves() -> None:
    """§8.3: a sub-min_delta brightness move with no CT change is skipped."""
    photo = _photo()
    cs = ChannelState(commanded_b=0.6, commanded_ct=3000, on=True)
    goal_b = photo.command_for_flux("c", photo.flux("c", 0.6) + 0.01)  # +0.01 flux < min_delta
    plan = Plan()
    governor.plan_channel(plan, CH, cs, True, goal_b, None, photo, TUN)
    assert plan.commands == []


def test_slew_ramp_numeric_active_and_empty() -> None:
    """§8.2: ramp_seconds = flux_step / slew * interval — concrete values.

    A flux step of 0.5 at slew_step 0.1 / interval 1.0 must ramp over exactly
    5.0 s while ACTIVE; the same step at slew_step_empty 0.25 must ramp over
    2.0 s. (min_delta 0.05 keeps flux 0.5 exactly on the quantization grid.)"""
    from dataclasses import replace

    tun = replace(TUN, min_delta=0.05, slew_step=0.1, slew_interval=1.0, slew_step_empty=0.25)
    photo = _photo()
    goal_b = sqrt(0.5)  # b**2 curve => flux 0.5

    active = Plan()
    governor.plan_channel(active, CH, ChannelState(), True, goal_b, None, photo, tun)
    assert active.commands[0].ramp_seconds == 5.0  # 0.5 / 0.1 * 1.0

    empty = Plan()
    governor.plan_channel(empty, CH, ChannelState(), False, goal_b, None, photo, tun)
    assert empty.commands[0].ramp_seconds == 2.0  # 0.5 / 0.25 * 1.0


def test_slew_ramp_scales_linearly_with_step() -> None:
    """§8.2 (mutation-sensitive): halving the flux step halves ramp_seconds."""
    from dataclasses import replace

    tun = replace(TUN, min_delta=0.05, slew_step=0.1, slew_interval=1.0)
    photo = _photo()
    big = Plan()
    governor.plan_channel(big, CH, ChannelState(), True, sqrt(0.5), None, photo, tun)
    small = Plan()
    governor.plan_channel(small, CH, ChannelState(), True, sqrt(0.25), None, photo, tun)
    # Steps 0.5 and 0.25 -> ramps 5.0 and 2.5; ratio matches the step ratio.
    assert big.commands[0].ramp_seconds / small.commands[0].ramp_seconds == 2.0


def test_zero_dim_floor_quantizes_to_off() -> None:
    """§8.6 (F8): a positive goal that quantizes to nothing turns off, never
    emits SetChannel(level=0)."""
    ch = ChannelConfig("c", fixed_ct=2700, dim_floor=0.0)
    photo = RoomPhotometry(RoomConfig("r", (ch,), Profile()))
    cs = ChannelState(commanded_b=0.5, on=True)
    plan = Plan()
    governor.plan_channel(plan, ch, cs, True, 0.01, None, photo, TUN)  # flux 1e-4 -> grid 0
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
