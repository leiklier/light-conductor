"""Colour-temperature policy (ENGINE_SPEC §5).

A CT-capable channel tracks ``ct_target = ct_day - E*(ct_day - ct_evening)``
(rule 5.1), clamped to hardware range, then:

- **blend anchoring** (rule 5.2): while any fixed-CT channel in the room is
  lit at >= ``blend_threshold`` output, the target is pulled to within
  ``blend_delta`` of the fixed channels' kelvin so mixed sources read as one
  scene. When the fixed channels are off (evening accent), CT may go fully
  warm;
- **low-output warmth** (rule 5.3): below ``warm_dim_output`` the upper cap
  slides toward ``ct_min_evening`` ("dim-to-warm").

Ordering (CT before brightness, rule 5.4) and the ``ct_min_delta`` rewrite
gate are enforced by the governor / adapter, not here.

Feature-module discipline: imports only :mod:`model` and :mod:`tunables`.
"""

from __future__ import annotations

from .model import ChannelConfig
from .tunables import Tunables


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def base_target(e: float, tun: Tunables) -> float:
    """The circadian CT target before per-channel clamping (rule 5.1)."""
    return tun.ct_day - e * (tun.ct_day - tun.ct_evening)


def ct_target(
    channel: ChannelConfig,
    e: float,
    output: float,
    fixed_anchor: int | None,
    tun: Tunables,
) -> int | None:
    """Target kelvin for a CT-capable channel (rules 5.1-5.3).

    ``fixed_anchor`` is the declared kelvin of the room's fixed-CT channels
    when at least one is lit at >= ``blend_threshold`` (rule 5.2), else None.
    Returns ``None`` for a non-CT channel (nothing to command).
    """
    if not channel.ct_capable or channel.ct_range is None:
        return None
    lo_hw, hi_hw = channel.ct_range
    target = base_target(e, tun)

    # Rule 5.3: dim light is warm — slide the *upper* cap toward ct_min_evening.
    if output < tun.warm_dim_output:
        # At output 0 the cap is ct_min_evening; at warm_dim_output it is ct_day.
        frac = output / tun.warm_dim_output if tun.warm_dim_output > 0 else 1.0
        warm_cap = tun.ct_min_evening + frac * (tun.ct_day - tun.ct_min_evening)
        target = min(target, warm_cap)

    # Rule 5.2: anchor to lit fixed-CT channels within blend_delta.
    if fixed_anchor is not None:
        target = _clamp(target, fixed_anchor - tun.blend_delta, fixed_anchor + tun.blend_delta)

    return round(_clamp(target, lo_hw, hi_hw))


def fixed_anchor(channels_output: dict[ChannelConfig, float], tun: Tunables) -> int | None:
    """The declared kelvin to anchor to, or None if no fixed channel is lit (rule 5.2).

    Uses the mean declared kelvin of fixed-CT channels currently at
    >= ``blend_threshold``.
    """
    lit = [
        ch.fixed_ct
        for ch, out in channels_output.items()
        if ch.fixed_ct is not None and not ch.ct_capable and out >= tun.blend_threshold
    ]
    if not lit:
        return None
    return round(sum(lit) / len(lit))
