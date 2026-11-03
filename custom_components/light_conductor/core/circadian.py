"""Circadian factors E (darkness) and W (wind-down) (ENGINE_SPEC §2.3, D33).

``E in [0, 1]`` (0 = full day, 1 = full evening) = ``max(E_sun, E_clock)`` —
"it is dark". ``W = max(E_clock, min(E_sun, dusk_ceiling))`` — "it is late":
the sun alone can only carry the wind-down to ``dusk_ceiling``, the clock
carries it the rest of the way. With ``dusk_ceiling`` 1.0 (default) W ≡ E —
the same factor everywhere, though the now-continuous evening cap (§2.4)
still makes dusk brighter than beta.19 (D33). In
a Nordic winter the two are hours apart (2 Nov: E ≥ 0.5 from ~16:09; 21 Dec
from ~14:51) and one factor doing both jobs dimmed the house to its bedtime
tier mid-afternoon.

Pure functions of (sun elevation, local time-of-day) — both arrive through
events, so this stays deterministic (rule 0). No hard steps: both factors are
continuous and the engine re-evaluates them every ``circadian_tick`` while
either term is inside a ramp (:func:`ramping`).

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


def wind_down(elevation_deg: float | None, now: datetime, tun: Tunables) -> float:
    """The wind-down factor W = max(E_clock, min(E_sun, dusk_ceiling)) (rule 2.3, D33).

    W drives everything that means "it is late": the day<->evening tier
    interpolation (ACTIVE/ADJACENT derivation, closed-loop lux targets), the
    evening cap and the boost evening lockout. E keeps everything that means
    "it is dark" (CT, outdoor gates, the corridor evening gate).
    """
    return max(e_clock(now, tun), min(e_sun(elevation_deg, tun), tun.dusk_ceiling))


def ramping(elevation_deg: float | None, now: datetime, tun: Tunables) -> bool:
    """Whether E or W is inside a ramp — the engine ticks while it is (rule 2.3).

    Either term strictly between 0 and 1. Checked per TERM, not on E: with the
    sun down E sits at 1 while W still climbs 20:00 -> 22:30 on the clock, and
    a plateau test on E alone would sleep until 06:00 (D33).
    """
    c = e_clock(now, tun)
    s = e_sun(elevation_deg, tun)
    return 0.0 < c < 1.0 or 0.0 < s < 1.0


def is_evening(elevation_deg: float | None, now: datetime, tun: Tunables) -> bool:
    """Whether E (darkness) has crossed ``evening_cap_threshold`` (rule 1.7)."""
    return factor(elevation_deg, now, tun) >= tun.evening_cap_threshold
