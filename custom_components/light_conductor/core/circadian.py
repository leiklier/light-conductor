"""Circadian factor E (ENGINE_SPEC §2.3).

``E in [0, 1]`` (0 = full day, 1 = full evening) = ``max(E_sun, E_clock)``.
Pure function of (sun elevation, local time-of-day) — both arrive through
events, so this stays deterministic (rule 0). No hard steps: E is continuous
and the engine evaluates it on the coarse ``circadian_tick`` schedule, so it
never jumps on its own; the slew limiter (§8.2) bounds any residual step.

Feature-module discipline: imports only :mod:`tunables`.
"""

from __future__ import annotations

from datetime import datetime

from .tunables import Tunables


def _ramp(x: float, lo: float, hi: float) -> float:
    """0 below ``lo``, 1 above ``hi``, linear between (``lo`` may exceed ``hi``)."""
    if lo == hi:
        return 1.0 if x >= hi else 0.0
    t = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, t))


def e_sun(elevation_deg: float | None, tun: Tunables) -> float:
    """Evening weight from sun elevation (rule 2.3).

    Ramps 0 -> 1 as elevation falls from ``sun_high_deg`` to ``sun_low_deg``.
    Unknown elevation contributes 0 (fail-safe: rely on the clock term).
    """
    if elevation_deg is None:
        return 0.0
    # Falling elevation raises E: invert the ramp direction.
    return 1.0 - _ramp(elevation_deg, tun.sun_low_deg, tun.sun_high_deg)


def _minute_of_day(now: datetime) -> int:
    return now.hour * 60 + now.minute


def e_clock(now: datetime, tun: Tunables) -> float:
    """Evening weight from wall-clock time (rule 2.3).

    0 through the day, ramps up ``evening_start -> evening_full``, holds 1
    overnight, ramps back down ``morning_start -> morning_full``. Guarantees
    a cozy ramp-down even in Nordic summer when the sun sets late.
    """
    m = _minute_of_day(now)
    if m < tun.morning_start_min:
        return 1.0  # still overnight before the morning ramp
    if m < tun.morning_full_min:
        return 1.0 - _ramp(m, tun.morning_start_min, tun.morning_full_min)
    if m < tun.evening_start_min:
        return 0.0  # full day
    return _ramp(m, tun.evening_start_min, tun.evening_full_min)


def factor(elevation_deg: float | None, now: datetime, tun: Tunables) -> float:
    """The circadian factor E = max(E_sun, E_clock) (rule 2.3)."""
    return max(e_sun(elevation_deg, tun), e_clock(now, tun))


def is_evening(elevation_deg: float | None, now: datetime, tun: Tunables) -> bool:
    """Whether E has crossed the evening threshold (rules 1.7, 2.4)."""
    return factor(elevation_deg, now, tun) >= tun.evening_cap_threshold
