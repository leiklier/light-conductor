"""Illuminance targets & circadian shaping (ENGINE_SPEC §2).

On the open-loop path (every room this PR) a role selects per-band
normalized outputs from the profile tables; ACTIVE interpolates the day and
evening tables by the circadian factor E, ADJACENT scales the interpolated
ACTIVE by ``adjacent_fraction`` (rule 1.5), BACKGROUND uses the profile's
``out_background`` table (rule 4.6), OFF is zero.

Structured for closed-loop drop-in: the same tier machinery would produce a
lux target T (``lux_*`` on the profile) instead of a normalized output, then
:mod:`photometry` would convert; here it produces the normalized output
directly (rule 2.2).

Feature-module discipline: imports only :mod:`model` and :mod:`tunables`.
"""

from __future__ import annotations

from .model import BAND_ORDER, Band, BandMap, Profile, Role
from .tunables import Tunables


def _interp_band(day: BandMap, evening: BandMap, e: float) -> dict[Band, float]:
    bands = set(day) | set(evening)
    return {b: day.get(b, 0.0) * (1.0 - e) + evening.get(b, 0.0) * e for b in bands}


def active_outputs(profile: Profile, e: float) -> dict[Band, float]:
    """ACTIVE per-band outputs, interpolated day<->evening by E (rule 2.1)."""
    return _interp_band(profile.out_active_day, profile.out_active_evening, e)


def role_outputs(profile: Profile, role: Role, e: float, tun: Tunables) -> dict[Band, float]:
    """Per-band normalized outputs for a role tier (rules 2.1, 1.5, 4.6).

    The evening cap (rule 2.4) is applied by the caller in the §8 funnel so
    every path honours it; this returns the pre-cap tier outputs.
    """
    if role in (Role.OFF, Role.NIGHT_PATH, Role.TV):
        # NIGHT_PATH / TV outputs are mode tables, resolved in modes.py.
        return dict.fromkeys(BAND_ORDER, 0.0)
    active = active_outputs(profile, e)
    if role is Role.ACTIVE:
        return active
    if role is Role.ADJACENT:
        return {b: v * tun.adjacent_fraction for b, v in active.items()}
    # BACKGROUND: the open-loop background table (rule 4.6).
    return dict(profile.out_background)


def apply_evening_cap(
    outputs: dict[Band, float], e: float, profile: Profile, tun: Tunables
) -> dict[Band, float]:
    """Clamp normalized outputs to ``evening_output_cap`` once E is high (rule 2.4)."""
    if e < tun.evening_cap_threshold:
        return outputs
    cap = profile.evening_output_cap
    return {b: min(v, cap) for b, v in outputs.items()}


def peak_output(outputs: dict[Band, float]) -> float:
    """The brightest band output (for diagnostics, rule 10)."""
    return max(outputs.values(), default=0.0)
