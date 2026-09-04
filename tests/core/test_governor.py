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


def _lit(flux: float) -> ChannelState:
    """A channel already lit at ``flux`` (b² curve) — a MOVE, not a turn-on, so
    the §8.2 turn-on cap (D26) does not apply and the slew formula is visible."""
    return ChannelState(commanded_b=sqrt(flux), on=True)


def test_slew_ramp_numeric_active_and_empty() -> None:
    """§8.2: ramp_seconds = flux_step / slew * interval — concrete values.

    A flux step of 0.5 at slew_step 0.1 / interval 1.0 must ramp over exactly
    5.0 s while ACTIVE; the same step at slew_step_empty 0.25 must ramp over
    2.0 s. (min_delta 0.05 keeps the fluxes exactly on the quantization grid.)"""
    from dataclasses import replace

    tun = replace(TUN, min_delta=0.05, slew_step=0.1, slew_interval=1.0, slew_step_empty=0.25)
    photo = _photo()
    goal_b = sqrt(0.75)  # b**2 curve => flux 0.75, i.e. a step of 0.5 from 0.25

    active = Plan()
    governor.plan_channel(active, CH, _lit(0.25), True, goal_b, None, photo, tun)
    assert active.commands[0].ramp_seconds == 5.0  # 0.5 / 0.1 * 1.0

    empty = Plan()
    governor.plan_channel(empty, CH, _lit(0.25), False, goal_b, None, photo, tun)
    assert empty.commands[0].ramp_seconds == 2.0  # 0.5 / 0.25 * 1.0


def test_slew_ramp_scales_linearly_with_step() -> None:
    """§8.2 (mutation-sensitive): halving the flux step halves ramp_seconds."""
    from dataclasses import replace

    tun = replace(TUN, min_delta=0.05, slew_step=0.1, slew_interval=1.0)
    photo = _photo()
    big = Plan()
    governor.plan_channel(big, CH, _lit(0.25), True, sqrt(0.75), None, photo, tun)
    small = Plan()
    governor.plan_channel(small, CH, _lit(0.25), True, sqrt(0.5), None, photo, tun)
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


# --- §8.2 (D26): the turn-on ramp cap -------------------------------------


def test_turn_on_ramp_is_capped_at_on_ramp_max() -> None:
    """§8.2/D26: lighting a dark channel is capped at on_ramp_max — the
    full-range ramp (flux 1.0 / slew_step 0.1 = 10 s) read as sluggish."""
    from dataclasses import replace

    tun = replace(TUN, min_delta=0.05, slew_step=0.1, slew_interval=1.0, on_ramp_max=3.0)
    photo = _photo()
    plan = Plan()
    governor.plan_channel(plan, CH, ChannelState(), True, 1.0, None, photo, tun)
    assert plan.commands[0].ramp_seconds == 3.0  # capped, not 10.0

    # A turn-on already shorter than the cap is untouched.
    short = Plan()
    governor.plan_channel(short, CH, ChannelState(), True, sqrt(0.2), None, photo, tun)
    assert short.commands[0].ramp_seconds == 2.0  # 0.2 / 0.1 * 1.0


def test_dim_and_off_ramps_are_not_capped() -> None:
    """§8.2/D26: only a TURN-ON is capped — a dim and a fade to off keep their
    full slew-derived ramp (that continuity is what the slew bound is for)."""
    from dataclasses import replace

    tun = replace(TUN, min_delta=0.05, slew_step=0.1, slew_interval=1.0, on_ramp_max=3.0)
    photo = _photo()
    dim = Plan()
    lit = ChannelState(commanded_b=1.0, on=True)
    governor.plan_channel(dim, CH, lit, True, sqrt(0.5), None, photo, tun)
    assert dim.commands[0].ramp_seconds == 5.0  # |1.0 - 0.5| / 0.1

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
