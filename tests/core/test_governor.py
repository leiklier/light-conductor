"""§8: write governor — slew sizing, quantization, min-delta, off-is-off."""

from __future__ import annotations

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


def test_slew_rate_bounded_active_vs_empty() -> None:
    """§8.2: ramp is sized so flux rate <= slew bound; empty rooms slew faster."""
    photo = _photo()
    for active, step in ((True, TUN.slew_step), (False, TUN.slew_step_empty)):
        cs = ChannelState()
        cmd = _plan(cs, active, 1.0).commands[0]
        assert isinstance(cmd, SetChannel)
        flux_delta = photo.flux("c", cmd.level)
        rate = flux_delta / cmd.ramp_seconds
        assert rate <= step / TUN.slew_interval + 1e-9
    # Empty ramp is quicker than the active ramp for the same move.
    active_ramp = _plan(ChannelState(), True, 1.0).commands[0].ramp_seconds
    empty_ramp = _plan(ChannelState(), False, 1.0).commands[0].ramp_seconds
    assert empty_ramp < active_ramp


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
