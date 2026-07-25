"""Master gain (ENGINE_SPEC §7).

The master dimmer scales lux targets and open-loop outputs (rules 2.5, 4.6):
``G = 2 ** ((pct - 50)/50 * gain_range_stops)`` — 50 % neutral, off => 0.
Night path and away/sleep hard-offs are absolute and never scaled (rule 7.4);
the engine passes ``gain_exempt`` resolutions around this module.

Feature-module discipline: imports only :mod:`model` and :mod:`tunables`.
"""

from __future__ import annotations

from .model import Band, EngineState
from .tunables import Tunables, gain_multiplier


def multiplier(state: EngineState, tun: Tunables) -> float:
    """Current gain G (rule 7.1). Master light off => 0 (rule 7.2)."""
    if not state.master_on:
        return 0.0
    return gain_multiplier(state.master_pct, tun.gain_range_stops)


def scale(outputs: dict[Band, float], g: float) -> dict[Band, float]:
    """Scale per-band normalized outputs by gain, clamped to [0, 1] (rule 4.6)."""
    return {b: max(0.0, min(1.0, v * g)) for b, v in outputs.items()}


def relax_to_neutral(state: EngineState, tun: Tunables) -> None:
    """Drift the gain back to neutral on the morning edge (rule 7.3)."""
    if tun.gain_reset:
        state.master_pct = 50.0
