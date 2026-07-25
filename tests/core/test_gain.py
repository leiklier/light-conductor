"""§7: master gain scaling and neutral drift."""

from __future__ import annotations

from custom_components.light_conductor.core import gain
from custom_components.light_conductor.core.model import Band, EngineState
from custom_components.light_conductor.core.tunables import Tunables

TUN = Tunables()


def test_multiplier_neutral_boost_dim() -> None:
    """§7.1: 50 % neutral, 100 % x2, low % dims."""
    assert gain.multiplier(EngineState(master_pct=50.0), TUN) == 1.0
    assert gain.multiplier(EngineState(master_pct=100.0), TUN) == 2.0
    assert gain.multiplier(EngineState(master_pct=0.0), TUN) == 0.5


def test_master_off_is_zero_gain() -> None:
    """§7.2: master light off => gain 0."""
    assert gain.multiplier(EngineState(master_on=False, master_pct=100.0), TUN) == 0.0


def test_scale_clamps_to_unit() -> None:
    """§4.6: gain-scaled outputs clamp to [0, 1]."""
    assert gain.scale({Band.PRIMARY: 0.6}, 2.0) == {Band.PRIMARY: 1.0}
    assert gain.scale({Band.PRIMARY: 0.4}, 0.5) == {Band.PRIMARY: 0.2}


def test_relax_to_neutral_respects_toggle() -> None:
    """§7.3: neutral drift only when gain_reset is on."""
    s = EngineState(master_pct=20.0)
    gain.relax_to_neutral(s, TUN)
    assert s.master_pct == 50.0
    s2 = EngineState(master_pct=20.0)
    from dataclasses import replace

    gain.relax_to_neutral(s2, replace(TUN, gain_reset=False))
    assert s2.master_pct == 20.0
