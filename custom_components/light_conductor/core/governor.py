"""Write governor — the engine-side of actuator discipline (ENGINE_SPEC §8).

The engine owns slew sizing, quantization, the write-economy gate, the dim
floor, and off-is-off (rules 8.2/8.3/8.6). It emits one
:class:`~.plan.SetChannel` / :class:`~.plan.TurnOffChannel` per changed
channel, carrying the goal and the ``ramp_seconds`` needed so the move's
flux-relative rate never exceeds the slew bound. The *adapter* owns rate-limit,
latest-wins coalescing, the ``max_inflight`` concurrency cap, and the echo
ledger (§8.3/8.4) — those are not this module's concern.

Quantization (rule 8.3, D27) happens in COMMAND space on a grid that is dense
at the low end: ``g_0 = dim_floor``, ``g_{k+1} = g_k + max(quant_step,
quant_rel·g_k)``, capped by ``1.0`` — 0.01 steps up to 10 %, then 10 % relative
steps (34 levels for the default ``dim_floor`` 0.02). The quantizer is sticky:
a lit channel keeps its level while the goal stays within ``QUANT_HOLD`` of a
local grid step of it, so goal jitter around a boundary emits nothing. Slew and
ramp sizing stay in flux space (rule 4.3). CT is only rewritten when it moves
>= ``ct_min_delta`` (rule 5.4).

Feature-module discipline: imports only :mod:`model`, :mod:`tunables`,
:mod:`plan`.
"""

from __future__ import annotations

from bisect import bisect_left
from functools import lru_cache

from .model import ChannelConfig, ChannelState, FluxModel
from .plan import Plan
from .tunables import Tunables

#: Sticky-quantizer hold band (rule 8.3, D27): a lit channel keeps its current
#: level while the goal is within ``QUANT_HOLD`` of the local grid step
#: ``max(quant_step, quant_rel·min(goal, current))`` of it. Rendering error is
#: then at most ``0.7`` of a step — ``max(0.007, 0.07·b)`` on the default grid,
#: inside the normative ``max(0.01, 0.08·b)`` — and goal jitter below 0.4 of a
#: step peak-to-peak can never re-command a channel. Outside the band the goal
#: renders to its NEAREST level, which is then provably a different level (so
#: the gate cannot strand a channel) and provably inside the new band (so the
#: same goal never re-emits). An engineering constant, not a §12 tunable: the
#: operator surface is ``quant_step`` / ``quant_rel``.
QUANT_HOLD = 0.7


@lru_cache(maxsize=32)
def level_grid(dim_floor: float, step: float, rel: float) -> tuple[float, ...]:
    """The command-space quantization grid for one channel (rule 8.3, D27).

    ``g_0 = dim_floor`` (the floor is always a level, so a floored goal is
    rendered exactly), ``g_{k+1} = g_k + max(step, rel·g_k)``, and ``1.0`` is
    always the last level (full output must be reachable). Absolute steps at
    the dim end — where a relative step would collapse below the device's
    1/255 resolution — and relative (Weber-like) steps above ``step/rel``.
    """
    floor = max(0.0, min(1.0, dim_floor))
    levels = [floor]
    g = floor
    while True:
        g = round(g + max(step, rel * g), 6)
        if g >= 1.0:
            break
        levels.append(g)
    if levels[-1] < 1.0:
        levels.append(1.0)
    return tuple(levels)


def _nearest(grid: tuple[float, ...], b: float) -> int:
    """Index of the grid level nearest to ``b`` (ties round up)."""
    i = bisect_left(grid, b)
    if i <= 0:
        return 0
    if i >= len(grid):
        return len(grid) - 1
    return i if grid[i] - b <= b - grid[i - 1] else i - 1


def quantize(channel: ChannelConfig, cs: ChannelState, goal_b: float, tun: Tunables) -> float:
    """The level the governor renders for ``goal_b`` (rules 8.3/8.6, D27).

    Returns ``0.0`` for off (goal 0, or a positive goal nearest to a zero dim
    floor — off is off, F8). A lit channel whose current level ``c`` (the
    ledger value, which may sit off the grid: a seeded or adopted level) lies
    within ``QUANT_HOLD · max(quant_step, quant_rel·min(goal, c))`` of the
    floored goal is HELD — ``c`` itself is returned, so the write gate emits
    nothing. Otherwise the goal renders to its nearest grid level (always
    ``>= dim_floor``). Pure — the engine calls it to compute dim-to-warm CT on
    the level that will actually be held or emitted (rule 5.3), and
    :func:`plan_channel` calls it again with identical inputs.
    """
    if goal_b <= 0.0:
        return 0.0
    # A lit channel is floored to its dim floor (rules 4.1/4.6).
    goal_b = max(min(goal_b, 1.0), channel.dim_floor)
    if cs.on and cs.commanded_b > 0.0:
        cur = cs.commanded_b
        band = QUANT_HOLD * max(tun.quant_step, tun.quant_rel * min(goal_b, cur))
        if abs(goal_b - cur) <= band:
            return cur
    grid = level_grid(channel.dim_floor, tun.quant_step, tun.quant_rel)
    return grid[_nearest(grid, goal_b)]


def _ramp_seconds(
    f0: float,
    f1: float,
    slew: float,
    tun: Tunables,
    override: float | None,
    crossing_on: bool = False,
) -> float:
    """Seconds to move flux ``f0 -> f1`` at the slew rate (rule 8.2).

    A TURN-ON (``crossing_on`` — the channel is off and the goal is positive)
    is additionally capped at ``on_ramp_max`` (D26): the slew bound protects the
    continuity of a lit room, and a dark room has none. An explicit mode
    ``override`` fade (sleep/night) always wins, and dimming/turn-off ramps are
    untouched.
    """
    if override is not None:
        return override
    ramp = abs(f1 - f0) / slew * tun.slew_interval
    if crossing_on:  # the only caller passing True has already guaranteed f1 > 0
        ramp = min(ramp, tun.on_ramp_max)
    return ramp


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
    A channel whose level was OBSERVED rather than commanded (``cs.reconcile``:
    seeded lit at startup, or reported at an availability recovery) converges
    with ``slew_step_empty`` on its next move — the quiet reconcile of rules
    8.5/11.1 (D28). ``fade`` overrides the computed ramp for mode transitions
    (sleep/night).
    """
    cid = channel.channel_id
    slew = tun.slew_step if room_active and not cs.reconcile else tun.slew_step_empty

    # Off is off (rule 8.6): goal 0 -> turn_off after ramping down.
    if goal_b <= 0.0:
        if cs.on:
            cur = flux.flux(cid, cs.commanded_b)
            plan.turn_off(cid, _ramp_seconds(cur, 0.0, slew, tun, fade))
            cs.on = False
            cs.commanded_b = 0.0
            cs.reconcile = False
        return

    # Quantize on the command-space grid (rule 8.3, D27) — sticky for a lit
    # channel, never below the dim floor.
    goal_b_q = quantize(channel, cs, goal_b, tun)

    # Off is off (rule 8.6): a positive goal that quantizes to nothing (e.g.
    # dim_floor 0 + a sub-grid level) must turn off, never emit level 0 (F8).
    if goal_b_q <= 0.0:
        if cs.on:
            cur = flux.flux(cid, cs.commanded_b)
            plan.turn_off(cid, _ramp_seconds(cur, 0.0, slew, tun, fade))
            cs.on = False
            cs.commanded_b = 0.0
            cs.reconcile = False
        return

    q_flux = flux.flux(cid, goal_b_q)
    cur_flux = flux.flux(cid, cs.commanded_b) if cs.on else 0.0
    crossing_on = not cs.on

    # CT rewrite gate (rule 5.4): only when it moves >= ct_min_delta.
    ct_cmd: int | None = None
    if goal_ct is not None and (
        cs.commanded_ct is None or abs(goal_ct - cs.commanded_ct) >= tun.ct_min_delta
    ):
        ct_cmd = goal_ct

    # Write economy (rule 8.3): a HELD channel (the quantizer returned its
    # current level) emits nothing unless a CT rewrite is due. Any other
    # rendering IS a command — the gate can never strand a channel (the beta.19
    # floor bug: an absolute 0.03-flux gate larger than the first grid step
    # above the floor left every dim goal pinned at 2 %).
    held = cs.on and goal_b_q == cs.commanded_b
    if held and ct_cmd is None:
        return

    plan.set_channel(
        cid, goal_b_q, ct_cmd, _ramp_seconds(cur_flux, q_flux, slew, tun, fade, crossing_on)
    )
    cs.on = True
    cs.commanded_b = goal_b_q
    cs.reconcile = False
    if ct_cmd is not None:
        cs.commanded_ct = ct_cmd
