"""Illuminance targets & circadian shaping (ENGINE_SPEC §2).

On the open-loop path a role selects per-band normalized outputs from the
profile tables; ACTIVE interpolates the day and evening tables by the
wind-down factor W (rule 2.3, D33), ADJACENT scales the interpolated
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


def _interp_band(day: BandMap, evening: BandMap, w: float) -> dict[Band, float]:
    bands = set(day) | set(evening)
    return {b: day.get(b, 0.0) * (1.0 - w) + evening.get(b, 0.0) * w for b in bands}


def active_outputs(profile: Profile, w: float) -> dict[Band, float]:
    """ACTIVE per-band outputs, interpolated day<->evening by W (rule 2.1, D33)."""
    return _interp_band(profile.out_active_day, profile.out_active_evening, w)


def role_outputs(profile: Profile, role: Role, w: float, tun: Tunables) -> dict[Band, float]:
    """Per-band normalized outputs for a role tier (rules 2.1, 1.5, 4.6).

    Interpolated by the wind-down factor W (D33), not by darkness.

    The evening cap (rule 2.4) is applied by the caller in the §8 funnel so
    every path honours it; this returns the pre-cap tier outputs.
    """
    if role in (Role.OFF, Role.NIGHT_PATH, Role.TV):
        # NIGHT_PATH / TV outputs are mode tables, resolved in modes.py.
        return dict.fromkeys(BAND_ORDER, 0.0)
    active = active_outputs(profile, w)
    if role is Role.ACTIVE:
        return active
    if role is Role.ADJACENT:
        return {b: v * tun.adjacent_fraction for b, v in active.items()}
    # BACKGROUND: the open-loop background table (rule 4.6).
    return dict(profile.out_background)


#: Width of the W window over which the BOOST band hands over from its tier
#: output to its evening value — ``boost_evening_output`` or 0 (rule 4.5, D33).
#: The window ENDS at ``boost_evening_max``, so at and above it the band is
#: exactly what beta.19 made it (locked out, or the explicit evening value);
#: below ``boost_evening_max - BOOST_FADE`` it is the plain tier. 0.2 keeps the
#: kitchen bench strip (0.8x - 0.5 mapping, 0.70 -> 0.85) inside one grid step
#: per ``circadian_tick`` at the fastest W ramp. An engineering constant.
BOOST_FADE = 0.2


def evening_cap(w: float, profile: Profile) -> float:
    """The evening cap at wind-down ``w`` (rule 2.4, D33).

    ``cap(W) = 1 - W·(1 - evening_output_cap)``: non-binding at W = 0, the
    profile's ``evening_output_cap`` at W = 1, linear and monotone between.
    It is the same straight line the ACTIVE interpolation draws from "full
    output" to the cap, so a profile whose evening tier sits at or below its
    cap (every live room) is never dimmed BY the cap — the interpolation does
    it, and the cap only trims what rides above (master gain > 1, closed-loop
    demand). beta.19 switched the cap on at E >= 0.5 and the kitchen fell
    0.625 -> 0.25 in one write.
    """
    w = max(0.0, min(1.0, w))
    return (1.0 - w) + w * profile.evening_output_cap  # exact at both ends


def apply_evening_cap(
    outputs: dict[Band, float], w: float, profile: Profile, tun: Tunables
) -> dict[Band, float]:
    """Clamp normalized outputs to the continuous evening cap (rule 2.4, D33)."""
    cap = evening_cap(w, profile)
    return {b: min(v, cap) for b, v in outputs.items()}


def boost_weight(w: float, tun: Tunables) -> float:
    """How much of the BOOST band is still its tier output (rule 4.5, D33).

    1 below ``boost_evening_max - BOOST_FADE``, 0 at and above
    ``boost_evening_max``, linear between. The band output is
    ``β·tier + (1 - β)·evening`` where ``evening`` is the explicit
    ``boost_evening_output`` (ACTIVE rooms that set one) or 0 (the lockout) —
    the hand-over is a fade across a W window, not a step at the threshold.
    """
    lo = tun.boost_evening_max - BOOST_FADE
    if w <= lo:
        return 1.0
    if w >= tun.boost_evening_max:
        return 0.0
    return (tun.boost_evening_max - w) / BOOST_FADE


def boost_evening_value(role: Role, profile: Profile, g: float) -> float | None:
    """The BOOST band's explicit evening output, or ``None`` (rule 4.5, D26).

    Only for an **ACTIVE** room whose profile sets ``boost_evening_output``:
    the value the band takes inside the lockout window instead of 0. Exempt
    from the evening cap — an explicit "this is what the bench strip does in
    the evening" value, not a tier the cap should trim — but scaled by the
    master gain ``g`` (0 with the master off, rule 7.2: no room may hold a band
    the master switch cannot extinguish). ADJACENT/BACKGROUND, the §6 mode
    tables and the closed loop never get one; their boost band fades to 0.
    """
    if profile.boost_evening_output is None or role is not Role.ACTIVE:
        return None
    return max(0.0, min(1.0, profile.boost_evening_output * g))


def daylight_factor(n_hat: float, tun: Tunables, full: float | None = None) -> float:
    """Daylight damping factor ``D`` (rule 4.7).

    ``D = clamp(1 - N̂ / full, daylight_min_factor, 1.0)``: bright daylight
    pulls the open-loop tables down, darkness leaves them at full. Replicates
    the legacy ``100 - 0.5·lux`` damping for rooms whose sensors read daylight
    well but are nearly blind to their own lamps (§3.1). ``N̂`` for an untrusted
    room ≈ the filtered lux.

    ``full`` is the room's per-room ``daylight_full`` override (D26); ``None``
    (or a non-positive value) falls back to the global tunable — sensors differ
    by an order of magnitude at the same noon (kjøkken 40-60 lx, spisebord
    300-450 lx), so one global reference cannot serve both.
    """
    reference = full if full else tun.daylight_full
    if reference <= 0.0:
        return 1.0
    d = 1.0 - n_hat / reference
    return max(tun.daylight_min_factor, min(1.0, d))


def outdoor_dusk_factor(n_hat: float | None, e: float, tun: Tunables) -> float:
    """Dusk factor for an outdoor room (rule 6.5a).

    ``n_hat`` is the room's natural-light estimate when it has a FRESH lux
    sensor, else ``None``.

    With no sensor (or a stale one, §3.5) this is the pre-6.5a all-or-nothing
    ``E >= outdoor_on_threshold`` gate, unchanged.

    With a sensor the measured ramp governs in BOTH directions — 0 -> 1 as
    ``N̂`` falls from ``outdoor_on_lux`` to ``outdoor_full_lux``, so an overcast
    evening lights the balcony when it actually gets dark and a summer morning
    releases it when the light returns rather than hours later when the sun
    ramp finally crosses its threshold. The one override is the dead of night
    (``E`` at its plateau: the sun below ``sun_low_deg``, or past the evening
    clock ramp): there the balcony is lit as it always was, so a sensor reading
    falsely bright — an indoor lamp, a torch, a wedge stuck at a daylight
    value — can never leave the balcony dark all night.
    """
    if n_hat is None:
        return 1.0 if e >= tun.outdoor_on_threshold else 0.0
    if e >= 1.0:
        return 1.0
    span = tun.outdoor_on_lux - tun.outdoor_full_lux
    measured = (tun.outdoor_on_lux - n_hat) / span if span > 0.0 else 0.0
    return max(0.0, min(1.0, measured))


def apply_daylight(
    outputs: dict[Band, float], n_hat: float, tun: Tunables, full: float | None = None
) -> dict[Band, float]:
    """Scale the ACTIVE/ADJACENT/BACKGROUND open-loop outputs by ``D`` (rule 4.7)."""
    d = daylight_factor(n_hat, tun, full)
    return {b: v * d for b, v in outputs.items()}


def peak_output(outputs: dict[Band, float]) -> float:
    """The brightest band output (for diagnostics, rule 10)."""
    return max(outputs.values(), default=0.0)
