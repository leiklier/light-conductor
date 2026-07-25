"""Write governor — the engine-side of actuator discipline (ENGINE_SPEC §8).

The engine owns slew sizing, quantization, min-delta, the dim floor, and
off-is-off (rules 8.2/8.3/8.6). It emits one :class:`~.plan.SetChannel` /
:class:`~.plan.TurnOffChannel` per changed channel, carrying the goal and
the ``ramp_seconds`` needed so the move's flux-relative rate never exceeds
the slew bound. The *adapter* owns rate-limit, latest-wins coalescing, the
``max_inflight`` concurrency cap, and the echo ledger (§8.3/8.4) — those are
not this module's concern.

All step sizing is flux-relative (rule 4.3): a "small step" looks small at
any level. CT is only rewritten when it moves >= ``ct_min_delta`` (rule 5.4).

Feature-module discipline: imports only :mod:`model`, :mod:`tunables`,
:mod:`plan`.
"""

from __future__ import annotations

from .model import ChannelConfig, ChannelState, FluxModel
from .plan import Plan
from .tunables import Tunables


def _ramp_seconds(
    f0: float, f1: float, slew: float, tun: Tunables, override: float | None
) -> float:
    """Seconds to move flux ``f0 -> f1`` at the slew rate (rule 8.2)."""
    if override is not None:
        return override
    return abs(f1 - f0) / slew * tun.slew_interval


def plan_channel(
    plan: Plan,
    channel: ChannelConfig,
    cs: ChannelState,
    room_active: bool,
    goal_b: float,
    goal_ct: int | None,
    flux: FluxModel,
    tun: Tunables,
    fade: float | None = None,
) -> None:
    """Reconcile one channel toward its goal (rules 8.2-8.6, 5.4).

    ``room_active`` selects the slew rate: occupied eyes get ``slew_step``,
    empty rooms and demotions get the faster ``slew_step_empty`` (rule 8.2).
    ``fade`` overrides the computed ramp for mode transitions (sleep/night).
    """
    cid = channel.channel_id
    slew = tun.slew_step if room_active else tun.slew_step_empty
    max_flux = flux.flux(cid, 1.0)

    # Off is off (rule 8.6): goal 0 -> turn_off after ramping down.
    if goal_b <= 0.0:
        if cs.on:
            cur = flux.flux(cid, cs.commanded_b)
            plan.turn_off(cid, _ramp_seconds(cur, 0.0, slew, tun, fade))
            cs.on = False
            cs.commanded_b = 0.0
        return

    # A lit channel is floored to its dim floor (rules 4.1/4.6).
    goal_b = max(goal_b, channel.dim_floor)

    # Quantize in flux space (rule 8.3), never below the dim floor.
    goal_flux = flux.flux(cid, goal_b)
    q_flux = round(goal_flux / tun.min_delta) * tun.min_delta
    q_flux = max(flux.flux(cid, channel.dim_floor), min(max_flux, q_flux))
    goal_b_q = flux.command_for_flux(cid, q_flux)

    # Off is off (rule 8.6): a positive goal that quantizes to nothing (e.g.
    # dim_floor 0 + a sub-grid level) must turn off, never emit level 0 (F8).
    if goal_b_q <= 0.0 or q_flux <= 0.0:
        if cs.on:
            cur = flux.flux(cid, cs.commanded_b)
            plan.turn_off(cid, _ramp_seconds(cur, 0.0, slew, tun, fade))
            cs.on = False
            cs.commanded_b = 0.0
        return

    cur_flux = flux.flux(cid, cs.commanded_b) if cs.on else 0.0
    crossing_on = not cs.on
    delta = abs(q_flux - cur_flux)

    # CT rewrite gate (rule 5.4): only when it moves >= ct_min_delta.
    ct_cmd: int | None = None
    if goal_ct is not None and (
        cs.commanded_ct is None or abs(goal_ct - cs.commanded_ct) >= tun.ct_min_delta
    ):
        ct_cmd = goal_ct

    # Write economy (rule 8.3): skip sub-min_delta brightness moves unless a
    # CT rewrite is due or we are crossing on.
    if delta < tun.min_delta and not crossing_on and ct_cmd is None:
        return

    plan.set_channel(cid, goal_b_q, ct_cmd, _ramp_seconds(cur_flux, q_flux, slew, tun, fade))
    cs.on = True
    cs.commanded_b = goal_b_q
    if ct_cmd is not None:
        cs.commanded_ct = ct_cmd
